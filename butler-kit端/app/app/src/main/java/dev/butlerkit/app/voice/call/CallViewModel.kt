package dev.butlerkit.app.voice.call

import android.app.Application
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import androidx.camera.core.ImageProxy
import androidx.lifecycle.AndroidViewModel
import dev.butlerkit.app.data.Prefs
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONObject

/** 通話畫面要的全部狀態。對話三態一律由 [state] 從兩個布林算出來，不另存。 */
data class CallUiState(
    val link: Link = Link.CONNECTING,
    val playing: Boolean = false,
    val asrEnded: Boolean = false,
    val muted: Boolean = false,
    val youText: String = "",
    val replyText: String = "",
    /** 麥克風音量 0～1。 */
    val level: Float = 0f,
    val error: String? = null,
    /** 助理挑的布丁狗動作；[petActSeq] 每來一個加一，同一段連挑兩次也會再演。 */
    val petAct: String = "",
    val petActSeq: Int = 0,
) {
    val state: CallState get() = deriveState(link, playing, asrEnded)
}

/**
 * 一通語音電話：把 [CallClient]（網路）、[MicRecorder]（收音）、[TtsPlayer]（播放）、
 * [SpeakerRoute]（外放）接起來，照協定 v1 的流程走。
 *
 * 為什麼放在 ViewModel（2026-11-18）：轉螢幕、深淺色切換都不該掛掉電話；
 * 畫面關掉（Activity 結束）時 [onCleared] 一定會跑，掛斷與還原外放放這裡就不會漏。
 *
 * 執行緒：CallClient 的回呼都在主執行緒；錄音回呼在錄音執行緒，只讀 [_state] 決定
 * 要不要送、只動自己的 [chunker]。
 */
class CallViewModel(app: Application) : AndroidViewModel(app), CallClient.Listener {

    private val prefs = Prefs(app)
    private val _state = MutableStateFlow(CallUiState())
    val state: StateFlow<CallUiState> = _state.asStateFlow()

    private val main = Handler(Looper.getMainLooper())
    private val filter = TurnFilter()
    private val route = SpeakerRoute(app)
    private var player: TtsPlayer? = null
    private var client: CallClient? = null
    private var mic: MicRecorder? = null
    /** 只在錄音執行緒上用。 */
    private val chunker = PcmChunker()
    /** 視訊模式：這一幀要不要送（見 [FrameGate]）。 */
    private val frameGate = FrameGate()
    private var started = false
    private var hungUp = false

    /** 等播完要回報的輪次；-1 表示沒在等。 */
    private var pendingDoneTurn = -1L
    private val donePoll = object : Runnable {
        override fun run() {
            val p = player ?: return
            if (pendingDoneTurn < 0) return
            if (p.finished()) {
                val turn = pendingDoneTurn
                pendingDoneTurn = -1L
                client?.send(JSONObject().put("type", "playback_done").put("turn", turn))
                _state.update { it.copy(playing = false, asrEnded = false) }
            } else {
                main.postDelayed(this, DONE_POLL_MS)
            }
        }
    }

    /** 撥出。[mode] 是協定的 "voice" 或 "video"；[convId] 是從哪條對話打的（null＝工具頁）。重複呼叫無效。 */
    fun start(mode: String = "voice", convId: String? = null) {
        if (started || hungUp) return
        started = true
        if (!prefs.isConfigured()) {
            _state.update { it.copy(link = Link.ENDED, error = "還沒設定連線，先到設定頁填主機與權杖。") }
            return
        }
        route.enable()
        player = TtsPlayer()
        client = CallClient(prefs, mode, this, convId).also { it.connect() }
    }

    /** 拿到麥克風權限之後呼叫（可能比 [start] 晚）。 */
    fun startMic() {
        if (hungUp || mic != null) return
        val m = MicRecorder(onPcm = ::onMicPcm, onLevel = { lv -> _state.update { it.copy(level = lv) } })
        if (m.start()) mic = m
        else _state.update { it.copy(error = "麥克風開不起來，可能被別的 App 占用。") }
    }

