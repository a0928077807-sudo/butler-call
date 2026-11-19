package dev.butlerkit.app.voice.call

import androidx.camera.core.CameraSelector
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import dev.butlerkit.app.ui.Palette
import dev.butlerkit.app.ui.Radii
import dev.butlerkit.app.ui.Space
import dev.butlerkit.app.ui.Type

/**
 * 語音通話畫面：上面大字狀態、音量條、布丁狗（2026-11-18 加）、字幕（你說的、我回的）、底下三顆鈕。
 *
 * 狀態字直接用 [CallUiState.state]（兩個布林算出來的），畫面不另外記狀態，
 * 就不會出現「畫面寫聆聽中、其實還在播」這種對不上。
 */
@Composable
fun CallScreen(micGranted: Boolean, convId: String?, onClose: () -> Unit) {
    val vm: CallViewModel = viewModel()
    val s by vm.state.collectAsState()

    LaunchedEffect(Unit) { vm.start(convId = convId) }
    LaunchedEffect(micGranted) { if (micGranted) vm.startMic() }

    Column(
        Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding()
            .padding(Space.Screen),
    ) {
        Text("語音通話", color = Palette.TextDim, fontSize = Type.Meta)
        Spacer(Modifier.height(24.dp))
        Text(
            s.state.label,
            color = when (s.state) {
                CallState.SPEAKING -> Palette.Accent
                CallState.RECONNECTING, CallState.ENDED -> Palette.Warn
                else -> Palette.Text
            },
            fontSize = 34.sp,
            modifier = Modifier.fillMaxWidth(),
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(16.dp))
        LevelBar(if (s.muted) 0f else s.level)
        if (s.muted) {
            Text(
                "已靜音，AI 聽不到你",
                color = Palette.Warn, fontSize = Type.Meta,
                modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
                textAlign = TextAlign.Center,
            )
        }

        val err = s.error
        when {
            !micGranted -> Hint("沒有麥克風權限，AI 聽不到你。到系統設定裡開給這個 App。")
            err != null -> Hint(err)
        }

        // 布丁狗：跟著通話狀態演（CallPet.kt）
        CallPet(s.state, Modifier.fillMaxWidth().height(300.dp).padding(top = 8.dp),
            act = s.petAct, actSeq = s.petActSeq)

        // 字幕：你說的、我回的
        Column(
            Modifier.weight(1f).fillMaxWidth().padding(vertical = 16.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            if (s.youText.isNotBlank()) Caption("你", s.youText, Palette.TextDim)
            if (s.replyText.isNotBlank()) Caption("我", s.replyText, Palette.Text)
        }

        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            val canInterrupt = s.state == CallState.THINKING || s.state == CallState.SPEAKING
            CallButton("打斷", enabled = canInterrupt, tint = Palette.Accent,
                modifier = Modifier.weight(1f)) { vm.interrupt() }
            CallButton(if (s.muted) "取消靜音" else "靜音", enabled = s.link != Link.ENDED,
                tint = if (s.muted) Palette.Warn else Palette.Text,
                modifier = Modifier.weight(1f)) { vm.toggleMute() }
            CallButton("掛斷", enabled = true, tint = Palette.Danger,
                modifier = Modifier.weight(1f)) {
                vm.hangup()
                onClose()
            }
        }
    }
}

/**
 * 視訊通話畫面（2026-11-18，通話第三棒）：整片是鏡頭預覽，狀態、字幕、按鈕疊在上面。
 *
 * 手機是架起來放遠的，所以字幕與按鈕放在底部半透明黑底上，背景再亂都看得清楚；
 * 預設後鏡頭（對著手上在做的東西），「切換鏡頭」換前鏡頭。
 * 狀態判斷跟語音通話同一套（[CallUiState.state]），送不送圖由 [CallViewModel.onFrame] 決定。
 */
