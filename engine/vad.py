# -*- coding: utf-8 -*-
"""串流版 Silero VAD：一包一包餵 16 kHz PCM，判斷「講完一句」就吐出整句音訊。

為什麼自己寫：faster-whisper 內附的 SileroVADModel 是整段離線算，通話要邊收邊判斷。
模型檔用它內附的 silero_vad_v6.onnx（輸入 64 樣本前文＋512 樣本、h/c 狀態各 1x1x128），
餵法照 faster_whisper/vad.py 的 SileroVADModel.__call__，只是改成逐幀並把 h/c 帶到下一幀。
"""
from collections import deque

import numpy as np
import onnxruntime

import config

FRAME = 512    # 32 ms @ 16 kHz
CONTEXT = 64

_sessions: dict[str, onnxruntime.InferenceSession] = {}


def _session(path: str) -> onnxruntime.InferenceSession:
    if path not in _sessions:
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        _sessions[path] = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"],
                                                       sess_options=opts)
    return _sessions[path]


class StreamVad:
    def __init__(self, model_path: str = config.VAD_MODEL, threshold: float = config.VAD_THRESHOLD,
                 silence_sec: float = config.VAD_SILENCE_SEC, min_speech_sec: float = config.VAD_MIN_SPEECH_SEC,
                 max_speech_sec: float = config.VAD_MAX_SPEECH_SEC, preroll_sec: float = config.VAD_PREROLL_SEC,
                 rate: int = config.IN_RATE):
        self._sess = _session(model_path)
        fsec = FRAME / rate
        self.th, self.neg_th = threshold, threshold - 0.15
        self.silence_frames = int(np.ceil(silence_sec / fsec))
        self.min_frames = int(np.ceil(min_speech_sec / fsec))
        self.max_frames = int(np.ceil(max_speech_sec / fsec))
        self.preroll_frames = int(np.ceil(preroll_sec / fsec))
        self.reset()

    def reset(self) -> None:
        """全部歸零（模型狀態＋這一句）。半雙工切換、打斷時呼叫。"""
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.int16)
        self._reset_segment()

    def _reset_segment(self) -> None:
        self._pre: deque[np.ndarray] = deque(maxlen=self.preroll_frames)
        self._utt: list[np.ndarray] = []
        self._in_speech = False
        self._silence = 0
        self._voiced_end = 0  # 這句裡最後一幀人聲的位置（幀數，從觸發算起）

    def _prob(self, frame16: np.ndarray) -> float:
        x = frame16.astype(np.float32) / 32768.0
        inp = np.concatenate([self._ctx, x])[None, :]
        out, self._h, self._c = self._sess.run(None, {"input": inp, "h": self._h, "c": self._c})
        self._ctx = x[-CONTEXT:]
        return float(out.reshape(-1)[0])

    def feed(self, pcm16: bytes) -> bytes | None:
        """餵一包 PCM16LE。講完一句（或超過最長）就回整句 PCM 並自動 reset，否則回 None。"""
        self._pending = np.concatenate([self._pending, np.frombuffer(pcm16, dtype="<i2")])
        while self._pending.size >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            utt = self._step(frame, self._prob(frame))
            if utt is not None:
                self.reset()
                return utt
        return None

    def _step(self, frame: np.ndarray, p: float) -> bytes | None:
        if not self._in_speech:
            if p >= self.th:
                self._in_speech = True
                self._utt = list(self._pre) + [frame]
                self._start = len(self._pre)
                self._voiced_end = len(self._utt)
                self._silence = 0
            else:
                self._pre.append(frame)
            return None
        self._utt.append(frame)
        if p >= self.neg_th:
            self._silence = 0
            self._voiced_end = len(self._utt)
        else:
            self._silence += 1
        voiced = self._voiced_end - self._start
        if self._silence >= self.silence_frames:
            if voiced >= self.min_frames:
                return np.concatenate(self._utt).astype("<i2").tobytes()
            self._reset_segment()  # 太短，當雜音
            return None
        if len(self._utt) - self._start >= self.max_frames:
            return np.concatenate(self._utt).astype("<i2").tobytes()
        return None
