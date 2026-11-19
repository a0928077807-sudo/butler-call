# -*- coding: utf-8 -*-
"""文字轉語音：輸入一句中文，邊合成邊吐 24 kHz／16 位元／單聲道 PCM。

介面只有 `stream(text)`（async 迭代器，一塊一塊吐 PCM），之後要換 VoiceStudio（使用者的聲音）
只要寫一個同介面的類別換掉 EdgeTts。現在用 edge-tts：雲端、快、不佔顯卡（顯卡留給 Breeze 辨識）。

為什麼要邊收邊解：2026-11-18 量到 edge-tts 是串流回傳，首位元組約 0.5 秒、整句要 0.75～2.2 秒；
原本收完整句 mp3 才丟 ffmpeg，第一包語音白等。現在 mp3 一到就寫進 ffmpeg 的 stdin，
另一條執行緒讀 stdout 的 PCM 往外送。ffmpeg 加 -probesize 32／-analyzeduration 0／-f mp3
（不要等探測）與 -flush_packets 1（輸出不要攢 32 KB 才寫），mp3 首包到 PCM 首包約 90 ms。
用執行緒＋subprocess.Popen 而不是 asyncio 子行程：不依賴 uvicorn 在 Windows 上用哪種事件迴圈。
"""
import asyncio
import subprocess
import threading

import edge_tts

import config

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class EdgeTts:
    def __init__(self, voice: str = config.TTS_VOICE, ffmpeg: str = config.FFMPEG,
                 rate: int = config.OUT_RATE):
        self.voice, self.ffmpeg, self.rate = voice, ffmpeg, rate

    async def stream(self, text: str):
        loop = asyncio.get_running_loop()
        proc = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error",
             "-probesize", "32", "-analyzeduration", "0", "-f", "mp3", "-i", "pipe:0",
             "-flush_packets", "1",
             "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(self.rate), "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW)
        out: asyncio.Queue = asyncio.Queue()

        def put(item) -> None:
            try:
                loop.call_soon_threadsafe(out.put_nowait, item)
            except RuntimeError:  # 事件迴圈已關（引擎正在結束）
                pass

        def reader() -> None:
            try:
                while True:
                    b = proc.stdout.read1(65536)
                    if not b:
                        break
                    put(b)
            except (OSError, ValueError):
                pass
            finally:
                put(None)

        async def feeder() -> None:
            try:
                async for chunk in edge_tts.Communicate(text, self.voice).stream():
                    if chunk["type"] == "audio" and chunk["data"]:
                        await asyncio.to_thread(proc.stdin.write, chunk["data"])
            finally:
                try:
                    await asyncio.to_thread(proc.stdin.close)
                except OSError:
                    pass

        threading.Thread(target=reader, daemon=True).start()
        feed = asyncio.create_task(feeder())
        try:
            while True:
                b = await out.get()
                if b is None:
                    break
                yield b
            await feed  # edge-tts 的錯誤（例如沒有音訊）從這裡丟出去
        finally:
            feed.cancel()
            if proc.poll() is None:
                proc.kill()
            await asyncio.to_thread(proc.wait)
