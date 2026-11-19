package dev.butlerkit.app.voice.call

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.platform.LocalContext
import dev.butlerkit.app.ui.Ch
import dev.butlerkit.app.ui.Chans
import dev.butlerkit.app.ui.MODE_ALL
import dev.butlerkit.app.ui.PurinAssets
import dev.butlerkit.app.ui.PurinAssetsLoader
import dev.butlerkit.app.ui.PurinPainter
import dev.butlerkit.app.ui.PurinPlayer
import dev.butlerkit.app.ui.applyXform
import dev.butlerkit.app.ui.ch
import kotlin.math.min
import kotlin.math.sin

/**
 * 通話畫面上的布丁狗（2026-11-18）。
 *
 * 為什麼有它：14:44 布丁在通話中問「這個打電話的介面能不能加上布丁狗的動畫」。原本的通話畫面只有
 * 狀態大字和音量條，看不出對面「是誰、在幹嘛」。這裡沿用書桌那隻的零件與動作庫（rig.json／anims.json，
 * 跟 PurinDesk 同一份資料、同一個 [PurinPlayer]），只畫布丁狗本體和牠動作裡帶的小道具，不畫桌子。
 *
 * 狀態對應（[CallState]）：
 *   連線中／重連中  發呆，整隻淡一點
 *   聆聽中          發呆；剛接通時先揮手打招呼
 *   思考中          先點頭（表示聽到了）再進思考循環
 *   說話中          發呆＋嘴巴不規律開合（沒有播放音量可讀，用兩個不同頻率的波疊出像講話的節奏）
 *   通話已結束      鞠躬
 *
 * 活起來（16:56「多做幾個大動作，我要讓布丁狗活起來」）：
 *   助理回覆裡挑的動作（[[動作:名稱]] → butler-call 轉成 pet_act）收到就演，不等講完。
 *   聆聽中一陣子沒事（20～45 秒隨機）自己演一段大動作。大動作＝資料裡有 `_m` 手機版的那些，新加的自動算進來。
 *   [big]＝語音畫面，空間大，演寬版；視訊角落那隻小，演 `_m`。
 *
 * 不接音效：動作片段裡的 sfx 事件一律不播，通話中出聲會被麥克風收進去、又干擾判停。
 * 單段動作的名稱集中在下面幾個常數，換成別段（例如新做的大動作）只改這裡；資料裡沒有那段就略過，不會當掉。
 */

/** 剛接通、第一次進入聆聽時演。 */
private const val ACT_HELLO = "waveHi"
/** 從聆聽轉到思考時演（聽到了）。 */
private const val ACT_HEARD = "nod"
/** 通話結束時演。 */
private const val ACT_BYE = "bow"

private val ORDER = listOf("base", "event")

/** 一隻通話用布丁狗的播放狀態；跟著 composable 活，換畫面就丟掉。 */
internal class CallPetRig(private val assets: PurinAssets, private val big: Boolean) {
    val player = PurinPlayer(assets.anims.clips, ::hiddenByDefault)
    private val painter = PurinPainter()
    private val lay = assets.anims.scene.actors.associateBy { it.name }
    /**
     * 畫的順序：scene.order 是「圖層」名（back、props、fx…），不是角色名，要照圖層展開成角色（同 PurinDesk.layers）。
     * 只要布丁狗和特效那幾層：桌子、家具、Muffin 不畫。2026-11-18 第一版直接拿圖層名當角色名，布丁狗整隻畫不出來。
     */
    private val drawOrder: List<String> = assets.anims.scene.order.flatMap { layer ->
        assets.anims.scene.actors.filter { it.layer == layer && (it.name == "purin" || layer == "fx") }.map { it.name }
    }
    var lastState: CallState? = null
    /** 大動作清單：有 `_m` 手機版的就是（第一批 8 段起，新加的自動進來）。 */
    private val bigActs = assets.anims.clips.keys.filter { it.endsWith("_m") }.map { it.removeSuffix("_m") }
        .filter { assets.anims.clips.containsKey(it) }
    /** 上一次「有事」的時間（不在聆聽、或正在演單段動作）；閒太久就自己演。 */
    private var busyAt = 0.0
    private var idleGap = 25.0

    init {
        // 單段動作演完就清掉那一層，回到底下的循環；事件（音效）不接
        player.onEnd = { slot, _ -> if (slot == "event") player.play("event", null, nowSec) }
    }

    /** onEnd 需要「現在」；每格畫之前更新。 */
    var nowSec = 0.0

    private fun hiddenByDefault(key: String): Boolean {
        val dot = key.indexOf('.')
        if (dot < 0) return lay[key]?.hidden ?: false
        return assets.things[key.substring(0, dot)]?.hidden?.get(key.substring(dot + 1)) ?: false
    }

    fun act(name: String, now: Double) {
        val clips = assets.anims.clips
        val n = if (!big && clips.containsKey(name + "_m")) name + "_m" else name
        if (clips.containsKey(n)) player.play("event", n, now, restart = true)
    }

