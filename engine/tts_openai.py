# -*- coding: utf-8 -*-
"""OpenAI 語音合成（gpt-4o-mini-tts）：可以用文字指定語氣，給通話一個「布丁狗」的聲音。

為什麼有它（2026-11-18）：edge-tts 的聲音他嫌難聽；挑了五個有「布丁狗感」的樣本，他說「完全不像」。
免費、能即時用的聲音只有那些，就改用能用 instructions 描述語氣的 OpenAI TTS（約每分鐘 0.015 美元，他選的）。

介面跟 tts.EdgeTts 一樣是 `stream(text)`（async 迭代器吐 24 kHz／16 位元／單聲道 PCM）。
response_format=pcm 本身就是 24 kHz s16le，不必經 ffmpeg。金鑰讀 openai_key.txt（不進版控、不進 log）。
"""
import httpx

import config

URL = "https://api.openai.com/v1/audio/speech"


class OpenAiTts:
    def __init__(self, key: str, voice: str = config.OPENAI_TTS_VOICE,
                 instructions: str = config.OPENAI_TTS_INSTRUCTIONS, model: str = config.OPENAI_TTS_MODEL):
        self.voice, self.instructions, self.model = voice, instructions, model
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=30.0),
                                        headers={"Authorization": f"Bearer {key}"})

    async def stream(self, text: str):
        body = {"model": self.model, "input": text, "voice": self.voice,
                "instructions": self.instructions, "response_format": "pcm"}
        async with self.client.stream("POST", URL, json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread())[:300].decode("utf-8", "replace")
                raise RuntimeError(f"OpenAI TTS {r.status_code}: {detail}")
            odd = b""
            async for chunk in r.aiter_bytes():
                chunk = odd + chunk
                if len(chunk) % 2:  # PCM16 不能切在半個樣本
                    chunk, odd = chunk[:-1], chunk[-1:]
                else:
                    odd = b""
                if chunk:
                    yield chunk
