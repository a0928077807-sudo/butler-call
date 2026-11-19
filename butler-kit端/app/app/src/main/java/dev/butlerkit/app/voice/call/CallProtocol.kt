package dev.butlerkit.app.voice.call

/**
 * 通話協定 v1 裡「不碰 Android」的那一半：封包格式、輪次過濾、狀態判斷、錄音分包。
 *
 * 為什麼抽出來（2026-11-18）：使用者要在 App 裡跟 AI 打電話，辨識全搬到伺服器
 * （他說手機內建的語音辨識「非常爛根本不能用」）。手機與伺服器之間的格式一錯，
 * 現象只會是「AI 不講話」或「講到一半插進上一句」，在實機上很難看出是哪一邊的問題。
 * 這幾樣抽成純 Kotlin，電腦上的單元測試就能把格式釘死，不必等手機。
 *
 * 協定原文在 `說明.md` 的「WebSocket 協定 v1」，
 * 改這裡之前先改那份，伺服器（services\butler-call）要一起改。
 */
object CallProtocol {
    /** 手機→伺服器：PCM16LE、16 kHz、單聲道。 */
    const val TYPE_PCM: Byte = 0x01
    /** 手機→伺服器：一張 JPEG。 */
    const val TYPE_JPEG: Byte = 0x02
    /** 伺服器→手機：4 位元組大端輪次＋PCM16LE、24 kHz、單聲道。 */
    const val TYPE_TTS: Byte = 0x11

    const val MIC_RATE = 16_000
    const val TTS_RATE = 24_000
    /** 160 ms × 16 kHz × 2 位元組 = 5120。照豆包一包的長度（見豆包拆解第 4 題）。 */
    const val PCM_CHUNK_BYTES = 5120

    fun pcmPacket(pcm: ByteArray): ByteArray = prefixed(TYPE_PCM, pcm)

    fun jpegPacket(jpeg: ByteArray): ByteArray = prefixed(TYPE_JPEG, jpeg)

    private fun prefixed(type: Byte, body: ByteArray): ByteArray {
        val out = ByteArray(body.size + 1)
        out[0] = type
        System.arraycopy(body, 0, out, 1, body.size)
        return out
    }

    /** 一包 0x11：哪一輪的語音、語音本體。 */
    class TtsPacket(val turn: Long, val pcm: ByteArray)

    /**
     * 解析伺服器送來的二進位訊息。不是 0x11 或長度不夠就回 null（丟掉，不當錯誤）。
     * 輪次當無號 32 位元讀，所以回 Long。
     */
    fun parseTts(bytes: ByteArray): TtsPacket? {
        if (bytes.size < 5 || bytes[0] != TYPE_TTS) return null
        val turn = ((bytes[1].toLong() and 0xFF) shl 24) or
            ((bytes[2].toLong() and 0xFF) shl 16) or
            ((bytes[3].toLong() and 0xFF) shl 8) or
            (bytes[4].toLong() and 0xFF)
        return TtsPacket(turn, bytes.copyOfRange(5, bytes.size))
    }
}

/**
 * 被打斷的那一輪，之後還在路上的語音、文字一律丟掉。
 *
 * 為什麼要有：按打斷時伺服器已經送出去的封包還在網路上，手機清掉播放緩衝之後
 * 它們才陸續到，不擋的話剛打斷的話會「再冒出一句」。豆包用 isManualBreak 擋，
 * 這裡用伺服器給的輪次編號擋（伺服器打斷時把輪次 +1）。
 *
 * 每次重新連上（收到 ready）要 [reset]：新連線伺服器的輪次可能從頭算，
 * 留著舊的上限會把新連線的每一句都當舊的丟掉。
 */
class TurnFilter {
    /** 最近一次 asr_ended 的輪次，-1 表示這條連線還沒有。 */
    var current: Long = -1L
        private set
    /** 小於等於這個的輪次全部丟掉（被打斷過的）。 */
    private var discardThrough: Long = -1L

    fun reset() {
        current = -1L
        discardThrough = -1L
    }

