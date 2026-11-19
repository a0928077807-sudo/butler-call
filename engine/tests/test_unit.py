# -*- coding: utf-8 -*-
"""通話引擎單元測試：切句、0x11 編碼、半雙工丟音訊、多餘 playback_done、打斷、空辨識、歷史保存、VAD。

不載入 Breeze、不打 API（辨識、大腦、合成全用假的）；VAD 用真的 Silero（CPU）配測試音檔。
直接跑：D:\\butler-asr\\venv\\Scripts\\python.exe tests\\test_unit.py   任何一條失敗 exit 1。
"""
import asyncio
import json
import logging
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")  # 主控台預設 cp950，印「≥」會炸

import config  # noqa: E402
from call_handler import CallHandler, SentenceSplitter, encode_tts_packet  # noqa: E402
from session import SessionStore  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILED.append(name)


# ---------- 假零件 ----------
class FakeVad:
    def __init__(self):
        self.feeds = 0
        self.resets = 0
        self.fire = False  # 下一包回一句

    def feed(self, pcm):
        self.feeds += 1
        if self.fire:
            self.fire = False
            return b"\x00\x00" * 8000
        return None

    def reset(self):
        self.resets += 1


class FakeBrain:
    def __init__(self, parts, delay=0.0, act=None):
        self.parts, self.delay, self.calls, self.act = parts, delay, [], act

    async def stream(self, messages, on_text, call=None, on_act=None):
        self.calls.append(messages)
        for p in self.parts:
            await asyncio.sleep(self.delay)
            await on_text(p)
        if self.act and on_act is not None:
            await on_act(self.act)
        return [{"type": "text", "text": "".join(self.parts)}], False


class FakeTts:
    async def stream(self, text):
        for _ in range(3):  # 共 0.5 秒，分三塊吐（每塊不是 100 ms 的整數倍）→ 湊成 5 包
            await asyncio.sleep(0.01)
            yield b"\x01\x00" * 4000


class Wire:
    """假 WebSocket：記下送出去的東西。"""

    def __init__(self):
        self.out: list = []

    async def send_text(self, s):
        self.out.append(json.loads(s))

    async def send_bytes(self, b):
        self.out.append(b)

    def json_types(self):
        return [m["type"] for m in self.out if isinstance(m, dict)]

    def audio(self, turn=None):
        return [m for m in self.out if isinstance(m, bytes)
                and (turn is None or int.from_bytes(m[1:5], "big") == turn)]


def make(asr_text="你好", brain=None, store=None):
    wire, vad = Wire(), FakeVad()
    h = CallHandler(wire.send_text, wire.send_bytes, store or SessionStore(), lambda pcm: asr_text,
                    brain or FakeBrain(["好的。", "我知道了！"]), FakeTts(), vad,
                    logging.getLogger("test"))
    return h, wire, vad


PKT = b"\x01" + b"\x00" * 5120


# ---------- 測試 ----------
def test_splitter():
    s = SentenceSplitter(comma_min=12)
    check("切句：句號、驚嘆號", s.feed("你好。今天天氣") + s.feed("很好！") == ["你好。", "今天天氣很好！"])
    check("切句：短的逗號不切", s.feed("好，我知道了") == [] and s.flush() == ["好，我知道了"])
    long = "一二三四五六七八九十十一十二"  # 14 字
    check("切句：累積 ≥12 字的逗號要切", s.feed(long + "，然後") == [long + "，"] and s.flush() == ["然後"])
    check("切句：換行、問號", s.feed("第一行\n要嗎？") == ["第一行", "要嗎？"])
    check("切句：flush 空的回空", s.flush() == [])
    f = SentenceSplitter(comma_min=12, first_min=4)
    check("切句：第一段 3 字逗號不切", f.feed("好的，") == [])
    check("切句：第一段 ≥4 字逗號就切", f.feed("我看看，") == ["好的，我看看，"])
    check("切句：第二段起恢復 12 字門檻", f.feed("你先把電阻拿出來，") == [] and f.flush() == ["你先把電阻拿出來，"])
    g = SentenceSplitter(comma_min=12, first_min=4)
    check("切句：第一段頓號、冒號也算", g.feed("紅色、") == [] and g.feed("藍色：") == ["紅色、藍色："])
    h = SentenceSplitter(comma_min=12, first_min=4)
    check("切句：第一段短句號照切", h.feed("好。") == ["好。"] and h.feed("那我們先這樣，") == [])


