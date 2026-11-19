"""語音／視訊通話的轉送：手機連 `/v1/call`，這裡驗完 token 就整條轉給本機通話引擎。

為什麼有它（2026-11-18）：使用者要在 App 裡打電話、開視訊給助理。辨識要在顯卡跑 Breeze-ASR-25，
得用 torch／CTranslate2，不能塞進 butler 的 venv；而且通話引擎改了要能自己重啟，
不能每次都重啟 butler（助理就跑在 butler 裡，重啟等於把自己關掉）。所以引擎是獨立服務
`services\\butler-call`（只聽 127.0.0.1:8790），butler 只負責兩件事：
① 驗 device token（跟其他 API 同一套，Cloudflare Access 在更外面已經擋過一層）；
② 把兩個方向的訊息原封不動搬過去。協定見 `說明.md`。

訪客金鑰不給打：引擎的系統提示是「布丁的助理」，而且走的是布丁的 API 額度。
引擎沒在跑時先 accept 再送一則 error 才關，App 才看得到原因，不會只顯示「連不上」。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import auth

log = logging.getLogger("butler.call")

router = APIRouter()

ENGINE_URL = "ws://127.0.0.1:8790/call"
REVERIFY_SEC = 15.0
CALL_PROTO = "butler-call"  # 網頁版的子協定名，跟 webaccess.CALL_PROTO 同一個字


# 下面三個小函式（2026-11-19）：這支原本直接用 webaccess.bearer、auth.verify(token, scope)、auth.guest_name_of，
# 那三個都是這台後來才加的（11-14、11-18），原版 butler-kit 與其他衍生版沒有，公開包照說明裝進去一 import 就失敗。
# 改成自己取 token、有那些函式才用，三邊共用同一份。

def _bearer(scope: dict) -> str:
    """App 帶 Authorization 標頭；瀏覽器的 WebSocket 帶不了，token 放在子協定 bearer.<token>。"""
    headers = scope.get("headers") or []
    for k, v in headers:
        if k == b"authorization":
            return v.decode("latin-1").removeprefix("Bearer ").strip()
    for k, v in headers:
        if k == b"sec-websocket-protocol":
            for p in v.decode("latin-1").split(","):
                p = p.strip()
                if p.startswith("bearer."):
                    return p[len("bearer."):]
    return ""


def _verify(token: str, scope: dict) -> bool:
    """這台的 auth.verify 收 (token, scope)，會順便擋只准本機的裝置；原版只收 token。"""
    try:
        takes_scope = len(inspect.signature(auth.verify).parameters) >= 2
    except (TypeError, ValueError):
        takes_scope = False
    return bool(auth.verify(token, scope) if takes_scope else auth.verify(token))


def _is_guest(token: str) -> bool:
    """訪客金鑰不給打（見檔頭）。沒有訪客金鑰這套的 butler 就沒有訪客。"""
    f = getattr(auth, "guest_name_of", None)
    return bool(f and f(token))


@router.websocket("/v1/call")
async def call(ws: WebSocket) -> None:
    token = _bearer(ws.scope)
    if not _verify(token, ws.scope) or _is_guest(token):
        await ws.close(code=1008)
        return
    # 瀏覽器送了子協定，伺服器沒挑一個回去的話，瀏覽器會直接把連線判失敗
    offered = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol=CALL_PROTO if CALL_PROTO in offered else None)
    try:
        upstream = await connect(ENGINE_URL, max_size=None, open_timeout=5,
                                 ping_interval=None)
    except Exception as e:  # 引擎沒開、埠被占……都一樣：告訴手機再關
        log.warning("通話引擎連不上：%s", e)
        await ws.send_text(json.dumps(
            {"type": "error", "message": "通話引擎沒有在跑，請到電腦上檢查 butler-call"},
            ensure_ascii=False))
        await ws.close(code=1011)
        return

    async def phone_to_engine() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send(msg["text"])

    async def engine_to_phone() -> None:
        async for data in upstream:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_text(data)

    async def still_valid() -> None:
        # 撤銷或到期就斷：握手時驗過不夠，偷到 token 的人連線不斷就能一直講下去（復查 #1，同 SSE 的做法）
        while True:
            await asyncio.sleep(REVERIFY_SEC)
            if not _verify(token, ws.scope):
                log.warning("通話中 token 失效，斷線")
                return

    tasks = [asyncio.create_task(phone_to_engine()), asyncio.create_task(engine_to_phone()),
             asyncio.create_task(still_valid())]
    try:
        # 任一邊斷了整條就結束，另一邊收掉
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
                log.warning("通話轉送中斷：%r", exc)
    finally:
        await upstream.close()
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass  # 手機那邊已經關了（uvicorn 新版回 WebSocketDisconnect，不是 RuntimeError）
