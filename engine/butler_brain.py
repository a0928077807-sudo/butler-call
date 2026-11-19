# -*- coding: utf-8 -*-
"""通話的大腦改成「butler 那條對話裡的助理本人」：辨識出的句子送進對話，回覆的字串流回來念。

為什麼有它（2026-11-18）：第一版直接打 Claude API，快但是接電話的不是他平常那個助理——
沒有記憶、不知道他在做什麼（10:29「跟我講電話的agent不是你他沒有記憶問什麼都不知道」）。
他要從哪條對話打電話，就由那條對話的助理接（10:35，例子是焊接時直接在那條對話打）。
原本以為 butler 每句要十幾秒，實測空對話首字 1～3 秒、整句 2～4 秒（任務紀錄 10:5x），就改走這裡。

介面跟 brain.ClaudeBrain 一樣是 `await stream(messages, on_text, call=CallSession)`，
call_handler 不用知道背後是誰。butler 的 API：POST /v1/conversations/<id>/message 送出，
GET /v1/stream（SSE，事件型別在 event 欄、data 攤平）收 text.delta，turn.done 收工。
"""
import asyncio
import base64
import json
import logging
import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx
from httpx_sse import aconnect_sse

import config

log = logging.getLogger("butler_call")

# 每通電話第一句附上的規矩；之後每句只帶「〔通話〕」，助理的前文裡已經有這段。
CALL_RULE = (
    "〔通話〕（他正在用 App 跟你講電話，這句是語音辨識轉出來的文字，可能有同音錯字。"
    "你的回覆會用語音念給他聽：用口語短句、一兩句就好；不要用 markdown、表格、程式碼、網址、"
    "數學式或圖表，也不要用 ASK 按鈕，要他選就直接用講的。視訊時附的圖是他鏡頭當下的畫面。"
    "要在電腦上做比較久的事，先講一句你要去做什麼再動手。通話畫面上有布丁狗，想讓牠演動作就照常在回覆最後加動作標記，"
    "不會被念出來。這通電話後面的訊息都以〔通話〕開頭。）\n"
)
CALL_TAG = "〔通話〕"

# 念不出來或不該念的：[[DONE]] 之類的標記、``` 程式碼區塊整段丟；markdown 符號逐字丟
_BLOCKS = re.compile(r"\[\[.*?\]\]|```.*?```", re.S)
_DROP = str.maketrans("", "", "*`#$|\\_~>")


class SpeechFilter:
    """把助理的串流回覆濾成可以念的字。逐段餵進來，回新增、已經確定可以念的部分。

    做法：累積原文，每次整段重洗一次，跟上次洗出來的比，多出來的才吐出去。
    沒收尾的「[[」「```」從那裡切掉先不吐，尾巴單獨的「[」「`」也先扣著（可能是標記的開頭）。
    """

    def __init__(self):
        self.raw = ""
        self.done = ""

    @staticmethod
    def clean(raw: str) -> str:
        s = _BLOCKS.sub("", raw)
        for opener in ("[[", "```"):
            k = s.find(opener)
            if k >= 0:
                s = s[:k]
        while s.endswith(("[", "`")):
            s = s[:-1]
        return s.translate(_DROP)

    def feed(self, delta: str) -> str:
        self.raw += delta
        cur = self.clean(self.raw)
        if not cur.startswith(self.done):  # 理論上不會發生；發生就不吐，免得念重複
            self.done = cur
            return ""
        new, self.done = cur[len(self.done):], cur
        return new


def find_butler_base() -> str:
    """butler 綁在 Tailscale 位址上（不是 127.0.0.1），從 netstat 找誰在聽 47362。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] == "LISTENING" and parts[1].endswith(f":{config.BUTLER_PORT}"):
                host = parts[1].rsplit(":", 1)[0]
                if host not in ("0.0.0.0", "[::]"):
                    return f"http://{host}:{config.BUTLER_PORT}"
                return f"http://127.0.0.1:{config.BUTLER_PORT}"
    except Exception as e:
        log.warning("找 butler 位址失敗：%r", e)
    return config.BUTLER_FALLBACK


def last_user(messages: list[dict]) -> tuple[str, bytes | None]:
    """取最後一則 user 訊息的文字與最後一張圖（JPEG bytes）。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c, None
        text, img = "", None
        for b in c or []:
            if b.get("type") == "text":
                text += b.get("text", "")
            elif b.get("type") == "image":
                img = base64.b64decode(b["source"]["data"])
        return text, img
    return "", None