def test_encode():
    p = encode_tts_packet(258, b"ab")
    check("0x11 編碼", p == b"\x11\x00\x00\x01\x02ab", repr(p))
    check("0x11 大輪次", encode_tts_packet(0x01020304, b"")[1:5] == b"\x01\x02\x03\x04")
    check("一包約 100 ms", config.OUT_PACKET_BYTES == 4800)


async def test_half_duplex_and_playback_done():
    h, wire, vad = make()
    await h.on_binary(PKT)
    check("start 前音訊不進 VAD", vad.feeds == 0)
    await h.on_json({"type": "start", "mode": "voice", "call_id": "c1"})
    check("start 回 ready", wire.json_types() == ["ready"])
    vad.fire = True
    await h.on_binary(PKT)
    check("講完進入忙碌", h.busy_turn == 0)
    await h.task
    feeds = vad.feeds
    resets = vad.resets
    for _ in range(5):
        await h.on_binary(PKT)
    check("半雙工：忙碌時音訊不進 VAD", vad.feeds == feeds)
    types = wire.json_types()
    check("事件順序", types[:3] == ["ready", "asr_ended", "reply_delta"] and types[-1] == "tts_end", str(types))
    check("有 0x11 且輪次 0", len(wire.audio(0)) == 10 and len(wire.audio()) == 10, str(len(wire.audio())))
    check("串流塊重新湊成每包 100 ms", all(len(m) == 5 + 4800 for m in wire.audio()))
    await h.on_json({"type": "playback_done", "turn": 7})
    check("舊/錯輪次 playback_done 忽略", h.busy_turn == 0)
    await h.on_json({"type": "playback_done", "turn": 0})
    check("playback_done 回聆聽中且 VAD 重置", h.busy_turn is None and vad.resets > resets)
    n = len(wire.out)
    await h.on_json({"type": "playback_done", "turn": 0})
    await h.on_json({"type": "playback_done", "turn": 0})
    check("多餘 playback_done 不出錯、不送東西", h.busy_turn is None and len(wire.out) == n)
    await h.on_binary(PKT)
    check("回聆聽中後音訊進 VAD", vad.feeds == feeds + 1)
    hist = h.session.history
    check("歷史一問一答", [m["role"] for m in hist] == ["user", "assistant"])
    vad.fire = True
    await h.on_binary(PKT)
    check("下一輪輪次 1", h.busy_turn == 1)
    await h.task
    await h.on_json({"type": "ping"})
    check("ping→pong", wire.json_types()[-1] == "pong")


async def test_interrupt():
    brain = FakeBrain(["第一句話很長。", "第二句。", "第三句。", "第四句。"], delay=0.05)
    h, wire, vad = make(brain=brain)
    await h.on_json({"type": "start", "call_id": "c2"})
    vad.fire = True
    await h.on_binary(PKT)
    for _ in range(200):  # 等第一包 0x11
        await asyncio.sleep(0.01)
        if wire.audio(0):
            break
    check("打斷前有 0x11", bool(wire.audio(0)))
    h.interrupt()
    n0 = len(wire.out)
    await asyncio.sleep(0.4)
    after = wire.out[n0:]
    check("打斷後不再送該輪任何東西", after == [], str(after[:3]))
    check("打斷後回聆聽中", h.busy_turn is None and h.task.done())
    hist = h.session.history
    check("被打斷那輪只留已送出的文字", hist[-1]["role"] == "assistant" and hist[-1]["content"].startswith("第一句"),
          str(hist[-1]))
    vad.fire = True
    await h.on_binary(PKT)
    check("打斷後下一輪輪次 +1", h.busy_turn == 1)
    await h.task
    check("下一輪有 0x11（輪次 1）", bool(wire.audio(1)))


