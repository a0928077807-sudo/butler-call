"""通話轉送 /v1/call：驗 token、擋訪客、雙向原樣轉送、引擎沒開時回 error。
直接用 python 跑這支（腳本風格，見 AGENTS.md）。上游用一支假的 echo 引擎，不碰真的 8790。"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401  （先載入：tests/ 底下會自動指到暫存資料目錄）
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from transport import auth, call_proxy  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, info: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILED.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_echo(port: int) -> None:
    """假引擎：文字加前綴回傳，二進位原樣回傳。"""
    async def handler(conn):
        async for m in conn:
            await conn.send(m if isinstance(m, bytes) else "echo:" + m)

    async def main():
        async with serve(handler, "127.0.0.1", port):
            await asyncio.Future()

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()


app = FastAPI()
app.include_router(call_proxy.router)
auth.verify = lambda token, scope=None: token in ("good", "guest")
auth.guest_name_of = lambda token: "訪客" if token == "guest" else ""

port = free_port()
start_echo(port)
call_proxy.ENGINE_URL = f"ws://127.0.0.1:{port}"
client = TestClient(app)

print("沒帶 token／壞 token 被拒")
for tok in ("", "bad"):
    try:
        with client.websocket_connect("/v1/call", headers={"Authorization": f"Bearer {tok}"}):
            check(f"token={tok!r} 被拒", False)
    except WebSocketDisconnect as e:
        check(f"token={tok!r} 被拒", e.code == 1008, str(e.code))

print("訪客金鑰被拒")
try:
    with client.websocket_connect("/v1/call", headers={"Authorization": "Bearer guest"}):
        check("訪客被拒", False)
except WebSocketDisconnect as e:
    check("訪客被拒", e.code == 1008, str(e.code))

print("雙向轉送")
import time  # noqa: E402
time.sleep(0.3)  # 等假引擎起來
with client.websocket_connect("/v1/call", headers={"Authorization": "Bearer good"}) as ws:
    ws.send_text('{"type":"ping"}')
    check("文字轉過去又轉回來", ws.receive_text() == 'echo:{"type":"ping"}')
    pcm = b"\x01" + bytes(range(256)) * 20
    ws.send_bytes(pcm)
    check("二進位原樣來回", ws.receive_bytes() == pcm)

print("引擎沒開")
call_proxy.ENGINE_URL = f"ws://127.0.0.1:{free_port()}"
with client.websocket_connect("/v1/call", headers={"Authorization": "Bearer good"}) as ws:
    msg = json.loads(ws.receive_text())
    check("收到 error 訊息", msg.get("type") == "error" and "引擎" in msg.get("message", ""), str(msg))

print("網頁版：token 放子協定、經 WebGate 的 /web/api/v1/call（2026-11-18）")
from transport import webaccess  # noqa: E402

call_proxy.ENGINE_URL = f"ws://127.0.0.1:{port}"
auth.verify = lambda token, scope=None: token in ("good", "guest", "webtok")
auth.device_of = lambda t: {"kind": "web"} if t == "webtok" else ({"kind": "app"} if t == "good" else None)
app2 = FastAPI()
app2.include_router(call_proxy.router)
app2.add_middleware(webaccess.WebGate)
c2 = TestClient(app2)
with c2.websocket_connect("/web/api/v1/call", subprotocols=["butler-call", "bearer.webtok"]) as ws:
    check("網頁 token 走子協定連得上", ws.accepted_subprotocol == "butler-call", str(ws.accepted_subprotocol))
    ws.send_text('{"type":"ping"}')
    check("網頁路徑一樣雙向轉送", ws.receive_text() == 'echo:{"type":"ping"}')
with c2.websocket_connect("/v1/call", headers={"Authorization": "Bearer good"}) as ws:
    check("App 不帶子協定照舊連得上", ws.accepted_subprotocol is None, str(ws.accepted_subprotocol))
for name, path, kw in (
    ("網頁路徑用 App token 被拒", "/web/api/v1/call", {"headers": {"Authorization": "Bearer good"}}),
    ("網頁路徑沒帶 token 被拒", "/web/api/v1/call", {"subprotocols": ["butler-call"]}),
    ("網頁路徑別的 WebSocket 被拒", "/web/api/v1/other", {"subprotocols": ["butler-call", "bearer.webtok"]}),
    ("子協定壞 token 被拒", "/web/api/v1/call", {"subprotocols": ["butler-call", "bearer.nope"]}),
):
    try:
        with c2.websocket_connect(path, **kw):
            check(name, False)
    except WebSocketDisconnect as e:
        check(name, e.code == 1008, str(e.code))

print("通話中 token 被撤銷就斷線（復查 #1）")
call_proxy.REVERIFY_SEC = 0.3
alive = {"ok": True}
auth.verify = lambda token, scope=None: token == "good" and alive["ok"]
with client.websocket_connect("/v1/call", headers={"Authorization": "Bearer good"}) as ws:
    ws.send_text('{"type":"ping"}')
    check("撤銷前照常轉送", ws.receive_text() == 'echo:{"type":"ping"}')
    alive["ok"] = False
    try:
        ws.receive_text()
        check("撤銷後被斷線", False, "還收得到東西")
    except WebSocketDisconnect:
        check("撤銷後被斷線", True)

print()
if FAILED:
    print(f"失敗 {len(FAILED)} 項：{FAILED}")
    sys.exit(1)
print("全部通過")