class ButlerError(Exception):
    pass


class ButlerBrain:
    def __init__(self, token: str, base: str | None = None, upload_dir: Path = config.BUTLER_UPLOAD_DIR):
        self.token, self.upload_dir = token, upload_dir
        self.base = base or find_butler_base()
        self.last_usage = ""

    def _save_image(self, jpeg: bytes) -> dict:
        """存進 butler 的上傳目錄（伺服器只收那底下的附件），當附件送。"""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        p = self.upload_dir / f"call-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.jpg"
        p.write_bytes(jpeg)
        return {"name": "鏡頭畫面.jpg", "path": str(p), "bytes": len(jpeg), "mime": "image/jpeg"}

    async def stream(self, messages: list[dict], on_text, call=None, on_act=None) -> tuple[str | None, bool]:
        conv = (getattr(call, "conv_id", None) or config.DEFAULT_CONV)
        text, img = last_user(messages)
        briefed = bool(getattr(call, "briefed", False))
        body = {"text": (CALL_TAG if briefed else CALL_RULE) + text, "client": "phone"}
        if img:
            body["attachments"] = [self._save_image(img)]
        headers = {"Authorization": f"Bearer {self.token}"}
        filt, spoken = SpeechFilter(), []
        t0, why = time.perf_counter(), ""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None)) as c:
                # 先接上事件流再送出，回覆的第一個字才不會漏掉
                async with aconnect_sse(c, "GET", f"{self.base}/v1/stream", headers=headers) as es:
                    r = await c.post(f"{self.base}/v1/conversations/{conv}/message", json=body,
                                     headers=headers)
                    r.raise_for_status()
                    if call is not None:
                        call.briefed = True
                    why = await self._pump(es, conv, filt, spoken, on_text, on_act)
        except httpx.HTTPError as e:
            raise ButlerError(f"butler 連不上（{type(e).__name__}）") from e
        self.last_usage = f"對話 {conv}｜收工原因 {why}｜整輪 {time.perf_counter() - t0:.1f} 秒"
        return "".join(spoken) or None, False

    async def _pump(self, es, conv: str, filt: SpeechFilter, spoken: list[str], on_text, on_act=None) -> str:
        """讀事件直到這一輪可以交還麥克風。回一段給 log 看的說明。

        收工條件：turn.done；或已經講過話、接著開始用工具（去電腦上做事了，不讓他乾等）；
        或太久沒說話（額度閘、卡住）——講一句再交還，回覆之後還是會留在對話裡。
        """
        t0 = time.perf_counter()
        it = es.aiter_sse().__aiter__()
        while True:
            limit = config.BUTLER_SILENT_LIMIT_SEC if not spoken else config.BUTLER_TURN_LIMIT_SEC
            left = limit - (time.perf_counter() - t0)
            try:
                sse = await asyncio.wait_for(it.__anext__(), timeout=max(left, 0.1))
            except (asyncio.TimeoutError, StopAsyncIteration):
                msg = "我還在電腦上處理，好了會寫在對話裡，你也可以再問我一次。"
                spoken.append(msg)
                await on_text(msg)
                return "逾時"
            if not sse.data or not sse.event:
                continue
            try:
                ev = json.loads(sse.data)
            except ValueError:
                continue
            if ev.get("conv_id") != conv:
                continue
            t = sse.event
            if t == "text.delta":
                s = filt.feed(ev.get("d", ""))
                if s:
                    spoken.append(s)
                    await on_text(s)
            elif t == "pet.act":
                # 助理挑的布丁狗動作（[[動作:名稱]]，butler 已轉成 clip id，在 turn.done 之前發）：轉給通話畫面演
                if on_act is not None and ev.get("name"):
                    await on_act(str(ev["name"]))
            elif t == "tool.call" and "".join(spoken).strip():
                return "去用工具了"
            elif t == "turn.done":
                return "turn.done"
            elif t == "error":
                raise ButlerError(str(ev.get("detail") or ev.get("kind") or "butler 回報錯誤")[:200])
            elif t == "status" and ev.get("phase") == "waiting":
                msg = "額度用完了，要等重置才能回你。"
                spoken.append(msg)
                await on_text(msg)
                return "額度閘"
