# -*- coding: utf-8 -*-
"""butler 通話引擎：手機 App 打電話／視訊給助理時，伺服器這端的耳朵、嘴巴和腦。

為什麼有它：2026-11-18 使用者要在 butler App 裡直接打電話、開視訊給助理（開口講、AI 用語音回，
視訊時 AI 看鏡頭教他動手）。手機內建的語音辨識他說「非常爛根本不能用」；Claude 也沒有即時語音
API，所以在本機自己串一條：Silero VAD 判斷講完 → Breeze-ASR-25 辨識 → Claude 串流回覆 →
切句 → 布丁狗語音（purin_tts_server.py，連不上退回 edge-tts）→ 24 kHz PCM 串流回手機。做成獨立服務而不是塞進 butler，是因為辨識要
CTranslate2／CUDA（不能進 butler 的 venv），而且通話引擎要能自己重啟、不必動到 butler。

只聽 127.0.0.1:8790 的 WebSocket /call；驗證由 butler 的 /v1/call 做完才轉送進來。
協定 v1 見 說明.md。

用法：D:\\butler-asr\\venv\\Scripts\\python.exe server.py [--port 8791]
"""
import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import config
from butler_brain import ButlerBrain
from call_handler import CallHandler
from session import SessionStore
from tts import EdgeTts
from tts_qwen import QwenTts
from vad import StreamVad

log = logging.getLogger("butler_call")


def setup_log() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(config.LOG_DIR / "call.log", maxBytes=1_000_000,
                                             backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from asr_engine import AsrEngine  # 延後載入：要 CUDA，單元測試不必碰
    asr = AsrEngine()
    asr.transcribe(np.zeros(8000, dtype="<i2").tobytes())  # 暖機，第一次推論比較慢
    state.update(asr=asr, brain=ButlerBrain(config.BUTLER_TOKEN_FILE.read_text(encoding="utf-8").strip()), tts=QwenTts(fallback=EdgeTts()), store=SessionStore(),
                 executor=ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr"))
    log.info("引擎啟動，大腦＝butler %s，埠 %d", state["brain"].base, config.PORT)
    yield
    state["executor"].shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


@app.websocket("/call")
async def call_ws(ws: WebSocket):
    await ws.accept()
    h = CallHandler(ws.send_text, ws.send_bytes, state["store"], state["asr"].transcribe,
                    state["brain"], state["tts"], StreamVad(), log, state["executor"])
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await h.on_binary(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    obj = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(obj, dict) and await h.on_json(obj) == "hangup":
                    await ws.close()
                    break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await h.close()
        log.info("連線結束")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args()
    config.PORT = args.port
    setup_log()
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=args.port, log_level="warning", ws_max_size=8 * 1024 * 1024)


if __name__ == "__main__":
    sys.exit(main())
