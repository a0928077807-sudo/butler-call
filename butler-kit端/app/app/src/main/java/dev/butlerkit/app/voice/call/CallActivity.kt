package dev.butlerkit.app.voice.call

import android.Manifest
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.media.AudioManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import dev.butlerkit.app.ui.ButlerTheme

/**
 * 跟 AI 打電話的獨立畫面（2026-11-18，使用者要「直接跟你講話、你用 AI 語音回我」）。
 *
 * 照 TalkActivity 的寫法：獨立 Activity、亮著螢幕、全螢幕。多一件只有 Activity 做得到的事：
 * 音量鍵調通話音量（[AudioManager.STREAM_VOICE_CALL]），不然按音量鍵調的是媒體音量，
 * AI 的聲音大小完全不變。
 *
 * extra [EXTRA_VIDEO]：true 是視訊通話（鏡頭預覽＋抽圖給 AI 看），false 是語音通話。
 * 視訊模式一次要麥克風＋相機兩個權限；螢幕方向在 configChanges 時更新給相機，
 * 抽出來的圖才會是正的（Manifest 宣告了 orientation|screenSize，轉向不會重建 Activity）。
 */
class CallActivity : ComponentActivity() {

    private var micGranted by mutableStateOf(false)
    private var camGranted by mutableStateOf(false)
    /** 螢幕方向（Surface.ROTATION_*），給相機算影像要轉幾度。 */
    private var rotation by mutableIntStateOf(0)

    private val askMic = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted -> micGranted = granted }

    private val askMicAndCam = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { result ->
        micGranted = result[Manifest.permission.RECORD_AUDIO] ?: micGranted
        camGranted = result[Manifest.permission.CAMERA] ?: camGranted
    }

    private fun has(permission: String) =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun currentRotation(): Int = ContextCompat.getDisplayOrDefault(this).rotation

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 對話裡正在播的樣本或影片先停，不然會跟通話的語音疊在一起
        dev.butlerkit.app.ui.pauseChatMedia()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        volumeControlStream = AudioManager.STREAM_VOICE_CALL
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )

        val video = intent.getBooleanExtra(EXTRA_VIDEO, false)
        val convId = intent.getStringExtra(EXTRA_CONV_ID)
        micGranted = has(Manifest.permission.RECORD_AUDIO)
        if (!video) {
            if (!micGranted) askMic.launch(Manifest.permission.RECORD_AUDIO)
        } else {
            camGranted = has(Manifest.permission.CAMERA)
            rotation = currentRotation()
            val missing = listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.CAMERA)
                .filterNot(::has)
            if (missing.isNotEmpty()) askMicAndCam.launch(missing.toTypedArray())
        }

        setContent {
            // 通話一直亮著螢幕：固定星空，不跑會動的場景（同面對面翻譯）
            ButlerTheme(scenes = false) {
                if (video) VideoCallScreen(
                    micGranted = micGranted,
                    camGranted = camGranted,
                    rotation = rotation,
                    convId = convId,
                    onClose = { finish() },
                )
                else CallScreen(micGranted = micGranted, convId = convId, onClose = { finish() })
            }
        }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        rotation = currentRotation()
    }

    companion object {
        const val EXTRA_VIDEO = "video"

        /** 從哪條對話打的：電話由那條對話的助理接（2026-11-18）。沒帶＝工具頁，接助理那條。 */
        const val EXTRA_CONV_ID = "conv_id"
    }
}
