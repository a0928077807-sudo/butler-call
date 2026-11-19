# -*- coding: utf-8 -*-
"""檢查語音服務念字有沒有念錯：叫正在跑的語音服務念幾句，再用 Breeze 辨識轉回文字，算字錯率。

為什麼有它：照參考音念的模型會念錯字（實際碰過「電腦」念成「蛋堵」、「講」念成「搞」），耳朵聽不一定抓得到，
換聲音、換模型、調參數之後都該跑一次，用數字比。也會先把參考音本身轉回文字，跟你給的原文比——
參考音原文對不上，是念錯字最常見的原因。

跑法（在通話引擎的 venv，engine 資料夾要在旁邊；語音服務要先開著）：
  python check_voice.py --ref ../engine/voice/purin_ref4.wav --ref-text "參考音原文" [--url http://127.0.0.1:8792]
輸出：每句原文／轉回／字錯率，最後印平均。數字越小越好（原版 0.6B 約 0.10～0.16，1.7B 約 0.05）。
"""
import argparse
import sys
import wave
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
from asr_engine import AsrEngine  # noqa: E402

SENTS = [
    "對，你講完之後，電腦會先把你的聲音轉成文字，",
    "我看文字回答，再用這個聲音念給你聽。",
    "先把烙鐵插上電，等它熱起來再說。",
    "這個電阻是一千歐姆，接在第三排的位置。",
    "明天早上八點有實驗課，記得帶報告。",
    "抱歉，我剛剛沒聽清楚，你可以再講一次嗎？",
]


def norm(s):
    return "".join(c for c in s if c.isalnum())


def cer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(len(r), 1)


def to16k(y, sr):
    y = np.asarray(y, dtype=np.float32).reshape(-1)
    n = int(round(len(y) * 16000 / sr))
    y = np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y)
    return (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="參考音 wav（16 位元）")
    ap.add_argument("--ref-text", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8792")
    a = ap.parse_args()
    asr = AsrEngine()
    with wave.open(a.ref) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        y = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    if ch > 1:
        y = y.reshape(-1, ch).mean(axis=1)
    hyp = asr.transcribe(to16k(y, sr))
    print(f"參考音轉回：{hyp}（跟原文差 {cer(a.ref_text, hyp):.3f}；大於 0.15 先檢查原文有沒有打錯）")
    errs = []
    with httpx.Client(timeout=60) as c:
        for s in SENTS:
            pcm = c.post(f"{a.url.rstrip('/')}/tts", json={"text": s}).content
            y = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
            hyp = asr.transcribe(to16k(y, 24000))
            errs.append(cer(s, hyp))
            print(f"{errs[-1]:.3f}  原文：{s}\n       轉回：{hyp}")
    print(f"平均字錯率 {np.mean(errs):.3f}")


if __name__ == "__main__":
    main()
