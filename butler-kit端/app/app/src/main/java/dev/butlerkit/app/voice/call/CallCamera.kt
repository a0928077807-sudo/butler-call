package dev.butlerkit.app.voice.call

import android.graphics.Bitmap
import android.graphics.Matrix
import android.util.Log
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

/**
 * 視訊通話的鏡頭：全螢幕預覽＋ImageAnalysis 抽圖（2026-11-18，通話第三棒）。
 *
 * 為什麼這樣接：使用者要把手機架起來、讓 AI 邊看邊教他動手。預覽給人看，
 * 抽圖給 AI 看，兩個 use case 綁在同一個 lifecycle 上，畫面關掉 CameraX 自己收。
 * 要不要送哪一幀由 [onFrame] 的接收端（[CallViewModel.onFrame]）判斷，這裡只負責把畫面拿到。
 *
 * - 預覽用 COMPATIBLE（TextureView）：底下有 ButlerTheme 的星空背景，SurfaceView
 *   要在視窗上挖洞，跟 Compose 疊起來容易整片黑或被蓋住；TextureView 就是一般的 View。
 * - [rotation]：螢幕方向（Surface.ROTATION_*），Activity 在 configChanges 時更新。
 *   影像本身的 rotationDegrees 會跟著它算，[encodeFrame] 再照它轉正。
 */
@Composable
fun CameraPreview(
    lensFacing: Int,
    rotation: Int,
    onFrame: (ImageProxy) -> Unit,
    onError: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val frameSink by rememberUpdatedState(onFrame)
    val errorSink by rememberUpdatedState(onError)
    val previewView = remember {
        PreviewView(context).apply {
            implementationMode = PreviewView.ImplementationMode.COMPATIBLE
            scaleType = PreviewView.ScaleType.FILL_CENTER
        }
    }
    // 分析在自己的單一執行緒上跑：轉 JPEG 不能卡主執行緒，單執行緒也保證幀是一張一張進來
    val analysisExecutor = remember { Executors.newSingleThreadExecutor() }

    val preview = remember { Preview.Builder().build() }
    val analysis = remember {
        ImageAnalysis.Builder()
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            // 預設是 640×480，長邊不到 768；要 1024×768 左右才夠縮
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY)
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            Size(1024, 768),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                        ),
                    )
                    .build(),
            )
            .build()
            .also { a -> a.setAnalyzer(analysisExecutor) { img -> frameSink(img) } }
    }
    DisposableEffect(Unit) {
        onDispose {
            analysis.clearAnalyzer()
            analysisExecutor.shutdown()
        }
    }

    var provider by remember { mutableStateOf<ProcessCameraProvider?>(null) }
    LaunchedEffect(Unit) {
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            provider = runCatching { future.get() }
                .onFailure { errorSink("相機開不起來：${it.message}") }
                .getOrNull()
        }, ContextCompat.getMainExecutor(context))
    }

    LaunchedEffect(rotation) {
        preview.targetRotation = rotation
        analysis.targetRotation = rotation
    }

    DisposableEffect(provider, lensFacing) {
        val p = provider
        if (p != null) {
            val selector = CameraSelector.Builder().requireLensFacing(lensFacing).build()
            runCatching {
                if (!p.hasCamera(selector)) error("這支手機沒有這顆鏡頭")
                p.unbindAll()
                preview.setSurfaceProvider(previewView.surfaceProvider)
                p.bindToLifecycle(lifecycleOwner, selector, preview, analysis)
            }.onFailure {
                Log.w(TAG, "綁定相機失敗", it)
                errorSink("相機開不起來：${it.message}")
            }
        }
        onDispose { p?.unbindAll() }
    }

    AndroidView(factory = { previewView }, modifier = modifier)
}

/**
 * 一幀轉成要送的 JPEG：轉正（rotationDegrees）、長邊縮到 [FRAME_MAX_EDGE]、品質 [FRAME_JPEG_QUALITY]。
 * 不關 [image]，由呼叫端關（它還要在不送的時候關）。
 */
fun encodeFrame(image: ImageProxy): ByteArray {
    val src = image.toBitmap()
    val (w, h) = scaledSize(src.width, src.height)
    val m = Matrix().apply {
        postScale(w.toFloat() / src.width, h.toFloat() / src.height)
        postRotate(image.imageInfo.rotationDegrees.toFloat())
    }
    val out = Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true)
    return try {
        ByteArrayOutputStream().use { bos ->
            out.compress(Bitmap.CompressFormat.JPEG, FRAME_JPEG_QUALITY, bos)
            bos.toByteArray()
        }
    } finally {
        if (out !== src) out.recycle()
        src.recycle()
    }
}

private const val TAG = "CallCamera"