async def test_empty_asr():
    h, wire, vad = make(asr_text="  ")
    await h.on_json({"type": "start", "call_id": "c3"})
    vad.fire = True
    await h.on_binary(PKT)
    await h.task
    msgs = [m for m in wire.out if isinstance(m, dict)]
    check("空辨識：asr_ended 文字空＋tts_end",
          [m["type"] for m in msgs] == ["ready", "asr_ended", "tts_end"] and msgs[1]["text"] == "", str(msgs))
    check("空辨識：直接回聆聽中、不進歷史", h.busy_turn is None and h.session.history == [])
    await h.on_json({"type": "playback_done", "turn": 0})
    check("空辨識後的 playback_done 忽略", h.busy_turn is None)


async def test_image_and_session():
    store = SessionStore()
    h, wire, vad = make(store=store)
    await h.on_json({"type": "start", "mode": "video", "call_id": "c4"})
    for i in range(3):
        await h.on_binary(b"\x02" + bytes([i]) * 10)
    check("每張圖回 image_ack", wire.json_types().count("image_ack") == 3)
    check("只留最近 2 張", list(h.session.images) == [bytes([1]) * 10, bytes([2]) * 10])
    vad.fire = True
    await h.on_binary(PKT)
    await h.task
    user = h.session.history[0]["content"]
    check("圖放在文字前面", [c["type"] for c in user] == ["image", "image", "text"])
    check("送過的圖清掉", len(h.session.images) == 0)
    await h.close()
    h2, _, _ = make(store=store)
    await h2.on_json({"type": "start", "call_id": "c4"})
    check("同 call_id 重連接著歷史", len(h2.session.history) == 2)
    check("每條連線輪次從 0 起", h2.next_turn == 0)
    clock = [0.0]
    st = SessionStore(ttl=600, clock=lambda: clock[0])
    st.get("x").history.append({"role": "user", "content": "a"})
    clock[0] = 599
    check("10 分鐘內還在", len(st.get("x").history) == 1)
    clock[0] = 599 + 601
    check("過 10 分鐘清掉", st.get("x").history == [])


def test_real_vad():
    from vad import StreamVad
    with wave.open(r"D:\butler-asr\audio\s1_f.wav") as w:
        pcm = w.readframes(w.getnframes())
    v = StreamVad()
    silence = b"\x00\x00" * 16000
    chunks = [(silence[:16000] + pcm + silence * 2)[i:i + 5120] for i in range(0, len(silence[:16000] + pcm + silence * 2), 5120)]
    utts = [u for u in (v.feed(c) for c in chunks) if u]
    check("真 VAD：一句話切出一段", len(utts) == 1, str(len(utts)))
    if utts:
        dur = len(utts[0]) / 32000
        check("真 VAD：長度合理", len(pcm) / 32000 - 0.3 < dur < len(pcm) / 32000 + 1.5, f"{dur:.2f}s")
    v.reset()
    noise = [v.feed(silence[:5120]) for _ in range(20)]
    check("真 VAD：純靜音不觸發", not any(noise))
    v2 = StreamVad(max_speech_sec=1.0)
    forced = [u for u in (v2.feed(pcm[i:i + 5120]) for i in range(0, len(pcm), 5120)) if u]
    check("真 VAD：超過最長強制切", len(forced) >= 1)


def _img(tag):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tag}}


def _video_history():
    thinking = [{"type": "thinking", "thinking": "", "signature": "sig-abc"},
                {"type": "text", "text": "看到紅色的。"}]
    return [
        {"role": "user", "content": [_img("A"), {"type": "text", "text": "第一句"}]},
        {"role": "assistant", "content": thinking},
        {"role": "user", "content": [_img("B"), {"type": "text", "text": "被打斷那句"}]},
        {"role": "user", "content": [_img("C"), {"type": "text", "text": "最新這句"}]},
    ]


