# -*- coding: utf-8 -*-
"""通話引擎端到端測試：真的起引擎（8791 埠）、真的打 Claude API 與 edge-tts。

為什麼有它：單元測試把辨識、大腦、合成都換成假的；引擎實際起來、Breeze＋Claude＋edge-tts
接在一起能不能走完協定 v1，只有這支會驗。2026-11-18 第三棒寫。

四輪：
  1. 語音一問一答（s1_f.wav）
  2. 打斷：收到第一包 0x11 立刻送 interrupt＋ping；pong 之後還到的該輪輸出算違規；接著 s4_f.wav 要能正常走完
  3. 同 call_id 重連，問最一開始的問題，回覆要跟第一輪有關
  4. 視訊：送紅色實心圓 JPEG，問手上東西的顏色，回覆要含「紅」；再送一張圖問形狀，回覆要含「圓」
     （這時上一句的圖在歷史裡已換成 [畫面]，驗真 API 收得下這種歷史與頂層 cache_control）
  5. 記憶與對話脈絡（2026-11-18 大腦改成 butler 本人後加）：通話接的是一條暫時開的 butler 對話，
     開打前先在那條對話用打字講「我等一下要焊 LED 燈條」；電話裡問「我叫什麼名字」要答得出你的名字（環境變數 E2E_EXPECT_NAME，逗號分隔）／布丁，
     問「我等一下要做什麼」要答得出焊接。測完刪掉那條暫時對話。
失敗 exit 1。不播放任何聲音；引擎用 CREATE_NO_WINDOW 起、結束用 PID 收掉。

用法：D:\\butler-asr\\venv\\Scripts\\python.exe tests\\e2e_client.py
"""
import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

import edge_tts
import numpy as np
from PIL import Image, ImageDraw
from websockets.asyncio.client import connect

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = r"D:\butler-asr\venv\Scripts\python.exe"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
AUDIO = Path(r"D:\butler-asr\audio")
PORT = 8791
URL = f"ws://127.0.0.1:{PORT}/call"
PKT = 5120  # 160 ms @ 16 kHz 16 位元
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FAILED: list[str] = []
LAT: list[str] = []
CONV: dict = {}  # {"id": 暫時對話 id}


def butler():
    import httpx
    sys.path.insert(0, str(ROOT))
    from butler_brain import find_butler_base
    token = (ROOT / "token.txt").read_text(encoding="utf-8").strip()
    return httpx.Client(base_url=find_butler_base(), headers={"Authorization": f"Bearer {token}"},
                        timeout=30)


def butler_say_and_wait(c, conv: str, text: str, timeout: float = 120) -> bool:
    """在那條對話打字送一句，等到 turn.done。"""
    import json as _json
    from httpx_sse import connect_sse
    import httpx
    with httpx.Client(base_url=str(c.base_url), headers=c.headers, timeout=None) as sc:
        with connect_sse(sc, "GET", "/v1/stream") as es:
            c.post(f"/v1/conversations/{conv}/message", json={"text": text, "client": "desktop"}).raise_for_status()
            t = time.time()
            for sse in es.iter_sse():
                if time.time() - t > timeout:
                    return False
                if sse.event == "turn.done" and sse.data and _json.loads(sse.data).get("conv_id") == conv:
                    return True
    return False


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(("過  " if cond else "不過") + f" {name}" + (f"｜{detail}" if detail else ""))
    if not cond:
        FAILED.append(name)
    return cond


def read_wav16k(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return w.readframes(w.getnframes())


async def synth_wav(text: str, out: Path) -> bytes:
    """edge-tts 合成 → ffmpeg 轉 16 kHz 單聲道 wav（只寫檔，不播放）。"""
    mp3 = bytearray()
    async for c in edge_tts.Communicate(text, "zh-TW-HsiaoChenNeural").stream():
        if c["type"] == "audio":
            mp3 += c["data"]
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0",
                    "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", str(out)],
                   input=bytes(mp3), check=True, creationflags=NO_WINDOW)
    return read_wav16k(out)