    /** 錄音執行緒上跑：能送就切包送，不能送就把湊到一半的丟掉。 */
    private fun onMicPcm(buf: ByteArray, len: Int) {
        val s = _state.value
        val canSend = s.link == Link.READY && !s.playing && !s.asrEnded && !s.muted
        if (!canSend) {
            chunker.clear()
            return
        }
        val c = client ?: return
        for (pkt in chunker.feed(buf, len)) c.sendBytes(CallProtocol.pcmPacket(pkt))
    }

    /**
     * 相機分析執行緒上跑：條件全成立才轉 JPEG 送出，否則直接關掉影像（不轉檔，省電）。
     * 一定要 close，不關的話 CameraX 不會再給下一幀，畫面分析就停了。
     */
    fun onFrame(image: ImageProxy) {
        image.use {
            val s = _state.value
            val now = SystemClock.elapsedRealtime()
            if (!frameGate.shouldSend(s.state, s.muted, s.link, now)) return
            val c = client ?: return
            val jpeg = runCatching { encodeFrame(image) }
                .onFailure { Log.w(TAG, "轉 JPEG 失敗", it) }
                .getOrNull() ?: return
            if (c.sendBytes(CallProtocol.jpegPacket(jpeg))) frameGate.onSent(now)
        }
    }

    /** 打斷 AI（思考中或說話中才有意義）。 */
    fun interrupt() {
        val st = _state.value.state
        if (st != CallState.THINKING && st != CallState.SPEAKING) return
        client?.send(JSONObject().put("type", "interrupt"))
        filter.onInterrupt()
        stopPlayback()
    }

    fun toggleMute() {
        val on = !_state.value.muted
        client?.send(JSONObject().put("type", "mute").put("on", on))
        _state.update { it.copy(muted = on) }
    }

    fun hangup() {
        if (hungUp) return
        hungUp = true
        main.removeCallbacks(donePoll)
        pendingDoneTurn = -1L
        client?.hangup()
        client = null
        mic?.stop()
        mic = null
        player?.release()
        player = null
        route.restore()
        _state.update { it.copy(link = Link.ENDED, playing = false, asrEnded = false, level = 0f) }
    }

    override fun onCleared() {
        hangup()
    }

    /** 打斷播放器並回到聆聽中（打斷鈕、重新連上都用）。 */
    private fun stopPlayback() {
        main.removeCallbacks(donePoll)
        pendingDoneTurn = -1L
        player?.interrupt()
        _state.update { it.copy(playing = false, asrEnded = false) }
    }

    // ── CallClient.Listener（主執行緒） ──────────────────────────────

    override fun onLink(link: Link, attempt: Int) {
        if (hungUp) return
        _state.update { it.copy(link = link) }
    }

    override fun onReady() {
        // 新連線伺服器的輪次從 0 算，過濾要歸零；上一條連線沒播完的也不要了
        filter.reset()
        frameGate.reset()
        stopPlayback()
        _state.update { it.copy(link = Link.READY, error = null) }
    }

    override fun onAsrEnded(turn: Long, text: String) {
        if (!filter.onAsrEnded(turn)) return
        _state.update { it.copy(asrEnded = true, youText = text, replyText = "") }
    }

    override fun onReplyDelta(turn: Long, text: String) {
        if (!filter.accept(turn)) return
        _state.update { it.copy(replyText = it.replyText + text) }
    }

    override fun onPetAct(turn: Long, name: String) {
        if (!filter.accept(turn) || name.isBlank()) return
        _state.update { it.copy(petAct = name, petActSeq = it.petActSeq + 1) }
    }

    override fun onTtsEnd(turn: Long) {
        if (!filter.accept(turn)) return
        val p = player ?: return
        // 空辨識那輪也照走：沒有語音，只補 250 ms 靜音，播完一樣送 playback_done
        p.markEnd()
        pendingDoneTurn = turn
        main.removeCallbacks(donePoll)
        main.postDelayed(donePoll, DONE_POLL_MS)
    }

    override fun onTts(packet: CallProtocol.TtsPacket) {
        if (!filter.accept(packet.turn)) return
        val p = player ?: return
        if (!_state.value.playing) _state.update { it.copy(playing = true) }
        p.enqueue(packet.pcm)
    }

    override fun onImageAck() {
        frameGate.onAck()
    }

    override fun onServerError(message: String) {
        _state.update { it.copy(error = message) }
    }

    companion object {
        const val DONE_POLL_MS = 100L
        private const val TAG = "CallViewModel"
    }
}