    /** asr_ended 到了。被打斷過的舊輪次又冒出 asr_ended 就回 false（不理它）。 */
    fun onAsrEnded(turn: Long): Boolean {
        if (turn <= discardThrough) return false
        if (turn > current) current = turn
        return true
    }

    /** 使用者按了打斷：目前這一輪（含之前）之後到的封包都不要。 */
    fun onInterrupt() {
        if (current > discardThrough) discardThrough = current
    }

    /** reply_delta、tts_end、0x11 語音：只收目前這一輪（或更新的），被打斷的不收。 */
    fun accept(turn: Long): Boolean = turn > discardThrough && turn >= current
}

/** 畫面上的狀態。 */
enum class CallState(val label: String) {
    CONNECTING("連線中"),
    RECONNECTING("斷線重連中"),
    LISTENING("聆聽中"),
    THINKING("思考中"),
    SPEAKING("說話中"),
    ENDED("通話已結束"),
}

/** 連線本身在哪個階段（跟對話三態分開，對話三態只在 READY 時才有意義）。 */
enum class Link { CONNECTING, RECONNECTING, READY, ENDED }

/**
 * 照豆包的判斷式：`isPlaying ? SPEAKING : asrEnded ? THINK : LISTENING`
 * （豆包拆解第 5 題，讀碼確認）。只用兩個布林算，畫面就不會跟實際的音訊狀態對不上。
 */
fun deriveState(link: Link, playing: Boolean, asrEnded: Boolean): CallState = when (link) {
    Link.CONNECTING -> CallState.CONNECTING
    Link.RECONNECTING -> CallState.RECONNECTING
    Link.ENDED -> CallState.ENDED
    Link.READY -> when {
        playing -> CallState.SPEAKING
        asrEnded -> CallState.THINKING
        else -> CallState.LISTENING
    }
}

/**
 * 把 AudioRecord 每次讀到的不定長度資料切成剛好 [chunk] 位元組一包。
 * AudioRecord.read 不保證每次都讀滿，而伺服器那邊照 160 ms 一包在算時間。
 */
class PcmChunker(private val chunk: Int = CallProtocol.PCM_CHUNK_BYTES) {
    private val buf = ByteArray(chunk)
    private var filled = 0

    /** 餵進一段，回傳這次湊滿的每一包（可能 0 包或多包）。 */
    fun feed(data: ByteArray, len: Int = data.size): List<ByteArray> {
        val out = ArrayList<ByteArray>(1)
        var off = 0
        while (off < len) {
            val n = minOf(chunk - filled, len - off)
            System.arraycopy(data, off, buf, filled, n)
            filled += n
            off += n
            if (filled == chunk) {
                out += buf.copyOf()
                filled = 0
            }
        }
        return out
    }

    /** 不在送音的時候把湊到一半的丟掉，免得下一次送出去的第一包混著舊聲音。 */
    fun clear() {
        filled = 0
    }
}

/**
 * 一通電話的身分：call_id 在撥出時產生一次，之後斷線重連都沿用同一個。
 *
 * 為什麼要有（協定補充 01:55）：Tunnel 偶爾會斷，斷了重連時伺服器靠 call_id 把這通電話
 * 10 分鐘內的對話歷史接回來；每次重連換新 id 的話，AI 會忘了剛剛在聊什麼。
 * 抽成不碰 Android 的類別，單元測試才驗得到「重連送出的 start 跟第一次是同一個 id」。
 */
class CallSession(
    val mode: String,
    /**
     * 從哪條對話打的（2026-11-18 布丁：在哪條對話打，就由那條對話的助理接）。
     * null＝從工具頁打，伺服器接助理那條（main）。
     */
    val convId: String? = null,
    val callId: String = java.util.UUID.randomUUID().toString(),
) {
    /** 每次連上（含重連）送的第一則訊息。 */
    fun startMessage(): String = org.json.JSONObject()
        .put("type", "start")
        .put("mode", mode)
        .put("call_id", callId)
        .apply { if (!convId.isNullOrBlank()) put("conv_id", convId) }
        .toString()
}
