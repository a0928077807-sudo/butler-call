package dev.butlerkit.app.voice.call

/**
 * 視訊通話「這一幀要不要送」的判斷，不碰 Android，單元測試釘得住。
 *
 * 為什麼有它（2026-11-18，通話第三棒）：鏡頭每秒吐 30 幀，全部轉 JPEG 送出去
 * 手機會燙、網路會塞、伺服器也看不完。協定 v1 規定圖只在聆聽中送、
 * 送一張等 image_ack（5 秒沒來當收到）、兩張至少隔 1 秒。條件不成立時
 * analyzer 連轉檔都不做直接丟掉，耗電才壓得下來，所以判斷要在轉檔之前、而且要便宜。
 *
 * 執行緒：[shouldSend]、[onSent] 在相機分析執行緒；[onAck]、[reset] 在主執行緒。
 * 全部 synchronized，量很小不怕搶。時間一律傳進來（elapsedRealtime），測試才不用等真的秒數。
 */
class FrameGate(
    private val ackTimeoutMs: Long = CallClient.IMAGE_ACK_TIMEOUT_MS,
    private val minIntervalMs: Long = MIN_INTERVAL_MS,
) {
    /** 上一張送出的時間；Long.MIN_VALUE 表示這條連線還沒送過。 */
    private var lastSentAt = NEVER
    private var awaitingAck = false

    @Synchronized
    fun shouldSend(state: CallState, muted: Boolean, link: Link, now: Long): Boolean {
        if (state != CallState.LISTENING || muted || link != Link.READY) return false
        if (lastSentAt == NEVER) return true
        val since = now - lastSentAt
        if (awaitingAck && since < ackTimeoutMs) return false
        return since >= minIntervalMs
    }

    /** 一張圖真的送出去了。 */
    @Synchronized
    fun onSent(now: Long) {
        lastSentAt = now
        awaitingAck = true
    }

    /** 伺服器回了 image_ack，可以送下一張（間隔還是要滿 1 秒）。 */
    @Synchronized
    fun onAck() {
        awaitingAck = false
    }

    /** 重新連上：舊連線的 ack 不會來了，從頭算。 */
    @Synchronized
    fun reset() {
        lastSentAt = NEVER
        awaitingAck = false
    }

    companion object {
        const val MIN_INTERVAL_MS = 1_000L
        private const val NEVER = Long.MIN_VALUE
    }
}

/** 縮圖後的寬高：長邊超過 [maxEdge] 才等比縮到 [maxEdge]，小的不放大。 */
fun scaledSize(width: Int, height: Int, maxEdge: Int = FRAME_MAX_EDGE): Pair<Int, Int> {
    val long = maxOf(width, height)
    if (long <= maxEdge) return width to height
    val k = maxEdge.toDouble() / long
    return maxOf(1, Math.round(width * k).toInt()) to maxOf(1, Math.round(height * k).toInt())
}

/** 送給 AI 的圖長邊上限（通話第三棒規格定的）；再大只是多花流量與看圖的 token。 */
const val FRAME_MAX_EDGE = 768
/** JPEG 品質（同上，規格定的；實機看不清楚再調）。 */
const val FRAME_JPEG_QUALITY = 60