def test_drop_old_images():
    import copy
    from brain import IMAGE_PLACEHOLDER, drop_old_images
    hist = _video_history()
    before = copy.deepcopy(hist)
    out = drop_old_images(hist)
    check("舊圖：原本的歷史沒被改", hist == before)
    check("舊圖：最早那則 user 的圖換成 [畫面]",
          out[0]["content"] == [IMAGE_PLACEHOLDER, {"type": "text", "text": "第一句"}], str(out[0]))
    check("舊圖：連續 user 的前一則也換掉", out[2]["content"][0] == IMAGE_PLACEHOLDER, str(out[2]))
    check("舊圖：最新那則 user 保留圖", out[3]["content"][0] == _img("C"), str(out[3]))
    check("舊圖：assistant（含 thinking）原樣不動", out[1] is hist[1] and out[1] == before[1])
    check("舊圖：只有一則 user 時保留圖", drop_old_images(hist[:1])[0]["content"][0] == _img("A"))
    check("舊圖：字串 content 不出錯",
          drop_old_images([{"role": "user", "content": "嗨"}, {"role": "assistant", "content": "好"},
                           {"role": "user", "content": "再見"}])[0]["content"] == "嗨")


async def test_pet_act():
    """助理挑的布丁狗動作：大腦回報 → 送 pet_act 給 App（帶輪次），話照常念。"""
    h, wire, vad = make(brain=FakeBrain(["好喔。"], act="spinJump"))
    await h.on_json({"type": "start", "mode": "voice", "call_id": "c-act"})
    vad.fire = True
    await h.on_binary(PKT)
    await h.task
    acts = [m for m in wire.out if isinstance(m, dict) and m["type"] == "pet_act"]
    check("動作：送出 pet_act", len(acts) == 1 and acts[0]["name"] == "spinJump" and acts[0]["turn"] == 0, str(acts))
    check("動作：話照常送", "reply_delta" in wire.json_types() and wire.json_types()[-1] == "tts_end", str(wire.json_types()))

    # ButlerBrain 讀 SSE：pet.act 交給 on_act，別條對話的不理，turn.done 收工
    from butler_brain import ButlerBrain, SpeechFilter

    class Ev:
        def __init__(self, event, data):
            self.event, self.data = event, json.dumps(data)

    class Es:
        def __init__(self, evs):
            self.evs = evs

        def aiter_sse(self):
            async def gen():
                for e in self.evs:
                    yield e
            return gen()

    evs = [Ev("text.delta", {"conv_id": "c", "d": "好的[[動作:跳起來轉圈]]"}),
           Ev("pet.act", {"conv_id": "x", "name": "boing"}),
           Ev("pet.act", {"conv_id": "c", "name": "spinJump"}),
           Ev("turn.done", {"conv_id": "c"})]
    got, said = [], []

    async def on_text(t):
        said.append(t)

    async def on_act(n):
        got.append(n)
    b = ButlerBrain("tok", base="http://127.0.0.1:1")
    why = await b._pump(Es(evs), "c", SpeechFilter(), [], on_text, on_act)
    check("大腦：pet.act 轉給 on_act、別條對話的不理", got == ["spinJump"] and why == "turn.done", f"{got} {why}")
    check("大腦：動作標記不念出來", "".join(said) == "好的", "".join(said))


