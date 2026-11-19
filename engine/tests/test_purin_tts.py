"""布丁狗語音實測：tts_qwen.QwenTts 串流、中途取消、連不上退回 edge-tts（2026-11-18）。

要 purin_tts_server.py 開著（127.0.0.1:8792）；沒開時第一項就 FAIL，不會 SKIP 算過。
退回那項會打 edge-tts。不播放聲音；輸出 wav 寫到系統暫存目錄。
跑法：D:\\butler-asr\\venv\\Scripts\\python.exe tests\\test_purin_tts.py
"""
import asyncio
import os
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tts import EdgeTts  # noqa: E402
from tts_qwen import QwenTts  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    if not cond:
        FAILED.append(name)


class NoFallback:
    """測布丁狗本身時不准偷偷退回 edge-tts，否則服務沒開也會假通過。"""
    async def stream(self, text):
        raise RuntimeError("退回了 edge-tts：布丁狗語音服務沒開？")
        yield b""


async def collect(tts, text, stop_after=None):
    t0 = time.perf_counter()
    first, buf = None, bytearray()
    agen = tts.stream(text)
    async for b in agen:
        if first is None:
            first = time.perf_counter() - t0
        buf += b
        if stop_after and len(buf) >= stop_after:
            await agen.aclose()
            break
    return bytes(buf), first, time.perf_counter() - t0


async def main():
    q = QwenTts(fallback=NoFallback())
    try:
        pcm, first, total = await collect(q, "嘿嘿，布丁你好！今天要做什麼呀？")
    except RuntimeError as e:
        check(f"布丁狗有聲音（{e}）", False)
        sys.exit(1)
    dur = len(pcm) / 2 / 24000
    print(f"布丁狗：{dur:.2f}s 聲音，第一段 {first:.2f}s，總共 {total:.2f}s")
    check("布丁狗有聲音", dur > 1.0)
    check("第一段 1.5 秒內", first is not None and first < 1.5)
    check("位元組數是偶數", len(pcm) % 2 == 0)
    with wave.open(os.path.join(tempfile.gettempdir(), "purin_tts_test.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)

    # 中途取消後，下一句要能正常念（伺服器的顯卡鎖要放掉）
    pcm2, _, _ = await collect(q, "這是一句很長很長的句子，我要在中間把它掛掉，看看伺服器會不會卡住不放。",
                               stop_after=24000)
    check("取消時有拿到部分聲音", len(pcm2) >= 24000)
    pcm3, first3, total3 = await collect(q, "掛斷之後還能講話嗎？")
    print(f"取消後下一句：第一段 {first3:.2f}s，總共 {total3:.2f}s")
    check("取消後下一句正常", len(pcm3) / 48000 > 0.8 and first3 < 3.0)

    # 退回：指向沒人聽的埠
    bad = QwenTts(fallback=EdgeTts(), url="http://127.0.0.1:8799")
    pcm4, first4, _ = await collect(bad, "服務沒開的時候。")
    print(f"退回 edge-tts：第一段 {first4:.2f}s")
    check("連不上時退回 edge-tts 有聲音", len(pcm4) / 48000 > 0.5)
    print("FAILED", FAILED)
    sys.exit(1 if FAILED else 0)


asyncio.run(main())
