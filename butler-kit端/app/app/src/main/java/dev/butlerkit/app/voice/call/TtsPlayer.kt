package dev.butlerkit.app.voice.call

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.SystemClock

/**
 * 播伺服器送來的 0x11 語音（PCM16、24 kHz、單聲道），能隨時打斷、能判斷「真的播完了」。
 *
 * 為什麼自己寫而不用 MediaPlayer（2026-11-18）：語音是一段一段串流進來的，邊收邊播；
 * 打斷要在幾十毫秒內停住；而「播完」要等喇叭真的唸完最後一個字才算——太早送
 * playback_done，手機就開始收音，會把自己喇叭還在講的尾巴當成使用者講話送上去。
 *
 * 設計（第一棒定的，照做）：
 *  - 一條寫入執行緒從佇列取資料，每次只寫 [WRITE_BYTES]（40 ms）：AudioTrack.write 會卡住，
 *    切小塊才能每塊之間檢查「這一代被打斷了沒」，打斷後最多多出 40 ms 舊聲音。
 *  - 打斷：鎖內 generation +1、清佇列、pause、flush、已寫歸零、記下播放位置當基準、play。
 *  - 播完：tts_end 時排一個 END 標記；寫入執行緒讀到就記 endMark＝已寫的幀數，再補 250 ms 靜音
 *    （有些機型緩衝沒滿不開始播，最後一小段會卡在緩衝裡出不來，靜音把它推出去）。
 *    播放位置（減基準）追上 endMark 就算播完；位置 1.5 秒沒動也算（防卡死永遠在「說話中」）。
 */
class TtsPlayer {

    private sealed class Item {
        class Pcm(val bytes: ByteArray) : Item()
        class End(val seq: Long) : Item()
    }

    private val lock = Object()
    private val queue = ArrayDeque<Item>()

    // 以下全部在 lock 內讀寫
    private var gen = 0L
    /** 這一代（上次打斷之後）寫進 AudioTrack 的幀數，含補的靜音。 */
    private var framesWritten = 0L
    /** 上次打斷時的播放位置；播放位置要減掉它才跟 framesWritten 對得上。 */
    private var basePos = 0L
    /** 最後一個真正語音幀的位置；-1 表示還沒讀到 END。 */
    private var endMark = -1L
    /** 目前等的是第幾個 END。播完（或打斷）就換號，遲到的舊 END 不會設到下一輪的 endMark。 */
    private var endSeq = 0L
    private var endRequested = false
    private var lastPlayed = -1L
    private var lastMoveAt = 0L
    private var released = false

    private val track: AudioTrack = run {
        val format = AudioFormat.Builder()
            .setSampleRate(CallProtocol.TTS_RATE)
            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .build()
        val minBuf = AudioTrack.getMinBufferSize(
            CallProtocol.TTS_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    // 走通話那一路：音量鍵調的是通話音量，系統的回音消除也知道這是對方的聲音
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(format)
            .setTransferMode(AudioTrack.MODE_STREAM)
            // 緩衝不要開大：越大，打斷時 flush 前已經送進去的就越多，開始播的門檻也越高
            .setBufferSizeInBytes(maxOf(minBuf, WRITE_BYTES * 4))
            .build()
    }

    private val writer = Thread(::writeLoop, "TtsPlayer").apply { isDaemon = true }

    init {
        track.play()
        writer.start()
    }

    /** 收到一包 0x11（已過輪次過濾）。 */
    fun enqueue(pcm: ByteArray) {
        // PCM16 一幀 2 位元組，奇數長度的最後一個位元組寫不進去
        val even = if (pcm.size % 2 == 0) pcm else pcm.copyOf(pcm.size - 1)
        if (even.isEmpty()) return
        synchronized(lock) {
            if (released) return
            queue.addLast(Item.Pcm(even))
            lock.notifyAll()
        }
    }

    /** 收到 tts_end：這一輪不會再有語音了。之後用 [finished] 查有沒有播完。 */
    fun markEnd() {
        synchronized(lock) {
            if (released) return
            endSeq++
            endMark = -1L
            endRequested = true
            lastPlayed = -1L
            queue.addLast(Item.End(endSeq))
            lock.notifyAll()
        }
    }

    /** 立刻停、丟掉所有還沒播的。 */
    fun interrupt() {
        synchronized(lock) {
            if (released) return
            gen++
            endSeq++
            queue.clear()
            endMark = -1L
            endRequested = false
            lastPlayed = -1L
            // flush 只有在 pause／stop 狀態下才有效
            track.pause()
            track.flush()
            framesWritten = 0L
            basePos = head()
            track.play()
        }
    }

    /**
     * [markEnd] 之後每 100 ms 呼叫一次。回 true 表示這一輪播完了（只會回一次 true，
     * 之後要等下一次 [markEnd]）。
     */
    fun finished(now: Long = SystemClock.elapsedRealtime()): Boolean {
        synchronized(lock) {
            if (!endRequested || released) return false
            // playbackHeadPosition 是 int 但語意是無號 32 位元，會繞回；減基準也照無號算
            val played = (head() - basePos) and 0xFFFFFFFFL
            val done = when {
                endMark >= 0 && played >= endMark -> true
                played != lastPlayed -> {
                    lastPlayed = played
                    lastMoveAt = now
                    false
                }
                else -> now - lastMoveAt >= STALL_MS
            }
            if (done) {
                endRequested = false
                endMark = -1L
                endSeq++ // 位置卡住提早判完的話，寫入執行緒之後才讀到的那個 END 作廢
            }
            return done
        }
    }

    fun release() {
        synchronized(lock) {
            if (released) return
            released = true
            gen++
            queue.clear()
            lock.notifyAll()
        }
        runCatching {
            track.pause()
            track.flush()
        }
        writer.join(500)
        track.release()
    }

    private fun head(): Long = track.playbackHeadPosition.toLong() and 0xFFFFFFFFL

    private fun writeLoop() {
        while (true) {
            val item: Item
            val myGen: Long
            synchronized(lock) {
                while (queue.isEmpty() && !released) lock.wait()
                if (released) return
                item = queue.removeFirst()
                myGen = gen
            }
            when (item) {
                is Item.Pcm -> writeChunks(item.bytes, myGen)
                is Item.End -> {
                    var mine: Boolean
                    synchronized(lock) {
                        mine = myGen == gen && item.seq == endSeq
                        if (mine) endMark = framesWritten
                    }
                    if (mine) writeChunks(SILENCE, myGen)
                }
            }
        }
    }

    /** 切成 [WRITE_BYTES] 一塊寫，每塊前看這一代被打斷了沒。 */
    private fun writeChunks(data: ByteArray, myGen: Long) {
        var off = 0
        while (off < data.size) {
            synchronized(lock) { if (myGen != gen || released) return }
            val n = minOf(WRITE_BYTES, data.size - off)
            val w = track.write(data, off, n)
            if (w <= 0) return // 錯誤碼（例如已釋放），這段放棄
            synchronized(lock) { if (myGen == gen) framesWritten += w / 2 }
            off += w
        }
    }

    companion object {
        /** 40 ms × 24 kHz × 2 位元組。 */
        const val WRITE_BYTES = 1920
        const val STALL_MS = 1_500L
        /** 250 ms 靜音：24000 × 0.25 × 2。 */
        private val SILENCE = ByteArray(CallProtocol.TTS_RATE / 4 * 2)
    }
}
