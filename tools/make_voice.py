# -*- coding: utf-8 -*-
"""用一段文字描述「生」出一個新聲音，當通話語音的參考音（Qwen3-TTS 1.7B VoiceDesign）。

為什麼有它：通話的語音服務（engine/purin_tts_server.py）是「照參考音念」——給它一段幾秒的錄音和那段錄音的原文，
它就用那個音色念任何句子。參考音不用是真人錄音：原版布丁狗的聲音就是這支用描述生出來的（日系吉祥物那段描述），
好處是不必拿任何人（例如配音員）的錄音去複製聲音。
VoiceDesign 每次抽樣都不一樣，所以一個描述生好幾次，量音高挑最接近目標的那次。

跑法（要有 NVIDIA 顯卡；環境見說明.md「換成自己的聲音」）：
  python make_voice.py --desc "描述" --text "參考音要念的句子" --takes 3 [--hz 450 --range 10.5] [--out voice_ref]
輸出：<out>_1.wav、<out>_2.wav…，以及 <out>.json（每次的音高中位數、起伏半音）。挑一個聽起來最對的，連同 --text 一起交給語音服務。
"""
import argparse
import json
import time

import librosa
import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"


def measure(y, sr):
    """音高中位數（Hz）與起伏（第 10～90 百分位差幾個半音）。量不到人聲回 None。"""
    y = librosa.resample(np.asarray(y, dtype=np.float32), orig_sr=sr, target_sr=22050)
    f0, vflag, vprob = librosa.pyin(y, fmin=80, fmax=900, sr=22050, frame_length=2048)
    f = f0[vflag & (vprob > 0.5)]
    if len(f) < 20:
        return None
    semis = 12 * np.log2(f / np.median(f))
    return {"median_hz": round(float(np.median(f)), 1),
            "range_st": round(float(np.percentile(semis, 90) - np.percentile(semis, 10)), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desc", required=True, help="聲音描述：年紀、音高、個性、語氣起伏，越具體越好")
    ap.add_argument("--text", required=True, help="參考音要念的句子（之後要原封不動交給語音服務）")
    ap.add_argument("--takes", type=int, default=3)
    ap.add_argument("--hz", type=float, default=0, help="目標音高中位數（可省略：不挑，全部留著自己聽）")
    ap.add_argument("--range", type=float, default=10.0, help="目標起伏（半音）")
    ap.add_argument("--out", default="voice_ref")
    a = ap.parse_args()

    t0 = time.time()
    tts = Qwen3TTSModel.from_pretrained(MODEL, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
    print(f"載入 {time.time() - t0:.1f} 秒", flush=True)
    rows = []
    for k in range(1, a.takes + 1):
        t1 = time.time()
        wavs, sr = tts.generate_voice_design(text=a.text, instruct=a.desc, language="Chinese")
        m = measure(wavs[0], sr)
        score = None
        if a.hz and m:
            score = round(abs(12 * np.log2(m["median_hz"] / a.hz)) + 0.5 * abs(m["range_st"] - a.range), 2)
        sf.write(f"{a.out}_{k}.wav", wavs[0], sr)
        rows.append({"take": k, "file": f"{a.out}_{k}.wav", "gen_s": round(time.time() - t1, 1), "pitch": m, "score": score})
        print(rows[-1], flush=True)
    with open(f"{a.out}.json", "w", encoding="utf-8") as fp:
        json.dump({"desc": a.desc, "text": a.text, "takes": rows}, fp, ensure_ascii=False, indent=1)
    if a.hz:
        best = min((r for r in rows if r["score"] is not None), key=lambda r: r["score"], default=None)
        print("最接近目標的是", best and best["file"])


if __name__ == "__main__":
    main()
