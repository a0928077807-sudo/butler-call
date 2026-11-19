# -*- coding: utf-8 -*-
"""Claude 串流回覆。介面：`await brain.stream(messages, on_text) -> (assistant_content, refused)`。

獨立成一支是為了測試能換成假的大腦（不打 API）。金鑰只在這裡讀，不印、不進 log。
"""
import anthropic

import config


def read_key() -> str:
    return config.KEY_FILE.read_text(encoding="utf-8").strip()


IMAGE_PLACEHOLDER = {"type": "text", "text": "[畫面]"}


def drop_old_images(messages: list[dict]) -> list[dict]:
    """只讓最新那則 user 訊息帶圖；更早的 user 訊息裡的 image 區塊換成文字「[畫面]」。

    為什麼：視訊每輪都帶一張圖，全留著的話每輪送出的歷史越來越大、越來越慢（2026-11-18 第二棒記的風險）。
    回傳新的 list，不改原本的歷史；assistant 的 content（含 thinking 區塊）一律原樣不動，
    改了 thinking 區塊 API 會拒收。
    """
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    out = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if m.get("role") == "user" and i != last_user and isinstance(c, list) \
                and any(isinstance(b, dict) and b.get("type") == "image" for b in c):
            m = dict(m, content=[dict(IMAGE_PLACEHOLDER) if isinstance(b, dict) and b.get("type") == "image"
                                 else b for b in c])
        out.append(m)
    return out


class ClaudeBrain:
    def __init__(self, key: str, model: str = config.MODEL, effort: str = config.EFFORT,
                 max_tokens: int = config.MAX_TOKENS, system: str = config.SYSTEM_PROMPT):
        self.client = anthropic.AsyncAnthropic(api_key=key)
        self.model, self.effort, self.max_tokens, self.system = model, effort, max_tokens, system
        self.last_usage = ""

    async def stream(self, messages: list[dict], on_text) -> tuple[list[dict] | None, bool]:
        """邊收邊呼叫 on_text(片段)。回 (要放回歷史的 content, 是否拒答)。
        Opus 5.5 不能傳 thinking disabled／temperature（會 400），也不能 assistant prefill。
        thinking 只傳 adaptive＋drop_block：thinking 區塊的簽章綁住它前面整段對話，舊圖換成 [畫面] 後
        簽章就對不上（2026-11-18 e2e 實測 400「bound to a different conversation」）；drop_block 讓
        API 自己丟掉對不上的區塊，歷史裡的 assistant content 照樣原樣不動。"""
        async with self.client.beta.messages.stream(
            model=self.model, max_tokens=self.max_tokens,
            # block_binding 要配 thinking-binding-controls beta，沒帶會 400「Extra inputs are not permitted」
            betas=["server-side-fallback-2026-07-01", "thinking-binding-controls-2026-08-01"], fallbacks="default",
            output_config={"effort": self.effort},
            thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            cache_control={"type": "ephemeral"},  # 頂層：自動標在最後一個可快取區塊，讓歷史也吃快取
            messages=drop_old_images(messages),
        ) as stream:
            async for text in stream.text_stream:
                await on_text(text)
            final = await stream.get_final_message()
        u = final.usage  # 給 log 看快取有沒有吃到
        self.last_usage = (f"輸入 {u.input_tokens}＋快取寫 {u.cache_creation_input_tokens or 0}"
                           f"＋快取讀 {u.cache_read_input_tokens or 0}")
        if final.stop_reason == "refusal":
            return None, True
        # 整包 content 原樣放回（含 thinking 區塊），轉成 dict 方便下一輪送出
        return [b.model_dump(mode="json", exclude_none=True) for b in final.content], False