    fun onState(s: CallState, now: Double) {
        val prev = lastState
        lastState = s
        player.play("base", if (s == CallState.THINKING) "think" else "idle", now)
        when {
            s == CallState.LISTENING && (prev == null || prev == CallState.CONNECTING) -> act(ACT_HELLO, now)
            s == CallState.THINKING && prev == CallState.LISTENING -> act(ACT_HEARD, now)
            s == CallState.ENDED && prev != CallState.ENDED -> act(ACT_BYE, now)
        }
    }

    /**
     * 畫一格。大畫面（[big]）：身高約畫布 32%、腳底在 96%、水平放在 42%，上面留兩個身高給翻跟斗，右邊留給滾動；
     * 小畫面：身高約 55%、腳底 85%、置中。
     * 動作裡的整隻位移（"purin" 的 X／Y）是場景單位，依書桌那份版面的身高換算成畫布像素。
     */
    fun draw(c: android.graphics.Canvas, w: Float, h: Float, state: CallState) {
        val now = nowSec
        if (state != CallState.LISTENING || player.playing("event") != null) busyAt = now
        else if (bigActs.isNotEmpty() && now - busyAt > idleGap) {
            act(bigActs.random(), now)
            busyAt = now
            idleGap = 20.0 + Math.random() * 25.0
        }
        val pose = player.pose(now, ORDER)
        if (state == CallState.SPEAKING && player.playing("event") == null) {
            val open = sin(now * 2 * Math.PI * 4.7) + 0.6 * sin(now * 2 * Math.PI * 1.3) > 0.2
            if (open) pose.ch("purin.mouth").putV("open")
        }
        player.last = pose
        val pl = lay["purin"] ?: return
        val pt = assets.things["purin"] ?: return
        val px = (if (big) min(h * 0.32f, w * 0.3f) else min(h * 0.55f, w * 0.7f)) / pl.h   // 場景單位 → 畫布像素
        val ox = w * (if (big) 0.42f else 0.5f) - pl.x * px - pt.w * (pl.h / pt.h) * px / 2f
        val oy = h * (if (big) 0.96f else 0.85f) - pl.y * px - pl.h * px
        val dim = state == CallState.CONNECTING || state == CallState.RECONNECTING
        val save = c.save()
        if (dim) c.saveLayerAlpha(0f, 0f, w, h, 120)
        for (name in drawOrder) {
            // 道具只畫這一格動作有碰到的（道具本身或它的零件有軌道）
            if (name != "purin" && pose[name] == null && pose.keys.none { it.startsWith("$name.") }) continue
            drawActor(c, name, pose, ox, oy, px)
        }
        c.restoreToCount(save)
    }

    private fun drawActor(c: android.graphics.Canvas, name: String, pose: HashMap<String, Chans>, ox: Float, oy: Float, px: Float) {
        val L = lay[name] ?: return
        val t = assets.things[name] ?: return
        val ch = pose[name]
        val op = ch?.get(Ch.OP, if (L.hidden) 0f else 1f) ?: (if (L.hidden) 0f else 1f)
        if (op <= 0.003f) return
        val sc = L.h / t.h
        val save = c.save()
        c.translate(ox + (L.x + (ch?.get(Ch.X, 0f) ?: 0f)) * px, oy + (L.y + (ch?.get(Ch.Y, 0f) ?: 0f)) * px)
        c.scale(sc * px, sc * px)
        val (pvx, pvy) = L.pivot ?: (t.w / 2 to t.h)
        val inner = Chans()
        ch?.let {
            if (it.has(Ch.SX)) inner.set(Ch.SX, it[Ch.SX])
            if (it.has(Ch.SY)) inner.set(Ch.SY, it[Ch.SY])
            if (it.has(Ch.ROT)) inner.set(Ch.ROT, it[Ch.ROT])
        }
        applyXform(c, inner, pvx, pvy)
        if (op < 0.997f) c.saveLayerAlpha(-t.w, -t.h, t.w * 2, t.h * 2, (op * 255).toInt())
        painter.drawThing(c, t, pose, MODE_ALL)
        c.restoreToCount(save)
    }
}

@Composable
fun CallPet(
    state: CallState,
    modifier: Modifier = Modifier,
    big: Boolean = true,
    act: String = "",
    actSeq: Int = 0,
) {
    val ctx = LocalContext.current.applicationContext
    val assets by produceState(PurinAssetsLoader.cachedOrNull()) { value = PurinAssetsLoader.load(ctx) }
    val a = assets ?: return
    val rig = remember(a) { CallPetRig(a, big) }
    var frame by remember { mutableLongStateOf(0L) }
    val t0 = remember { System.nanoTime() }

    LaunchedEffect(rig) {
        while (true) withFrameNanos { frame = it }
    }
    LaunchedEffect(rig, state) {
        rig.nowSec = (System.nanoTime() - t0) / 1e9
        rig.onState(state, rig.nowSec)
    }

    LaunchedEffect(rig, actSeq) {
        if (actSeq > 0 && act.isNotBlank()) rig.act(act, (System.nanoTime() - t0) / 1e9)
    }

    Canvas(modifier) {
        frame.let { }   // 讀一次，讓每一格都重畫
        rig.nowSec = (System.nanoTime() - t0) / 1e9
        drawIntoCanvas { rig.draw(it.nativeCanvas, size.width, size.height, state) }
    }
}
