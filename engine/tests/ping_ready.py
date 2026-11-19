# -*- coding: utf-8 -*-
"""最小連線檢查：連正在跑的引擎（預設 8790）送 start、等 ready、送 hangup。不打 Claude、不出聲。

為什麼有它：開機自啟後要確認 8790 真的能通話，e2e 會另外起一份引擎，查不到正式那份。
用法：D:\\butler-asr\\venv\\Scripts\\python.exe tests\\ping_ready.py [埠]
成功 exit 0，失敗 exit 1。
"""
import asyncio
import json
import sys

import websockets


async def main(port: int) -> int:
    async with websockets.connect(f"ws://127.0.0.1:{port}/call") as ws:
        await ws.send(json.dumps({"type": "start", "mode": "voice", "call_id": "ping-ready"}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
        await ws.send(json.dumps({"type": "hangup"}))
    print("收到：", msg)
    return 0 if msg.get("type") == "ready" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8790)))
    except Exception as e:
        print("失敗：", type(e).__name__, e)
        sys.exit(1)
