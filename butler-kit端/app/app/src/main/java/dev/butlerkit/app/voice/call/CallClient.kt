package dev.butlerkit.app.voice.call

import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import dev.butlerkit.app.data.Prefs
import dev.butlerkit.app.net.AccessAuth
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 通話的 WebSocket（協定 v1）：連線、8 秒心跳、斷線自動重連。
 *
 * 為什麼有它（2026-11-18）：通話要即時雙向傳音訊，SSE 只能單向，所以這條是 App 裡
 * 唯一的 WebSocket。走跟其他請求同一條 Cloudflare Tunnel＋Access，
 * 而 Tunnel 閒置會被掐斷（豆包拆解第 1 題），心跳是必要的不是保險。
 *
 * 所有回呼都丟回主執行緒依序處理：文字事件（asr_ended、tts_end）跟 0x11 語音的
 * 先後順序有意義，分兩條執行緒處理就可能先播到下一輪的聲音才知道換輪了。
 * 送出（[send]、[sendBytes]）哪條執行緒都能呼叫，OkHttp 的 send 本身是執行緒安全的。
 */
class CallClient(
    private val prefs: Prefs,
    private val mode: String,
    private val listener: Listener,
    /** 從哪條對話打的；null＝工具頁（見 [CallSession.convId]）。 */
    convId: String? = null,
) {
    interface Listener {
        fun onLink(link: Link, attempt: Int)
        fun onReady()
        fun onAsrEnded(turn: Long, text: String)
        fun onReplyDelta(turn: Long, text: String)
        fun onTtsEnd(turn: Long)
        fun onTts(packet: CallProtocol.TtsPacket)
        fun onImageAck()
        fun onServerError(message: String)
        /** 助理在這一輪挑的布丁狗動作（clip id），通話畫面演。 */
        fun onPetAct(turn: Long, name: String) {}
    }

    private val main = Handler(Looper.getMainLooper())

    // 升級成 WebSocket 之後 OkHttp 自己會把 read timeout 拿掉，斷線靠下面的心跳發現。
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .addInterceptor(AccessAuth.interceptor)
        .build()

    /** 整通電話共用一個 call_id，重連沿用（見 [CallSession]）。 */
    val session = CallSession(mode, convId)

    @Volatile private var ws: WebSocket? = null
    private var attempts = 0
    private var lastPong = 0L
    private var closed = false

    fun connect() {
        if (closed) return
        val req = Request.Builder()
            // OkHttp 的 url() 接受 http(s)://，會自己換成 ws(s)://
            .url("${prefs.baseUrl}/v1/call")
            .header("Authorization", "Bearer ${prefs.token}")
            .build()
        listener.onLink(if (attempts == 0) Link.CONNECTING else Link.RECONNECTING, attempts)
        ws = http.newWebSocket(req, Callbacks())
    }

    fun send(json: JSONObject): Boolean = ws?.send(json.toString()) ?: false

    fun sendBytes(bytes: ByteArray): Boolean = ws?.send(bytes.toByteString()) ?: false

    /** 掛斷：先告訴伺服器，再關。之後任何回呼都不理。 */
    fun hangup() {
        if (closed) return
        closed = true
        main.removeCallbacksAndMessages(null)
        ws?.let {
            it.send(JSONObject().put("type", "hangup").toString())
            it.close(1000, "hangup")
        }
        ws = null
    }

    private inner class Callbacks : WebSocketListener() {
        // 每條連線一份回呼；不是目前那條的回呼（舊連線遲到的事件）一律不理
        private fun mine(s: WebSocket) = s === ws && !closed

        override fun onOpen(webSocket: WebSocket, response: Response) {
            main.post {
                if (!mine(webSocket)) return@post
                lastPong = SystemClock.elapsedRealtime()
                webSocket.send(session.startMessage())
                scheduleHeartbeat(webSocket)
            }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            main.post {
                if (!mine(webSocket)) return@post
                val o = runCatching { JSONObject(text) }.getOrNull() ?: return@post
                val turn = o.optLong("turn", -1L)
                when (o.optString("type")) {
                    "ready" -> {
                        attempts = 0
                        listener.onReady()
                    }
                    "pong" -> lastPong = SystemClock.elapsedRealtime()
                    "asr_ended" -> listener.onAsrEnded(turn, o.optString("text"))
                    "reply_delta" -> listener.onReplyDelta(turn, o.optString("text"))
                    "tts_end" -> listener.onTtsEnd(turn)
                    "image_ack" -> listener.onImageAck()
                    "error" -> listener.onServerError(o.optString("message"))
                    "pet_act" -> listener.onPetAct(turn, o.optString("name"))
                }
            }
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            val pkt = CallProtocol.parseTts(bytes.toByteArray()) ?: return
            main.post { if (mine(webSocket)) listener.onTts(pkt) }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
            main.post { if (mine(webSocket)) dropped(webSocket, "伺服器關閉連線（$code）") }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.w(TAG, "通話連線失敗", t)
            val why = response?.let { "HTTP ${it.code}" } ?: (t.message ?: t.javaClass.simpleName)
            main.post { if (mine(webSocket)) dropped(webSocket, why) }
        }
    }

    /** 每 8 秒 ping 一次；16 秒沒收到 pong 就當這條死了，自己掐掉重連。 */
    private fun scheduleHeartbeat(s: WebSocket) {
        main.postDelayed({
            if (s !== ws || closed) return@postDelayed
            if (SystemClock.elapsedRealtime() - lastPong > PONG_TIMEOUT_MS) {
                s.cancel()
                dropped(s, "16 秒沒有回應")
                return@postDelayed
            }
            s.send(JSONObject().put("type", "ping").toString())
            scheduleHeartbeat(s)
        }, PING_INTERVAL_MS)
    }

    private fun dropped(s: WebSocket, why: String) {
        if (s !== ws || closed) return
        ws = null
        if (attempts >= MAX_RETRIES) {
            listener.onServerError("通話連線已中斷：$why")
            listener.onLink(Link.ENDED, attempts)
            return
        }
        attempts++
        listener.onLink(Link.RECONNECTING, attempts)
        listener.onServerError("連線中斷（$why），第 $attempts 次重連")
        main.postDelayed({ connect() }, minOf(attempts * 1000L, 5000L))
    }

    companion object {
        private const val TAG = "CallClient"
        const val PING_INTERVAL_MS = 8_000L
        const val PONG_TIMEOUT_MS = 16_000L
        const val MAX_RETRIES = 10
        /** 送出一張圖後等 image_ack 的上限；逾時就當作收到了，可以送下一張（協定補充，視訊那棒用）。 */
        const val IMAGE_ACK_TIMEOUT_MS = 5_000L
    }
}