@Composable
fun VideoCallScreen(
    micGranted: Boolean,
    camGranted: Boolean,
    rotation: Int,
    convId: String?,
    onClose: () -> Unit,
) {
    val vm: CallViewModel = viewModel()
    val s by vm.state.collectAsState()
    var lens by rememberSaveable { mutableIntStateOf(CameraSelector.LENS_FACING_BACK) }
    var camError by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) { vm.start("video", convId) }
    LaunchedEffect(micGranted) { if (micGranted) vm.startMic() }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        if (camGranted) {
            CameraPreview(
                lensFacing = lens,
                rotation = rotation,
                onFrame = vm::onFrame,
                onError = { camError = it },
                modifier = Modifier.fillMaxSize(),
            )
        }

        // 右上角小隻布丁狗，避開上方狀態列與下方字幕，不擋鏡頭中央
        CallPet(
            s.state, big = false, act = s.petAct, actSeq = s.petActSeq,
            modifier = Modifier.align(Alignment.TopEnd).statusBarsPadding().padding(top = 64.dp, end = 8.dp)
                .size(width = 110.dp, height = 120.dp),
        )

        // 上方：狀態字，壓一條半透明底
        Column(
            Modifier.fillMaxWidth().background(Scrim).statusBarsPadding()
                .padding(horizontal = Space.Screen, vertical = 10.dp),
        ) {
            Text(
                s.state.label,
                color = when (s.state) {
                    CallState.SPEAKING -> Palette.Accent
                    CallState.RECONNECTING, CallState.ENDED -> Palette.Warn
                    else -> Color.White
                },
                fontSize = 26.sp,
                modifier = Modifier.fillMaxWidth(),
                textAlign = TextAlign.Center,
            )
            if (s.muted) {
                Text(
                    "已靜音，AI 聽不到也看不到你",
                    color = Palette.Warn, fontSize = Type.Meta,
                    modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                    textAlign = TextAlign.Center,
                )
            }
        }

        // 下方：提示、字幕、按鈕，同一塊半透明底
        Column(
            Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(Scrim)
                .navigationBarsPadding().padding(Space.Screen),
        ) {
            val err = s.error
            when {
                !camGranted -> Hint("沒有相機權限，AI 看不到畫面。到系統設定裡開給這個 App。")
                !micGranted -> Hint("沒有麥克風權限，AI 聽不到你。到系統設定裡開給這個 App。")
                camError != null -> Hint(camError!!)
                err != null -> Hint(err)
            }
            Column(
                Modifier.fillMaxWidth().heightIn(max = 180.dp).padding(vertical = 10.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                if (s.youText.isNotBlank()) Caption("你", s.youText, Color.White.copy(alpha = 0.75f))
                if (s.replyText.isNotBlank()) Caption("我", s.replyText, Color.White)
            }
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                val canInterrupt = s.state == CallState.THINKING || s.state == CallState.SPEAKING
                CallButton("打斷", enabled = canInterrupt, tint = Palette.Accent,
                    modifier = Modifier.weight(1f)) { vm.interrupt() }
                CallButton(if (s.muted) "取消靜音" else "靜音", enabled = s.link != Link.ENDED,
                    tint = if (s.muted) Palette.Warn else Palette.Text,
                    modifier = Modifier.weight(1f)) { vm.toggleMute() }
                CallButton("切換鏡頭", enabled = camGranted, tint = Palette.Text,
                    modifier = Modifier.weight(1f)) {
                    camError = null
                    lens = if (lens == CameraSelector.LENS_FACING_BACK) CameraSelector.LENS_FACING_FRONT
                    else CameraSelector.LENS_FACING_BACK
                }
                CallButton("掛斷", enabled = true, tint = Palette.Danger,
                    modifier = Modifier.weight(1f)) {
                    vm.hangup()
                    onClose()
                }
            }
        }
    }
}

/** 疊在鏡頭畫面上的半透明黑底。 */
private val Scrim = Color.Black.copy(alpha = 0.55f)

@Composable
private fun LevelBar(level: Float) {
    Box(
        Modifier.fillMaxWidth().height(8.dp).clip(Radii.Tiny).background(Palette.Surface),
    ) {
        Box(
            Modifier.fillMaxHeight().fillMaxWidth(level.coerceIn(0f, 1f))
                .background(Palette.Accent),
        )
    }
}

@Composable
private fun Caption(who: String, text: String, color: Color) {
    Column {
        Text(who, color = Palette.TextFaint, fontSize = Type.Tiny)
        Text(text, color = color, fontSize = 20.sp, lineHeight = 28.sp)
    }
}

@Composable
private fun Hint(text: String) {
    Text(
        text,
        color = Palette.Warn, fontSize = Type.Meta, lineHeight = Type.MetaLine,
        modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
    )
}

/** 大塊按鈕，整塊都可按（手機架在桌上時不必瞄準）。 */
@Composable
private fun CallButton(
    text: String,
    enabled: Boolean,
    tint: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Box(
        modifier
            .clip(Radii.Card)
            .background(if (enabled) Palette.SurfaceHi else Palette.Surface)
            .clickable(enabled = enabled, role = Role.Button) { onClick() }
            .padding(vertical = 20.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text,
            color = if (enabled) tint else Palette.TextFaint,
            fontSize = 18.sp,
            textAlign = TextAlign.Center,
        )
    }
}
