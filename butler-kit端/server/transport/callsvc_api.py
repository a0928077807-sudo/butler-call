"""通話服務的開關：工具頁一顆開關，關掉／開回電腦上的通話引擎（8790）與布丁狗語音（8792）。

為什麼有它（2026-11-19）：布丁玩三角洲很卡，查到顯卡 8GB 用到 7.5GB，其中約 4.5GB 是這兩個
服務（Breeze 辨識＋Qwen3-TTS），記憶體也只剩 0.5GB。他要「隨時能開關的按鈕在工具頁」。

**關的時候要連重啟迴圈一起關。** 兩個服務都是工作排程器起 wscript（start-*.vbs）→ cmd
跑 start-*.bat 的迴圈 → python；只殺 python 的話 bat 迴圈 5～10 秒後就拉回來。11-18 23:58
助理就是只殺了 python、回報「關好了」，23:59 兩個服務又起來，顯存回到 6.8GB 他還在卡。
所以這裡從 wscript 那一層整棵收掉，殘留的 cmd／python（例如手動起的那份）也一併收。

開回來交給工作排程器（schtasks /run），不從這裡直接起：butler 底下起的行程會被
Job Object 綁住（見 AGENTS.md），butler 一重啟就跟著被收；排程器起的不在 Job 裡。

訪客金鑰碰不到：guestaccess 是白名單，這個路徑不在上面。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import subprocess

import psutil
from fastapi import APIRouter, Body, Depends

from .auth import require_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/callsvc", tags=["callsvc"])

# (排程工作名, 埠, 命令列裡認得出它的字串)
SERVICES = (
    ("butler-call", 8790, ("start-call.vbs", "start-call.bat", "butler-call\\server.py")),
    ("purin-tts", 8792, ("start-purin-tts.vbs", "start-purin-tts.bat", "purin_tts_server.py")),
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


# 只認這三種：別的程式命令列剛好含這些字（編輯器開著 bat、助理在 grep）不能被誤殺
_KINDS = {"wscript.exe", "cmd.exe", "python.exe"}


def _procs(marks: tuple[str, ...]) -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() not in _KINDS:
                continue
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if any(m.lower() in cmd.lower() for m in marks):
            out.append(p)
    return out


def status() -> dict:
    """每個服務：ready＝埠在聽、alive＝還有它的行程在。整體 state：on／off／starting。"""
    items = []
    for task, port, marks in SERVICES:
        items.append({"task": task, "port": port, "ready": _listening(port),
                      "alive": bool(_procs(marks))})
    if all(i["ready"] for i in items):
        state = "on"
    elif not any(i["alive"] or i["ready"] for i in items):
        state = "off"
    else:
        state = "starting"
    return {"state": state, "services": items}


def stop_all() -> int:
    """整棵收掉（含重啟迴圈），回收了幾支行程。"""
    victims: dict[int, psutil.Process] = {}
    for _task, _port, marks in SERVICES:
        for p in _procs(marks):
            victims[p.pid] = p
            try:
                for c in p.children(recursive=True):
                    victims[c.pid] = c
            except psutil.Error:
                pass
    for p in victims.values():
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(list(victims.values()), timeout=5)
    log.info("通話服務關閉：收掉 %d 支行程", len(victims))
    return len(victims)


def start_all() -> list[str]:
    """交給工作排程器開回來；回傳開不起來的工作名。"""
    failed = []
    for task, port, _marks in SERVICES:
        if _listening(port):
            continue
        r = subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0:
            failed.append(task)
            log.warning("schtasks /run %s 失敗：%s", task, r.stderr.decode("cp950", "ignore")[:200])
    return failed


@router.get("")
async def get_status(_t: str = Depends(require_token)) -> dict:
    return await asyncio.to_thread(status)


@router.post("")
async def set_power(body: dict = Body(...), _t: str = Depends(require_token)) -> dict:
    """{"on": true|false}。開的話模型要載入，約半分鐘後 state 才會變 on。"""
    on = bool(body.get("on"))
    if on:
        failed = await asyncio.to_thread(start_all)
        res = await asyncio.to_thread(status)
        if failed:
            res["error"] = "排程工作開不起來：" + "、".join(failed)
        return res
    await asyncio.to_thread(stop_all)
    return await asyncio.to_thread(status)
