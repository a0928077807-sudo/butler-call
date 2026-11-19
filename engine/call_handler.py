# -*- coding: utf-8 -*-
"""一條 /call WebSocket 連線的通話邏輯（協定 v1，見任務紀錄 2026-11-18_butler語音視訊通話.md）。

不認識 HTTP：送訊息靠建構時給的 send_text／send_bytes，辨識、大腦、合成、VAD 也都從外面給，
所以單元測試能全部換成假的，不必載入顯卡模型或打 API。

輪次：每條連線從 0 起，每講完一句用一個新號碼（下一句一定比被打斷的大，等於「打斷時 +1」）。
半雙工：VAD 判斷講完 → 到收到該輪 playback_done 或 interrupt 之前，進來的音訊直接丟、VAD 重置。
"""
import asyncio
import base64
import json
import logging
import time

import anthropic

import config
from butler_brain import ButlerError

ENDERS = set("。！？!?\n")
COMMAS = set("，,、；;：:")


class SentenceSplitter:
    """Claude 串流文字切成一句一句丟 TTS：句號／問號／驚嘆號／換行切；逗號在累積夠長時也切。

    每輪第一段的逗號門檻比較低（first_min）：第一包語音要等第一段切出來才開始合成，
    原本要等 12 字或句號，Claude 首字到第一包語音中間卡 1.5 秒（2026-11-18 第三棒實測）。
    每輪開一個新的 SentenceSplitter，所以「第一段」就是這個物件切出的第一段。
    """

    def __init__(self, comma_min: int = config.SPLIT_COMMA_MIN_CHARS,
                 first_min: int = config.SPLIT_FIRST_MIN_CHARS):
        self.comma_min, self.first_min, self.buf = comma_min, first_min, ""
        self.first = True

    def feed(self, text: str) -> list[str]:
        out = []
        for ch in text:
            self.buf += ch
            need = self.first_min if self.first else self.comma_min
            if ch in ENDERS or (ch in COMMAS and len(self.buf[:-1].strip()) >= need):
                s, self.buf = self.buf.strip(), ""
                if s:
                    out.append(s)
                    self.first = False
        return out

    def flush(self) -> list[str]:
        s, self.buf = self.buf.strip(), ""
        return [s] if s else []


def encode_tts_packet(turn: int, pcm: bytes) -> bytes:
    """0x11＋4 位元組大端輪次＋PCM16LE 24 kHz。"""
    return b"\x11" + turn.to_bytes(4, "big") + pcm


def speakable(s: str) -> bool:
    """只有標點的句子 edge-tts 會回「沒有音訊」錯誤，先擋掉。"""
    return any(c.isalnum() for c in s)