async def test_brain_request_args():
    """不打 API：把 client 換成假的，看送出去的參數。"""
    from brain import IMAGE_PLACEHOLDER, ClaudeBrain
    got = {}

    class FakeFinal:
        stop_reason = "end_turn"
        content = []

        class usage:
            input_tokens, cache_creation_input_tokens, cache_read_input_tokens = 10, 0, 5

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def gen():
                yield "好"
            return gen()

        async def get_final_message(self):
            return FakeFinal()

    def fake_stream(**kw):
        got.update(kw)
        return FakeStream()

    b = ClaudeBrain("sk-fake")
    b.client.beta.messages.stream = fake_stream
    hist = _video_history()
    parts = []

    async def on_text(t):
        parts.append(t)
    await b.stream(hist, on_text)
    check("大腦：有頂層 cache_control", got.get("cache_control") == {"type": "ephemeral"}, str(got.get("cache_control")))
    check("大腦：thinking 簽章對不上時 drop_block（舊圖換掉後才不會 400）",
          got.get("thinking") == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
          str(got.get("thinking")))
    msgs = got.get("messages") or []
    check("大腦：送出的舊圖換成 [畫面]、最新保留",
          len(msgs) == 4 and msgs[0]["content"][0] == IMAGE_PLACEHOLDER and msgs[3]["content"][0] == _img("C"))
    check("大腦：歷史本身仍保留原圖", hist[0]["content"][0] == _img("A"))


def test_speech_filter():
    from butler_brain import SpeechFilter, last_user
    f = SpeechFilter()
    parts = ["好的，**350", "°C** 就夠了。", "[", "[DO", "NE]]"]
    out = "".join(f.feed(p) for p in parts)
    check("濾掉 markdown 與 [[DONE]]", out == "好的，350°C 就夠了。", repr(out))
    f = SpeechFilter()
    out = "".join(f.feed(p) for p in ["先看", "[[ASK:選哪個？|甲|乙]]", "這樣。"])
    check("ASK 標記整段不念（裡面的問號也不會被切句）", out == "先看這樣。", repr(out))
    f = SpeechFilter()
    out = "".join(f.feed(p) for p in ["看這段", "```py", "\nx=1\n``", "`", "跑完了。"])
    check("程式碼區塊整段不念", out == "看這段跑完了。", repr(out))
    f = SpeechFilter()
    out = "".join(f.feed(p) for p in ["畫面[左", "邊]有東西"])
    check("單一中括號照念", out == "畫面[左邊]有東西", repr(out))
    import base64 as b64
    msgs = [{"role": "user", "content": "舊的"}, {"role": "assistant", "content": "x"},
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": b64.b64encode(b"A").decode()}},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": b64.b64encode(b"B").decode()}},
                {"type": "text", "text": "這是什麼"}]}]
    check("取最後一句與最新一張圖", last_user(msgs) == ("這是什麼", b"B"), repr(last_user(msgs)))


async def test_conv_id_from_start():
    store = SessionStore()
    h, _, _ = make(store=store)
    await h.on_json({"type": "start", "call_id": "k1", "conv_id": "c12345678"})
    check("start 帶 conv_id 就接那條", h.session.conv_id == "c12345678", str(h.session.conv_id))
    h.session.briefed = True
    await h.close()
    h2, _, _ = make(store=store)
    await h2.on_json({"type": "start", "call_id": "k1", "conv_id": "c12345678"})
    check("同一條重連不重送規矩", h2.session.briefed is True)
    await h2.close()
    h3, _, _ = make(store=store)
    await h3.on_json({"type": "start", "call_id": "k2"})
    check("沒帶 conv_id 接助理那條", h3.session.conv_id == config.DEFAULT_CONV, str(h3.session.conv_id))
    await h3.close()


async def amain():
    test_speech_filter()
    await test_conv_id_from_start()
    test_drop_old_images()
    await test_brain_request_args()
    await test_pet_act()
    test_splitter()
    test_encode()
    await test_half_duplex_and_playback_done()
    await test_interrupt()
    await test_empty_asr()
    await test_image_and_session()
    test_real_vad()


if __name__ == "__main__":
    asyncio.run(amain())
    print(f"\n{'全部通過' if not FAILED else '失敗 %d 條：%s' % (len(FAILED), FAILED)}")
    sys.exit(1 if FAILED else 0)
