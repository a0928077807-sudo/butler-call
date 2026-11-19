# -*- coding: utf-8 -*-
"""用你自己的一段錄音，比 Breeze-ASR-25＋hotwords（這包）和 large-v3-turbo＋initial_prompt 誰聽得準。

為什麼有它（2026-11-19）：說明.md 裡的比較只量了一個人的一段錄音，換一個人的口音結果可能不一樣。
兩邊都用同一份常用詞，各照自己的呼叫方式跑：
  Breeze：beam 5、condition_on_previous_text=False、hotwords（跟 engine/asr_engine.py 一樣）
  turbo ：beam 5、initial_prompt＝「以下是繁體中文的對話，可能會出現這些專有名詞：…。」（另一種常見做法）
字錯率只做「去標點、轉繁體、英文小寫」，沒做國字數字轉換，所以會比說明.md 的表高一點，兩顆之間可以比。

跑法（在通話引擎的 venv）：
  python compare_asr.py --wav 我的錄音.wav --ref 我的錄音.txt --breeze <Breeze 模型資料夾> --turbo <turbo 模型資料夾> [--words "詞1 詞2 …"]
--words 不給就讀 engine/config.py 的 ASR_HOTWORDS。錄音要 16 kHz 單聲道 16 位元 wav，轉檔：
  ffmpeg -i 錄音.m4a -ac 1 -ar 16000 -c:a pcm_s16le 我的錄音.wav
"""
import argparse
import os
import site
import sys
import time
import unicodedata
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
for _sp in site.getsitepackages():  # ctranslate2 要 CUDA 12 的 DLL；用 pip 的 nvidia-* 套件（同 asr_engine.py）
    for _b in Path(_sp).glob("nvidia/*/bin"):
        os.environ["PATH"] = str(_b) + os.pathsep + os.environ["PATH"]
        os.add_dll_directory(str(_b))

import numpy as np  # noqa: E402
import opencc  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

CC = opencc.OpenCC("s2twp")


def clean(s: str) -> str:
    """轉繁體、英文小寫，去掉標點與空白。"""
    s = CC.convert(s).lower()
    return "".join(c for c in s if not unicodedata.category(c).startswith(("P", "Z", "S")) and not c.isspace())


def cer(ref: str, hyp: str) -> float:
    r, h = clean(ref), clean(hyp)
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / max(1, len(r))


def default_words() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
    from config import ASR_HOTWORDS
    return ASR_HOTWORDS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--ref", required=True, help="錄音實際講的內容（txt，UTF-8）")
    ap.add_argument("--breeze", required=True, help="Breeze-ASR-25 的 CTranslate2 模型資料夾")
    ap.add_argument("--turbo", required=True, help="faster-whisper large-v3-turbo 模型資料夾（或 large-v3-turbo 讓它自己下載）")
    ap.add_argument("--words", default=None, help="常用詞，空白分隔")
    a = ap.parse_args()

    with wave.open(a.wav) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            print("錄音要 16 kHz 單聲道 16 位元 wav，轉檔指令見檔頭")
            return 2
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    ref = Path(a.ref).read_text(encoding="utf-8")
    words = (a.words if a.words is not None else default_words()).split()
    prompt = "以下是繁體中文的對話，可能會出現這些專有名詞：" + "、".join(words) + "。"
    print(f"錄音 {len(audio) / 16000:.1f} 秒，常用詞 {len(words)} 個\n")

    runs = [("Breeze＋hotwords（這包）", a.breeze, "int8_float16",
             dict(condition_on_previous_text=False, hotwords=" ".join(words))),
            ("turbo＋initial_prompt", a.turbo, "float16", dict(initial_prompt=prompt))]
    for name, mdir, ct, kw in runs:
        m = WhisperModel(mdir, device="cuda", compute_type=ct)
        m.transcribe(audio[:16000 * 2], language="zh", beam_size=5, **kw)  # 暖機，不計時
        t = time.perf_counter()
        segs, _ = m.transcribe(audio, language="zh", beam_size=5, **kw)
        hyp = CC.convert("".join(s.text for s in segs).strip())
        sec = time.perf_counter() - t
        print(f"【{name}】字錯率 {cer(ref, hyp):.3f}，{sec:.1f} 秒\n{hyp}\n")
        del m
    return 0


if __name__ == "__main__":
    sys.exit(main())
