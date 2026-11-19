# -*- coding: utf-8 -*-
"""通話引擎的參數。改數字改這裡；埠與 log 目錄可用環境變數覆寫（測試用暫存目錄、另一個埠）。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 只聽本機；對外由 butler 的 /v1/call 驗證後轉送進來
HOST = "127.0.0.1"
PORT = int(os.environ.get("BUTLER_CALL_PORT", "8790"))
LOG_DIR = Path(os.environ.get("BUTLER_CALL_LOG_DIR", str(BASE_DIR / "log")))

# 大腦（Claude）
KEY_FILE = Path(os.environ.get("BUTLER_CALL_KEY_FILE", str(BASE_DIR / "anthropic_api_key.txt")))  # 只有舊大腦 brain.py 用
MODEL = os.environ.get("BUTLER_CALL_MODEL", "claude-opus-5-5")
EFFORT = "low"
MAX_TOKENS = 2000
REFUSAL_TEXT = "這個我沒辦法回答，換個問題好嗎？"
SYSTEM_PROMPT = (
    "你是使用者的助理，正在跟他講電話。"
    "說話要口語、用短句，一次只講一兩句重點。"
    "不要用 markdown、條列、表情符號或網址。"
    "數字和英文縮寫要寫成念出來好聽、好懂的樣子。"
    "視訊時你會看到他鏡頭的畫面，要照畫面具體指導他動手；看不清楚就請他把鏡頭靠近一點或換個角度。"
    "被打斷時，不要重複前面已經講過的話。"
)

# 大腦改成 butler 那條對話的助理（2026-11-18，見 butler_brain.py）
BUTLER_PORT = 47362
BUTLER_FALLBACK = os.environ.get("BUTLER_URL", "http://127.0.0.1:47362")  # netstat 找不到 butler 時用
BUTLER_TOKEN_FILE = BASE_DIR / "token.txt"       # butler 的裝置權杖（工具頁新增裝置拿到的那串）；不進版控
BUTLER_UPLOAD_DIR = Path(os.environ.get("BUTLER_UPLOAD_DIR", r"..\butler-kit\server\data\uploads"))  # 視訊截圖放這裡給 butler 讀
DEFAULT_CONV = "main"            # 從工具頁打（沒帶 conv_id）就接助理那條
BUTLER_SILENT_LIMIT_SEC = 45.0   # 一個字都還沒回就等這麼久：講一句、交還麥克風
BUTLER_TURN_LIMIT_SEC = 90.0     # 已經在回了，整輪最多等這麼久

# 語音輸入與 VAD（Silero v6，faster-whisper 內附那份）
IN_RATE = 16000
VAD_MODEL = os.environ.get("BUTLER_CALL_VAD_MODEL", r"D:\butler-asr\venv\Lib\site-packages\faster_whisper\assets\silero_vad_v6.onnx")  # faster-whisper 內附
VAD_THRESHOLD = 0.5        # 超過算有人聲；低於 threshold-0.15 才算靜音（遲滯，照 Silero 官方做法）
VAD_SILENCE_SEC = 0.7      # 靜音這麼久算講完
VAD_MIN_SPEECH_SEC = 0.3   # 比這短的當雜音丟掉
VAD_MAX_SPEECH_SEC = 30.0  # 講超過這麼久強制切
VAD_PREROLL_SEC = 0.3      # 觸發前保留的音訊，避免吃掉第一個字

# 辨識常用詞（faster-whisper hotwords）。2026-11-18 用他的真人錄音量：不給 0.219 → 給 0.154（正規化字錯率），
# 「逛店實驗→光電實驗」「棒球在 cloud→幫我查一下 Claude」「店主→電阻」都修好，速度不變。
# 詞以空白分隔，太多會擠掉效果，保持二三十個。換成你自己常講的人名、地名、專有名詞（說明.md 有量法）。
ASR_HOTWORDS = os.environ.get("BUTLER_CALL_HOTWORDS", "Claude butler 布丁狗 實驗 報告 麵包板 電阻 電容 Arduino USB 烙鐵 焊錫 Word PDF")

# 語音輸出
TTS_VOICE = "zh-TW-HsiaoChenNeural"
# OpenAI 語音（tts_openai.py）。金鑰檔存在就用它，不存在退回 edge-tts
OPENAI_KEY_FILE = BASE_DIR / "openai_key.txt"   # 不進版控
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "coral"
OPENAI_TTS_INSTRUCTIONS = (
    "你是布丁狗：一隻慢吞吞、溫和、有點懶洋洋的金黃色小狗。"
    "用台灣口音的國語，語速偏慢，聲音軟軟的、帶點笑意，像剛睡醒的可愛小男生。"
)
# 布丁狗聲音（purin_tts_server.py，另一個行程；連不上就退回 edge-tts，見 tts_qwen.py）
PURIN_TTS_URL = os.environ.get("PURIN_TTS_URL", "http://127.0.0.1:8792")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")  # 不在 PATH 就設環境變數 FFMPEG 指到 ffmpeg.exe
OUT_RATE = 24000
OUT_PACKET_BYTES = OUT_RATE * 2 // 10  # 約 100 ms 一包（16 位元單聲道）

# 切句：句號／問號／驚嘆號／換行一定切；逗號在累積到這麼多字時也切
SPLIT_COMMA_MIN_CHARS = 12
SPLIT_FIRST_MIN_CHARS = 4   # 每輪第一段：逗號、頓號、冒號等累積到這麼多字就切，讓第一包語音早點出來

# 對話保存
HISTORY_TTL_SEC = 600  # 同一個 call_id 斷線後保留歷史 10 分鐘
MAX_IMAGES = 2         # 只留最近兩張圖
