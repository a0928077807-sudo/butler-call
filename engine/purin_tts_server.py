# -*- coding: utf-8 -*-
"""布丁狗語音小服務：收一句中文，邊合成邊吐 24 kHz／16 位元／單聲道 PCM。

為什麼有它：2026-11-18 使用者試過 edge-tts、OpenAI、WORLD 卡通化都說不像卡通人物，
最後挑定 Qwen3-TTS VoiceDesign 生出來的「日系吉祥物」聲音（第二輪 4 號）。原版 VoiceDesign
在 RTX 4060 上生 1 秒聲音要 6 秒，通話用不了；改用 faster-qwen3-tts（CUDA graph）讓
0.6B Base 模型照 4 號念，量到 RTF 0.7～0.8、第一段約 0.7 秒，使用者聽過說「像」（14:24）。
參考音是模型生的聲音，不是真人錄音。

為什麼獨立一個行程：faster-qwen3-tts 要 transformers 5.15（引擎那邊的 Breeze 環境是 4.x），
裝在同一個 venv 會互相打架，所以另開 D:\\qwen-tts\\venv-fast，跟引擎用 HTTP 講話。
只綁 127.0.0.1；模型載入、暖機完才開始聽，所以引擎連不上＝還沒好，會自己退回 edge-tts。
一次只合成一句（顯卡只有一張），用戶端斷線就停止這句。

介面：
  GET  /health          → 200 "ok"
  POST /tts  {"text"}   → 200，chunked，內容是 PCM s16le 24 kHz mono
跑法：D:\\qwen-tts\\venv-fast\\Scripts\\python.exe purin_tts_server.py（平常由 start-purin-tts.vbs 起）
"""
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("HF_HOME", r"D:\qwen-tts\hf")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np  # noqa: E402
import torch  # noqa: E402
from faster_qwen3_tts import FasterQwen3TTS  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("PURIN_TTS_PORT", "8792"))
# 11-18 15:51 他選「換大模型試試看」：用新辨識（Breeze＋常用詞）驗字，0.6B 錯 9.4%、1.7B（temperature 0.5）錯 2.2%
# （_中間檔/butler-call/score_purin_variants.py）。代價：聲音較低、RTF 約 0.8～1.0、顯存峰值約 4.5GB。
# 11-18 18:35 他選「換回小模型」：通話裡 1.7B 長句音調掉到像普通 AI（18:22 原話「不像布丁狗」）。
# sweep_temp.py 量長句音高中位數：1.7B t0.3／0.5／0.7＝320／353／316 Hz，0.6B t0.5＝436（參考音 545），調溫度救不了 1.7B。
MODEL = os.environ.get("PURIN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
TEMPERATURE = float(os.environ.get("PURIN_TTS_TEMP", "0.5"))  # 0.9（預設）錯字多，量過 0.5 較準
REF_WAV = os.environ.get("PURIN_REF_WAV", str(BASE_DIR / "voice" / "purin_ref4.wav"))  # 換聲音：換這個檔（見說明.md）
REF_TEXT = os.environ.get("PURIN_REF_TEXT", "哇！布丁你回來啦～嘿嘿，我等你好久了耶。咦？這個是什麼呀？好厲害喔！")  # 參考音裡一字不差的原文
OUT_RATE = 24000
CHUNK_STEPS = 8          # 每 8 個編碼步（約 0.67 秒聲音）吐一次
MAX_TEXT = 200

log = logging.getLogger("purin_tts")
_gpu = threading.Lock()
_tts = None


def _to_pcm16(audio, sr: int) -> bytes:
    y = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sr != OUT_RATE and len(y):
        n = int(round(len(y) * OUT_RATE / sr))
        y = np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y).astype(np.float32)
    return (np.clip(y, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def synth(text: str):
    """產生 PCM 片段；呼叫端停止迭代（close）時生成器跟著結束。"""
    gen = _tts.generate_voice_clone_streaming(text=text, language="Chinese", ref_audio=REF_WAV,
                                              ref_text=REF_TEXT, chunk_size=CHUNK_STEPS,
                                              temperature=TEMPERATURE)
    try:
        for item in gen:
            yield _to_pcm16(item[0], item[1])
    finally:
        gen.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 不要每個請求都印一行到 stderr
        pass

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/tts":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            text = str(json.loads(self.rfile.read(n) or b"{}").get("text", "")).strip()[:MAX_TEXT]
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "bad json")
            return
        if not text:
            self.send_error(400, "empty text")
            return
        t0 = time.perf_counter()
        first = None
        total = 0
        with _gpu:
            self.send_response(200)
            self.send_header("Content-Type", f"audio/L16; rate={OUT_RATE}; channels=1")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            gen = synth(text)
            try:
                for pcm in gen:
                    if not pcm:
                        continue
                    if first is None:
                        first = time.perf_counter() - t0
                    self.wfile.write(f"{len(pcm):X}\r\n".encode() + pcm + b"\r\n")
                    self.wfile.flush()
                    total += len(pcm)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (ConnectionError, OSError):
                log.info("用戶端斷線，停止這句：%s", text[:30])
                self.close_connection = True
            except Exception:
                log.exception("合成失敗：%s", text[:30])
                self.close_connection = True
            finally:
                gen.close()
        dur = total / 2 / OUT_RATE
        log.info("%.2fs 聲音，第一段 %d ms，總共 %d ms：%s", dur, (first or 0) * 1000,
                 (time.perf_counter() - t0) * 1000, text[:30])


def main():
    global _tts
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    t0 = time.time()
    _tts = FasterQwen3TTS.from_pretrained(MODEL, device="cuda", dtype=torch.bfloat16)
    # 第一次會擷取 CUDA graph 並把參考音編碼進快取；暖機完才開埠
    for _ in synth("你好。"):
        pass
    log.info("模型就緒 %.1fs，顯存 %.2f GB，聽 %s:%d", time.time() - t0,
             torch.cuda.memory_allocated() / 2**30, HOST, PORT)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
