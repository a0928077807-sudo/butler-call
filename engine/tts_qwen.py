# -*- coding: utf-8 -*-
"""布丁狗聲音：向本機的 purin_tts_server.py 要 PCM，跟 EdgeTts 同一個 `stream(text)` 介面。

為什麼有它：2026-11-18 14:24 使用者挑定 Qwen3-TTS 照第二輪 4 號念的聲音（A 像）。
那個模型要另一個 Python 環境，所以合成放在獨立行程（8792），這裡只負責串流取回。

退路：小服務沒開、還在載入、或這句還沒吐出任何聲音就出錯，這句改用 edge-tts 念，
通話不會變成沒聲音；已經念到一半才斷就停在那裡，不再從頭用別的聲音重念。
"""
import logging

import httpx

import config

log = logging.getLogger("butler_call")


class QwenTts:
    def __init__(self, fallback, url: str = config.PURIN_TTS_URL):
        self.fallback, self.url = fallback, url.rstrip("/")
        # 連線逾時短：沒開的話馬上退回；讀取逾時給長一點，長句第一段可能要一秒多
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=1.0, read=15.0, write=5.0, pool=5.0))

    async def stream(self, text: str):
        got = False
        carry = b""
        try:
            async with self._client.stream("POST", f"{self.url}/tts", json={"text": text}) as r:
                if r.status_code != 200:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                async for b in r.aiter_bytes():
                    b = carry + b
                    cut = len(b) - (len(b) % 2)   # 16 位元取樣不能從中間切開
                    carry = b[cut:]
                    if cut:
                        got = True
                        yield b[:cut]
            return
        except (httpx.HTTPError, OSError) as e:
            if got:
                log.warning("布丁狗語音念到一半斷了（%s）：%s", type(e).__name__, text[:30])
                return
            log.warning("布丁狗語音連不上，這句改用 edge-tts（%s）", type(e).__name__)
        async for b in self.fallback.stream(text):
            yield b
