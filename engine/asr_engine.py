# -*- coding: utf-8 -*-
"""伺服器端語音辨識引擎（Breeze-ASR-25，faster-whisper / CTranslate2）。

為什麼有它：butler 語音通話原本用手機內建語音辨識，使用者說「非常爛根本不能用」，
台灣華語＋中英夾雜（resistor、breadboard、LED）幾乎認不出來。2026-11-18 改成
手機只傳 PCM 上來、由這台 RTX 4060 跑聯發科 Breeze-ASR-25（Whisper large-v2 微調、
專攻台灣華語與中英夾雜）。選型與量測見同資料夾 結果.md。
同日下午他又說「現在的根本不能完全識別對」：比過 Qwen3-ASR、SenseVoice、gpt-4o-transcribe，
結果是 Breeze 加常用詞（hotwords，見 config.ASR_HOTWORDS）最準，所以沒換模型、只加詞。

用法：
    eng = AsrEngine()                      # 載入一次（約數秒、佔 VRAM）
    text = eng.transcribe(pcm16_bytes)      # 16-bit little-endian 單聲道 PCM
模型放 D 槽（C 槽空間不夠），路徑可用環境變數 BUTLER_ASR_MODEL 覆寫。
"""
import os
import threading
from pathlib import Path

import numpy as np


def _add_cuda_dlls() -> None:
    """ctranslate2 要 CUDA 12 的 cublas64_12.dll／cuDNN 9；系統沒裝，用 pip 的 nvidia-* 套件，把它們的 bin 加進 PATH。"""
    import site
    for sp in site.getsitepackages():
        for bin_dir in Path(sp).glob("nvidia/*/bin"):
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
            os.add_dll_directory(str(bin_dir))


_add_cuda_dlls()

import opencc
from faster_whisper import WhisperModel

DEFAULT_MODEL = r"D:\butler-asr\models\breeze-asr-25-ct2"
DEFAULT_COMPUTE = "int8_float16"  # 量測後選定，理由見 結果.md


class AsrEngine:
    def __init__(self, model_dir: str | None = None, compute_type: str | None = None,
                 device: str = "cuda", hotwords: str | None = None):
        if hotwords is None:  # 預設讀 config 的常用詞；給空字串就是不用
            try:
                from config import ASR_HOTWORDS as hotwords
            except ImportError:
                hotwords = ""
        self.hotwords = hotwords
        self.model = WhisperModel(model_dir or os.environ.get("BUTLER_ASR_MODEL", DEFAULT_MODEL),
                                  device=device, compute_type=compute_type or DEFAULT_COMPUTE)
        self._cc = opencc.OpenCC("s2twp")
        self._lock = threading.Lock()  # 同一個模型不要多執行緒同時推論

    def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        """16-bit 單聲道 PCM → 繁體中文文字。強制語言 zh。空音訊回空字串。"""
        audio = np.frombuffer(pcm16_bytes, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size == 0:
            return ""
        if sample_rate != 16000:  # Whisper 只吃 16 kHz；簡單線性內插重取樣
            n = int(round(audio.size * 16000 / sample_rate))
            audio = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio).astype(np.float32)
        with self._lock:
            segs, _ = self.model.transcribe(audio, language="zh", beam_size=5,
                                            condition_on_previous_text=False,
                                            hotwords=self.hotwords or None)
            text = "".join(s.text for s in segs).strip()
        return self._cc.convert(text)


if __name__ == "__main__":
    # 自我測試：python asr_engine.py <16k 單聲道 wav>
    import sys
    import time
    import wave

    with wave.open(sys.argv[1]) as w:
        sr, pcm = w.getframerate(), w.readframes(w.getnframes())
    t = time.perf_counter()
    eng = AsrEngine()
    print("載入", round(time.perf_counter() - t, 2), "秒")
    t = time.perf_counter()
    print(eng.transcribe(pcm, sr), round(time.perf_counter() - t, 2), "秒")
