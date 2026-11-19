package dev.butlerkit.app.voice.call

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.SystemClock
import android.util.Log
import kotlin.math.log10
import kotlin.math.sqrt

/**
 * 通話期間把聲音切到「通話模式＋內建喇叭」，掛斷時還原。
 *
 * 為什麼（2026-11-18）：通話模式才會啟用系統的回音消除，AI 的聲音不會被麥克風收回去；
 * 但通話模式預設走聽筒，手機架在桌上時根本聽不到，所以要再指定外放。
 * API 31 起 isSpeakerphoneOn 已不建議用，改用 setCommunicationDevice。
 */
class SpeakerRoute(context: Context) {
    private val am = context.getSystemService(AudioManager::class.java)
    private var on = false
    private var oldMode = AudioManager.MODE_NORMAL
    private var oldSpeaker = false

    fun enable() {
        if (on) return
        on = true
        oldMode = am.mode
        am.mode = AudioManager.MODE_IN_COMMUNICATION
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            am.availableCommunicationDevices
                .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
                ?.let { am.setCommunicationDevice(it) }
        } else {
            @Suppress("DEPRECATION")
            oldSpeaker = am.isSpeakerphoneOn
            @Suppress("DEPRECATION")
            am.isSpeakerphoneOn = true
        }
    }

    fun restore() {
        if (!on) return
        on = false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            am.clearCommunicationDevice()
        } else {
            @Suppress("DEPRECATION")
            am.isSpeakerphoneOn = oldSpeaker
        }
        am.mode = oldMode
    }
}

/**
 * 麥克風：16 kHz、PCM16、單聲道，音源 VOICE_COMMUNICATION（會套系統的回音消除與降噪）。
 *
 * 一條執行緒每次讀 [READ_BYTES]（80 ms），讀到的原樣丟給 [onPcm]（在錄音執行緒上呼叫，
 * 分包與要不要送由呼叫端決定）；音量每 100 ms 給 [onLevel] 一次（0～1）。
 */
class MicRecorder(
    private val onPcm: (ByteArray, Int) -> Unit,
    private val onLevel: (Float) -> Unit,
) {
    @Volatile private var running = false
    private var rec: AudioRecord? = null
    private var thread: Thread? = null

    /** 呼叫前要先有 RECORD_AUDIO 權限。開不起來回 false。 */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (running) return true
        val minBuf = AudioRecord.getMinBufferSize(
            CallProtocol.MIC_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuf <= 0) return false
        val r = runCatching {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                CallProtocol.MIC_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, READ_BYTES * 4),
            )
        }.getOrNull() ?: return false
        if (r.state != AudioRecord.STATE_INITIALIZED) {
            r.release()
            return false
        }
        r.startRecording()
        rec = r
        running = true
        thread = Thread({ loop(r) }, "MicRecorder").apply { isDaemon = true; start() }
        return true
    }

    fun stop() {
        running = false
        thread?.join(500)
        thread = null
        rec?.let {
            runCatching { it.stop() }
            it.release()
        }
        rec = null
    }

    private fun loop(r: AudioRecord) {
        val buf = ByteArray(READ_BYTES)
        var sumSq = 0.0
        var samples = 0
        var lastLevelAt = SystemClock.elapsedRealtime()
        while (running) {
            val n = r.read(buf, 0, buf.size)
            if (n < 0) {
                Log.w(TAG, "AudioRecord.read 錯誤 $n")
                break
            }
            if (n == 0) continue
            onPcm(buf, n)
            var i = 0
            while (i + 1 < n) {
                val s = ((buf[i + 1].toInt() shl 8) or (buf[i].toInt() and 0xFF)).toShort().toInt()
                sumSq += s.toDouble() * s
                samples++
                i += 2
            }
            val now = SystemClock.elapsedRealtime()
            if (now - lastLevelAt >= LEVEL_INTERVAL_MS && samples > 0) {
                onLevel(levelOf(sqrt(sumSq / samples)))
                sumSq = 0.0
                samples = 0
                lastLevelAt = now
            }
        }
    }

    companion object {
        private const val TAG = "MicRecorder"
        /** 80 ms × 16 kHz × 2 位元組。 */
        const val READ_BYTES = 2560
        const val LEVEL_INTERVAL_MS = 100L

        /** RMS（PCM16 振幅）→ dBFS，-60～0 dB 線性對到 0～1。 */
        fun levelOf(rms: Double): Float {
            if (rms <= 0.0) return 0f
            val db = 20 * log10(rms / 32768.0)
            return ((db + 60) / 60).coerceIn(0.0, 1.0).toFloat()
        }
    }
}