def red_circle_jpeg() -> bytes:
    img = Image.new("RGB", (640, 480), (235, 235, 235))
    ImageDraw.Draw(img).ellipse((200, 120, 440, 360), fill=(220, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class Conn:
    """一條連線：背景收訊息，記下（收到時間, 種類, 內容）。"""

    def __init__(self, ws):
        self.ws, self.events, self.cond = ws, [], asyncio.Condition()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for m in self.ws:
                t = time.perf_counter()
                if isinstance(m, bytes):
                    ev = (t, "audio", {"turn": int.from_bytes(m[1:5], "big"), "kind": m[0], "n": len(m) - 5})
                else:
                    obj = json.loads(m)
                    ev = (t, obj.get("type"), obj)
                async with self.cond:
                    self.events.append(ev)
                    self.cond.notify_all()
        except Exception:
            pass

    async def wait(self, pred, start: int = 0, timeout: float = 60.0):
        """等第一個符合 pred 的事件（從 start 起找）。回 (索引, 事件)，逾時回 (None, None)。"""
        async def _w():
            i = start
            while True:
                async with self.cond:
                    while i >= len(self.events):
                        await self.cond.wait()
                    while i < len(self.events):
                        if pred(self.events[i]):
                            return i, self.events[i]
                        i += 1
        try:
            return await asyncio.wait_for(_w(), timeout)
        except asyncio.TimeoutError:
            return None, None

    async def send_json(self, obj):
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def send_speech(self, pcm: bytes, tail_sec: float = 1.5) -> float:
        """照實際速度每 160 ms 送一包，尾巴補靜音。回「講完」的時間：含最後一個人聲樣本的那包送出時
        （音檔本身尾巴有靜音，用檔案結尾當講完會少算）。"""
        a = np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32))
        voiced = np.nonzero(a > 500)[0]
        last_pkt = (int(voiced[-1]) * 2 // PKT) if voiced.size else 0
        t_next = time.perf_counter()
        t_end = None
        for k, i in enumerate(range(0, len(pcm), PKT)):
            chunk = pcm[i:i + PKT].ljust(PKT, b"\x00")
            await self.ws.send(b"\x01" + chunk)
            if k == last_pkt:
                t_end = time.perf_counter()
            t_next += 0.16
            await asyncio.sleep(max(0.0, t_next - time.perf_counter()))
        t_end = t_end or time.perf_counter()
        for _ in range(int(tail_sec / 0.16)):
            await self.ws.send(b"\x01" + b"\x00" * PKT)
            t_next += 0.16
            await asyncio.sleep(max(0.0, t_next - time.perf_counter()))
        return t_end

    async def close(self):
        await self.ws.close()
        self.reader.cancel()


async def open_conn(call_id: str, mode: str = "voice") -> Conn:
    c = Conn(await connect(URL, max_size=None))
    await c.send_json({"type": "start", "mode": mode, "call_id": call_id, "conv_id": CONV["id"]})
    i, _ = await c.wait(lambda e: e[1] == "ready", timeout=10)
    check(f"start({mode}) 收到 ready", i is not None)
    return c


async def one_turn(c: Conn, label: str, pcm: bytes, timeout: float = 90.0):
    """送一句、等該輪 tts_end。回 (turn, 辨識文字, 回覆文字, 語音包數)；失敗回 None。"""
    start = len(c.events)
    speech_task = asyncio.create_task(c.send_speech(pcm))
    i, ev = await c.wait(lambda e: e[1] == "asr_ended", start, timeout)
    if not check(f"{label} 收到 asr_ended", i is not None):
        speech_task.cancel()
        return None
    t_end = await speech_task
    turn, text = ev[2]["turn"], ev[2]["text"]
    check(f"{label} 辨識文字非空", bool(text.strip()), text)
    j, end = await c.wait(lambda e: e[1] == "tts_end" and e[2].get("turn") == turn, i, timeout)
    if not check(f"{label} 收到 tts_end", j is not None):
        return None
    seg = c.events[i:j + 1]
    deltas = [e for e in seg if e[1] == "reply_delta" and e[2]["turn"] == turn]
    audio = [e for e in seg if e[1] == "audio" and e[2]["turn"] == turn]
    errors = [e[2] for e in seg if e[1] == "error"]
    reply = "".join(e[2]["text"] for e in deltas)
    check(f"{label} 有 reply_delta", bool(deltas), reply[:80])
    check(f"{label} 至少一包 0x11", bool(audio) and all(e[2]["kind"] == 0x11 for e in audio), f"{len(audio)} 包")
    check(f"{label} 沒有 error", not errors, str(errors))
    lat(label, t_end, ev[0], deltas[0][0] if deltas else None, audio[0][0] if audio else None, end[0])
    await c.send_json({"type": "playback_done", "turn": turn})
    return turn, text, reply, len(audio)


def lat(label, t_end, t_asr, t_claude, t_audio, t_done):
    def ms(t):
        return f"{round((t - t_end) * 1000)} ms" if t is not None else "無"
    s = f"{label}：講完→辨識 {ms(t_asr)}｜→Claude 首字 {ms(t_claude)}｜→第一包語音 {ms(t_audio)}｜→tts_end 總計 {ms(t_done)}"
    LAT.append(s)
    print("延遲 " + s)


async def run(tmp: Path) -> None:
    call_id = str(uuid.uuid4())
    q3 = await synth_wav("我最一開始問你的問題是什麼？", tmp / "q3.wav")
    q4 = await synth_wav("我手上拿的是什麼顏色的東西？", tmp / "q4.wav")
    q4b = await synth_wav("那它是什麼形狀？", tmp / "q4b.wav")

    # ---- 第一輪 ----
    print("== 第一輪：語音一問一答")
    c = await open_conn(call_id)
    r1 = await one_turn(c, "第一輪", read_wav16k(AUDIO / "s1_f.wav"))
    if r1:
        check("第一輪 輪次是 0", r1[0] == 0, str(r1[0]))

    # ---- 第二輪：打斷 ----
    print("== 第二輪：打斷")
    start = len(c.events)
    speech = asyncio.create_task(c.send_speech(read_wav16k(AUDIO / "s2_f.wav")))
    i, ev = await c.wait(lambda e: e[1] == "audio", start, 90)
    if check("第二輪 收到第一包 0x11", i is not None):
        turn2 = ev[2]["turn"]
        await c.send_json({"type": "interrupt"})
        await c.send_json({"type": "ping"})
        p, pong = await c.wait(lambda e: e[1] == "pong", i, 10)
        check("第二輪 interrupt 後收到 pong", p is not None)
        await asyncio.sleep(4.0)  # 看 pong 之後還有沒有漏網的
        late = [e for e in c.events[(p or i) + 1:]
                if e[1] in ("audio", "reply_delta", "tts_end") and (e[2].get("turn") == turn2)]
        check("第二輪 pong 之後沒有該輪輸出", not late, f"違規 {len(late)} 則：{[e[1] for e in late][:5]}")
        print(f"   打斷前已到 0x11 {sum(1 for e in c.events[i:p or i] if e[1] == 'audio')} 包")
    await speech
    r2b = await one_turn(c, "第二輪之後", read_wav16k(AUDIO / "s4_f.wav"))
    if r2b and i is not None:
        check("第二輪之後 輪次比被打斷的大", r2b[0] > ev[2]["turn"], f"{ev[2]['turn']}→{r2b[0]}")
    await c.close()

    # ---- 第三輪：同 call_id 重連 ----
    print("== 第三輪：同 call_id 重連")
    await asyncio.sleep(0.5)
    c = await open_conn(call_id)
    r3 = await one_turn(c, "第三輪", q3)
    if r3:
        check("第三輪 重連後輪次從 0 起", r3[0] == 0, str(r3[0]))
        keys = ("電阻", "resistor", "Resistor", "麵包板", "breadboard", "Breadboard", "第三排", "第 3 排", "三排",
                "焊", "LED")  # 接 butler 對話後，那條對話的第一句是開打前打字講的「要焊 LED」，答它也對
        check("第三輪 回覆跟第一輪有關", any(k in r3[2] for k in keys), r3[2][:120])
    await c.close()

    # ---- 第四輪：視訊 ----
    print("== 第四輪：視訊")
    c = await open_conn(str(uuid.uuid4()), "video")
    start = len(c.events)
    await c.ws.send(b"\x02" + red_circle_jpeg())
    a, _ = await c.wait(lambda e: e[1] == "image_ack", start, 5)
    check("第四輪 收到 image_ack", a is not None)
    r4 = await one_turn(c, "第四輪", q4)
    if r4:
        check("第四輪 回覆含「紅」", "紅" in r4[2], r4[2][:120])
    # 視訊第二句：再送一張圖，這時歷史裡上一句的圖會被換成 [畫面]（驗真 API 收這種歷史、頂層快取不報錯）
    start = len(c.events)
    await c.ws.send(b"\x02" + red_circle_jpeg())
    a, _ = await c.wait(lambda e: e[1] == "image_ack", start, 5)
    check("第四輪第二句 收到 image_ack", a is not None)
    r4b = await one_turn(c, "第四輪第二句", q4b)
    if r4b:
        check("第四輪第二句 回覆含「圓」", "圓" in r4b[2], r4b[2][:120])
    await c.send_json({"type": "hangup"})
    await asyncio.sleep(0.3)
    await c.close()

    # ---- 第五輪：記憶與對話脈絡 ----
    print("== 第五輪：記憶與對話脈絡")
    q5 = await synth_wav("我叫什麼名字？", tmp / "q5.wav")
    q5b = await synth_wav("我等一下要做什麼？", tmp / "q5b.wav")
    c = await open_conn(str(uuid.uuid4()))
    r5 = await one_turn(c, "第五輪 名字", q5)
    if r5:
        check("第五輪 答得出名字", any(k in r5[2] for k in tuple(os.environ.get("E2E_EXPECT_NAME", "布丁").split(","))), r5[2][:120])
    r5b = await one_turn(c, "第五輪 對話脈絡", q5b)
    if r5b:
        check("第五輪 知道要焊 LED", "焊" in r5b[2], r5b[2][:120])
    await c.send_json({"type": "hangup"})
    await asyncio.sleep(0.3)
    await c.close()


def wait_port(proc, timeout=120) -> bool:
    t = time.time()
    while time.time() - t < timeout:
        if proc.poll() is not None:
            return False
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.5)
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bcall_e2e_"))
    env = dict(os.environ, BUTLER_CALL_LOG_DIR=str(tmp / "log"), PYTHONIOENCODING="utf-8")
    out = open(tmp / "server_out.txt", "wb")
    proc = subprocess.Popen([PY, str(ROOT / "server.py"), "--port", str(PORT)], cwd=str(ROOT), env=env,
                            stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    print(f"引擎 PID {proc.pid}，暫存 {tmp}")
    try:
        t = time.time()
        if not check("引擎起來（埠開了）", wait_port(proc), f"{time.time() - t:.1f} 秒"):
            return 1
        bc = butler()
        CONV["id"] = bc.post("/v1/conversations", json={}).json()["conv_id"]
        bc.patch(f"/v1/conversations/{CONV['id']}", json={"title": "通話測試（測完會自動刪）"})
        print(f"暫時對話 {CONV['id']}")
        check("先在對話裡打字講要焊 LED", butler_say_and_wait(
            bc, CONV["id"], "（這是通話功能的自動測試）我等一下要焊 LED 燈條，先記著就好，回一句「好」。"))
        asyncio.run(run(tmp))
    except Exception as e:
        check("測試跑完沒有例外", False, f"{type(e).__name__}: {e}")
    finally:
        if CONV.get("id"):
            try:
                d = butler().delete(f"/v1/conversations/{CONV['id']}")
                print(f"刪暫時對話 {CONV['id']}：{d.status_code}")
            except Exception as e:
                print(f"刪暫時對話失敗：{e!r}")
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()
    log = tmp / "log" / "call.log"
    print("== 引擎 log 的延遲行")
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if "延遲" in line or "ERROR" in line or "WARNING" in line or "被打斷" in line:
                print("  " + line[24:])
    print("== 延遲摘要")
    for s in LAT:
        print("  " + s)
    if FAILED:
        print("== 失敗：" + "、".join(FAILED))
        tail = (tmp / "server_out.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        print("== 引擎輸出尾巴\n" + "\n".join(tail))
        return 1
    print("== 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
