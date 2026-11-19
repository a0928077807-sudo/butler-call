"""通話服務開關 /v1/callsvc：狀態判斷、連重啟迴圈一起關、開不起來要回報、沒 token 進不來。
直接用 python 跑這支（腳本風格，見 AGENTS.md）。不碰真的 8790／8792：用假的「迴圈→服務」兩層 python 代替，
真的服務要吃顯卡，測試不該去開它。"""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401  （先載入：tests/ 底下會自動指到暫存資料目錄）
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from transport import auth, callsvc_api  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, info: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({info})" if info and not cond else ""))
    if not cond:
        FAILED.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 假服務：聽一個埠。假迴圈：像 start-*.bat 一樣，服務死了 1 秒後再起一份。
SERVICE = """import socket,sys,time
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); time.sleep(600)
"""
LOOP = """import subprocess,sys,time
while True:
    subprocess.run([sys.executable, sys.argv[1], sys.argv[2]])
    time.sleep(1)
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    svc, loop = tmp / "fake_callsvc_service.py", tmp / "fake_callsvc_loop.py"
    svc.write_text(SERVICE, encoding="utf-8")
    loop.write_text(LOOP, encoding="utf-8")
    port = free_port()
    callsvc_api.SERVICES = (("butlerkit-test-no-such-task", port,
                             ("fake_callsvc_loop.py", "fake_callsvc_service.py")),)

    print("關著的時候")
    check("沒行程、埠沒開 → off", callsvc_api.status()["state"] == "off")

    print("開著（迴圈＋服務）")
    lp = subprocess.Popen([sys.executable, str(loop), str(svc), str(port)],
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(50):
        if callsvc_api._listening(port):
            break
        time.sleep(0.1)
    check("埠在聽 → on", callsvc_api.status()["state"] == "on")

    print("關掉")
    n = callsvc_api.stop_all()
    check("迴圈和服務都收掉（至少兩支）", n >= 2, f"收了 {n}")
    time.sleep(2.5)  # 迴圈若還活著，1 秒後會再起一份
    st = callsvc_api.status()
    check("2.5 秒後沒被迴圈拉回來 → off", st["state"] == "off", str(st))
    check("迴圈行程真的死了", lp.poll() is not None)

    print("只剩行程、埠還沒開 → starting")
    sp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "fake_callsvc_loop.py"],
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(0.5)
    check("starting", callsvc_api.status()["state"] == "starting")
    callsvc_api.stop_all()
    check("stop_all 收得掉它", sp.wait(timeout=5) is not None)

    print("開不起來要回報")
    failed = callsvc_api.start_all()
    check("排程工作不存在 → 回報名字", failed == ["butlerkit-test-no-such-task"], str(failed))

    print("路由")
    app = FastAPI()
    app.include_router(callsvc_api.router)
    c = TestClient(app)
    check("沒 token → 401", c.get("/v1/callsvc").status_code == 401)
    token, _ = auth.ensure_token()
    h = {"Authorization": f"Bearer {token}"}
    r = c.get("/v1/callsvc", headers=h)
    check("有 token → 200 且有 state", r.status_code == 200 and r.json().get("state") == "off", r.text)
    r = c.post("/v1/callsvc", headers=h, json={"on": True})
    check("開不起來 → 回 error", "error" in r.json(), r.text)
    r = c.post("/v1/callsvc", headers=h, json={"on": False})
    check("關 → 200 off", r.status_code == 200 and r.json()["state"] == "off", r.text)

    print("訪客")
    from transport import guestaccess
    for m in ("GET", "POST"):
        r = guestaccess.check("someone", m, "/v1/callsvc")
        check(f"訪客 {m} /v1/callsvc → 403", r is not None and r[0] == 403, str(r))

    print("FAILED", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