class CallHandler:
    def __init__(self, send_text, send_bytes, store, asr, brain, tts, vad, log: logging.Logger,
                 executor=None):
        self._send_text_raw, self._send_bytes_raw = send_text, send_bytes
        self.store, self.asr, self.brain, self.tts, self.vad = store, asr, brain, tts, vad
        self.log, self.executor = log, executor
        self.session = None
        self.mode = "voice"
        self.muted = False
        self.next_turn = 0
        self.busy_turn: int | None = None  # 不是 None ＝ 思考中／說話中，音訊丟掉
        self.task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- 送出（同一條 WebSocket 不准兩個協程同時寫） ----
    async def _send_json(self, obj: dict, turn: int | None = None) -> None:
        if turn is not None and turn != self.busy_turn:
            return  # 這輪已被打斷
        async with self._lock:
            await self._send_text_raw(json.dumps(obj, ensure_ascii=False))

    async def _send_audio(self, turn: int, pcm: bytes) -> None:
        if turn != self.busy_turn:
            return
        async with self._lock:
            await self._send_bytes_raw(encode_tts_packet(turn, pcm))

    # ---- 收到 ----
    async def on_binary(self, data: bytes) -> None:
        if not data:
            return
        kind, body = data[0], data[1:]
        if kind == 0x01:
            self.on_audio(body)
        elif kind == 0x02:
            if self.session is not None:
                self.session.images.append(body)
            await self._send_json({"type": "image_ack"})

    def on_audio(self, pcm: bytes) -> None:
        if self.session is None or self.muted or self.busy_turn is not None:
            return  # 半雙工：思考／說話中的音訊丟掉
        utt = self.vad.feed(pcm)
        if utt is not None:
            self._start_turn(utt)

    async def on_json(self, msg: dict) -> str | None:
        t = msg.get("type")
        if t == "start":
            self.session = self.store.get(msg.get("call_id"))
            self.mode = msg.get("mode") or "voice"
            conv = str(msg.get("conv_id") or "").strip() or config.DEFAULT_CONV
            if self.session.conv_id != conv:  # 換了對話：規矩要再送一次
                self.session.conv_id, self.session.briefed = conv, False
            self.vad.reset()
            self.log.info("start call_id=%s mode=%s 對話=%s 歷史=%d 則", self.session.call_id, self.mode,
                          self.session.conv_id, len(self.session.history))
            await self._send_json({"type": "ready"})
        elif t == "ping":
            await self._send_json({"type": "pong"})
        elif t == "interrupt":
            self.interrupt()
        elif t == "playback_done":
            turn = msg.get("turn")
            if self.busy_turn is not None and (turn is None or turn == self.busy_turn) \
                    and (self.task is None or self.task.done()):
                self._back_to_listening()
            # 其他（多的、舊輪次的、還沒講完就到的）一律忽略
        elif t == "mute":
            self.muted = bool(msg.get("on"))
            self.vad.reset()
        elif t == "hangup":
            return "hangup"
        return None

    def interrupt(self) -> None:
        turn = self.busy_turn
        self._back_to_listening()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if turn is not None:
            self.log.info("turn=%d 被打斷", turn)

    def _back_to_listening(self) -> None:
        self.busy_turn = None
        self.vad.reset()

    async def close(self) -> None:
        self.interrupt()
        if self.task is not None:
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        if self.session is not None:
            self.store.touch(self.session)

    # ---- 一輪 ----
    def _start_turn(self, pcm: bytes) -> None:
        turn = self.next_turn
        self.next_turn += 1
        self.busy_turn = turn
        self.vad.reset()
        self.task = asyncio.create_task(self._run_turn(turn, pcm, time.perf_counter()))

    async def _run_turn(self, turn: int, pcm: bytes, t0: float) -> None:
        loop = asyncio.get_running_loop()
        hist = self.session.history
        sent: list[str] = []
        marks: dict[str, float] = {}
        user_added = False
        tts_task = None
        try:
            text = (await loop.run_in_executor(self.executor, self.asr, pcm)).strip()
            marks["asr"] = time.perf_counter()
            await self._send_json({"type": "asr_ended", "turn": turn, "text": text}, turn)
            if not text:
                await self._send_json({"type": "tts_end", "turn": turn}, turn)
                self.log.info("turn=%d 沒聽到東西（辨識 %d ms）", turn, (marks["asr"] - t0) * 1000)
                if self.busy_turn == turn:
                    self._back_to_listening()  # 手機直接回聆聽中，之後那包 playback_done 會被忽略
                return

            content: list[dict] = []
            while self.session.images:  # 圖放在文字前面；送過就不再送
                b64 = base64.b64encode(self.session.images.popleft()).decode("ascii")
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": b64}})
            content.append({"type": "text", "text": text})
            hist.append({"role": "user", "content": content})
            user_added = True

            q: asyncio.Queue = asyncio.Queue()
            tts_task = asyncio.create_task(self._tts_worker(turn, q, marks))
            splitter = SentenceSplitter()

            async def on_text(delta: str) -> None:
                marks.setdefault("claude", time.perf_counter())
                sent.append(delta)
                await self._send_json({"type": "reply_delta", "turn": turn, "text": delta}, turn)
                for s in splitter.feed(delta):
                    marks.setdefault("seg", time.perf_counter())
                    q.put_nowait(s)

            async def on_act(name: str) -> None:
                await self._send_json({"type": "pet_act", "turn": turn, "name": name}, turn)

            reply = None
            try:
                reply, refused = await self.brain.stream(list(hist), on_text, call=self.session, on_act=on_act)
                if refused:
                    for s in splitter.flush():
                        q.put_nowait(s)
                    await on_text(config.REFUSAL_TEXT)
                    reply = "".join(sent)
            except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError,
                    ButlerError) as e:
                # API 的錯誤訊息不含金鑰，記前 300 字才查得出 400 是哪裡錯
                self.log.warning("turn=%d Claude 錯誤：%s %s", turn, type(e).__name__, str(e)[:300])
                await self._send_json({"type": "error", "message": f"AI 暫時沒回應（{type(e).__name__}）"}, turn)
            for s in splitter.flush():
                q.put_nowait(s)
            q.put_nowait(None)
            await tts_task
            if reply is None and sent:
                reply = "".join(sent)  # 串到一半出錯：留已送出的部分
            if reply:
                hist.append({"role": "assistant", "content": reply})
            await self._send_json({"type": "tts_end", "turn": turn}, turn)

            def ms(k):
                return round((marks[k] - t0) * 1000) if k in marks else -1
            self.log.info("turn=%d 延遲 講完→辨識完 %d ms｜→Claude 首字 %d ms｜→第一段切出 %d ms｜→第一包語音 %d ms｜%s｜問：%s",
                          turn, ms("asr"), ms("claude"), ms("seg"), ms("audio"),
                          getattr(self.brain, "last_usage", ""), text[:40])
        except asyncio.CancelledError:
            if tts_task is not None:
                tts_task.cancel()
            if user_added and sent:
                hist.append({"role": "assistant", "content": "".join(sent)})
            raise
        except Exception as e:  # 通話不斷：報錯、收尾這輪
            self.log.exception("turn=%d 失敗", turn)
            if tts_task is not None:
                tts_task.cancel()
            await self._send_json({"type": "error", "message": f"這一輪出錯了（{type(e).__name__}）"}, turn)
            await self._send_json({"type": "tts_end", "turn": turn}, turn)
        finally:
            if self.session is not None:
                self.store.touch(self.session)

    async def _tts_worker(self, turn: int, q: asyncio.Queue, marks: dict) -> None:
        """依序合成、依序送；跟 Claude 串流並行。"""
        while True:
            s = await q.get()
            if s is None:
                return
            if not speakable(s):
                continue
            # 邊合成邊送：PCM 一到就湊成約 100 ms 一包送出，不等整句合成完
            n = config.OUT_PACKET_BYTES
            buf = bytearray()
            try:
                async for pcm in self.tts.stream(s):
                    buf += pcm
                    while len(buf) >= n:
                        await self._send_audio(turn, bytes(buf[:n]))
                        del buf[:n]
                        marks.setdefault("audio", time.perf_counter())
            except Exception as e:
                self.log.warning("turn=%d TTS 失敗（%s）：%s", turn, type(e).__name__, s[:30])
            if len(buf) % 2:  # PCM16 不能送半個樣本
                del buf[-1]
            if buf:
                await self._send_audio(turn, bytes(buf))
                marks.setdefault("audio", time.perf_counter())
