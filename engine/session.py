# -*- coding: utf-8 -*-
"""依 call_id 保存一通電話的對話歷史與最近的圖。

為什麼：手機斷線重連（換網路、隧道抖一下）時會沿用同一個 call_id，
協定補充（2026-11-18 01:55）定好伺服器要保留歷史 10 分鐘，重連後接著聊。
"""
import time
import uuid
from collections import deque

import config


class CallSession:
    def __init__(self, call_id: str, now: float):
        self.call_id = call_id
        self.history: list[dict] = []  # 直接給 Claude messages 用
        self.images: deque[bytes] = deque(maxlen=config.MAX_IMAGES)  # 還沒送給 Claude 的 JPEG
        self.last_seen = now
        self.conv_id: str | None = None  # 接哪條 butler 對話（start 帶來的）
        self.briefed = False             # 通話規矩已經送進那條對話了沒


class SessionStore:
    def __init__(self, ttl: float = config.HISTORY_TTL_SEC, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._items: dict[str, CallSession] = {}

    def get(self, call_id: str | None) -> CallSession:
        """拿 call_id 的歷史；沒有或已過期就開新的。沒給 call_id 就給一個新的。"""
        now = self.clock()
        for k in [k for k, s in self._items.items() if now - s.last_seen > self.ttl]:
            del self._items[k]
        call_id = call_id or str(uuid.uuid4())
        s = self._items.get(call_id)
        if s is None:
            s = self._items[call_id] = CallSession(call_id, now)
        s.last_seen = now
        return s

    def touch(self, s: CallSession) -> None:
        s.last_seen = self.clock()
