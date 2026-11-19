# butler-call：讓 butler 可以打電話、開視訊

在 butler 的手機 App 或網頁版按電話鈕，就能直接開口跟助理講話，助理用語音回答；開視訊時助理還看得到鏡頭畫面，可以邊看邊教你動手。
接電話的就是那條 butler 對話裡的助理本人：記憶、前後文都是同一個，在哪條對話撥出就由那條對話接。

這包是從實際在用的版本整理出來的（2026-11-18～19 做、實機驗過）。分兩部分：

- `engine\`：電腦上的通話引擎，是獨立的服務。流程是判斷講完、辨識、把文字送進 butler 對話、合成語音、串流回手機。
- `butler-kit端\`：butler 要加的檔案，包括伺服器的轉送路由、網頁版的通話介面，以及 App 的通話畫面。

---

## 一、為什麼要先把聲音轉成文字

**Claude 沒有語音系統**：API 只收文字、圖片和 PDF，不收音訊，也不會吐出聲音（2026-11 查官方文件確認）。
所以不能像豆包、ChatGPT 那樣讓模型直接「聽」，只能串成一條線：

```
你的聲音 → 判斷講完（Silero VAD）→ 語音辨識轉成文字（Breeze-ASR-25）→ Claude 看文字回答 → 語音合成念出來 → 手機播放
```

這樣做的代價是 **Claude 只看得到辨識出來的字**。辨識聽錯，Claude 就照錯的字回答，它不會知道你其實講了什麼。
所以要降低出錯，重點在辨識這一關。實際做了兩件事：

1. **挑最準的辨識模型**。用真人錄音（59.5 秒、多數是生活和實驗課用語）量字錯率，數字越小越好：

   | 辨識模型 | 不給常用詞 | 給常用詞 |
   |---|---|---|
   | Breeze-ASR-25（聯發科，Whisper 台灣華語微調） | 0.219 | **0.154**（hotwords） |
   | Qwen3-ASR 0.6B | 0.237 | 0.201 |
   | Qwen3-ASR 1.7B | 0.172 | — |
   | OpenAI gpt-4o-transcribe（雲端） | 0.172 | — |
   | Whisper large-v3-turbo（float16，beam 5） | 0.272 | 0.243 |

   Breeze 加常用詞最準，約 4 秒的短句辨識約 1 秒，佔顯存約 1.6 GB（int8_float16）。手機內建的語音辨識錯太多，沒採用。
   數字是把稿子和辨識結果都去掉標點、轉簡體、國字數字轉阿拉伯數字之後算的。錄音的人沒有照稿一字不差念，所以絕對值偏高，看相對高低就好。
   **只量了一個人的一段錄音**，換一個人的口音結果可能不一樣，最好用自己的聲音再量一次（做法見下面「跟 large-v3-turbo＋initial_prompt 比」）。
2. **給辨識模型一份常用詞**（`config.py` 的 `ASR_HOTWORDS`）。把你常講的人名、專有名詞放進去，例如「光電實驗」原本被聽成「逛店實驗」、「Claude」被聽成「cloud」、「電阻」被聽成「店主」，加詞之後都對了，速度不變。
   詞保持二三十個就好，太多反而擠掉效果。雜音、靜音、嗡聲都測過，加了詞也不會憑空冒出這些詞。

另外，辨識出來的字會顯示在通話畫面上。聽錯的時候你看得到，可以按打斷重講。

### 跟 large-v3-turbo＋initial_prompt 比

另一種常見做法是 Whisper large-v3-turbo（float16、beam 5），常用詞組成一句「以下是繁體中文的對話，可能會出現這些專有名詞：…。」塞進 `initial_prompt`。
這包用的是另一顆模型 Breeze-ASR-25，常用詞塞進 `hotwords`。拿同一批錄音、同一份常用詞，兩邊照各自的呼叫方式跑（2026-11-19）：

| 設定 | 真人錄音 | 同稿合成音 | 短句 48 句 | 59.5 秒那段要幾秒 |
|---|---|---|---|---|
| Breeze 不給詞 | 0.219 | 0.112 | 0.098 | 11.8（載入後第一段） |
| **Breeze＋hotwords（這包）** | **0.154** | **0.012** | **0.075** | 8.4 |
| Breeze＋initial_prompt | 0.154 | 0.012 | 0.075 | 8.5 |
| turbo 只有繁體前導句 | 0.272 | 0.065 | 0.101 | 9.4 |
| turbo＋一份跟錄音無關的詞表（電子、CAD 術語） | 0.266 | 0.083 | 0.097 | 2.1 |
| turbo＋initial_prompt，詞換成同一份 | 0.243 | 0.041 | 0.095 | 2.1 |
| turbo＋hotwords | 0.243 | 0.047 | 0.076 | 2.2 |

（字錯率，正規化後；RTX 4060 Laptop 8 GB，模型已暖機。）

看得出來的三件事：

- **差在模型，不在塞詞的方法**。同一顆模型，`hotwords` 和 `initial_prompt` 的分數幾乎一樣。
- **Breeze 比較準，turbo 比較快**。真人錄音 0.154 對 0.243；速度上 turbo 約快 4 倍（59.5 秒的錄音 2.1 秒對 8.4 秒，約 4 秒的短句 0.45 秒對 1.1 秒）。通話是講完一句送一句，延遲主要卡在 Claude 吐第一個字（1～5 秒），辨識慢的那 0.7 秒不是大頭，所以這包選準的。
- **詞表要放自己真的會講的詞**。跟錄音無關的詞表（KiCad、ESP32 這類）幾乎沒有幫助；換成錄音裡會出現的詞才有效。

想用自己的聲音比，照這兩步做：

1. 錄一段 30～60 秒的話存成 16 kHz 單聲道 wav，再把實際講的內容一字不差打成 txt。
2. 跑 `tools\compare_asr.py`，它會用兩顆模型各跑一次，印出字錯率：
   ```
   python tools\compare_asr.py --wav 我的錄音.wav --ref 我的錄音.txt --breeze D:\butler-asr\models\breeze-asr-25-ct2 --turbo D:\butler-asr\models\large-v3-turbo-ct2
   ```
   這支只做「去標點、轉繁體」，沒做國字數字轉換，數字會比上表高一點，兩顆之間仍然可以比。

---

## 二、架構

```
手機 App／網頁 ⇄ (WebSocket，走 Cloudflare Tunnel＋Access) ⇄ butler 主伺服器 /v1/call（只驗證、轉送）
             ⇄ 通話引擎 ws://127.0.0.1:8790/call（只聽本機）
                  ├ Silero VAD：靜音 0.7 秒算講完
                  ├ Breeze-ASR-25：辨識（加常用詞）
                  ├ butler_brain.py：把字送進那條 butler 對話，收助理的串流回覆
                  └ 語音：purin_tts_server.py（127.0.0.1:8792，照參考音念）；連不上自動改用 edge-tts
```

- **為什麼做成獨立服務**：辨識要 CUDA／CTranslate2，不能裝進 butler 的 venv。而且通話引擎改了可以自己重啟，不必動到 butler。
- **半雙工**：助理講話時不聽麥克風，要插話就按打斷鈕（照豆包的做法）。
- **WebSocket 協定 v1**：
  - 手機往上送的二進位封包：`0x01` 加 16 kHz PCM（每包 160 ms），`0x02` 加 JPEG。
  - 伺服器往下送的二進位封包：`0x11` 加 4 位元組輪次編號，再加 24 kHz PCM。
  - 文字訊息：`start`／`ready`／`asr_ended`／`reply_delta`／`tts_end`／`playback_done`／`interrupt`／`image_ack`／`ping`／`pong`／`hangup`。
  - 詳細規則（輪次、打斷、重連沿用 call_id 保留 10 分鐘歷史、送圖背壓）寫在 `engine\call_handler.py` 開頭和 `engine\tests\test_unit.py`。
- 實測延遲：從你講完到聽到第一個字，約 3～6 秒。大部分花在 Claude 吐出第一個字（1～5 秒，看對話長短）。

---

## 三、需要什麼

- Windows，NVIDIA 顯卡 8 GB 以上（辨識加語音合計約 4.5 GB；用 edge-tts 當聲音的話只要約 1.6 GB）。
- 已經在跑的 butler-kit（這包是接在它上面的）。
- ffmpeg（edge-tts 轉檔用）。
- 兩個 Python venv：辨識和合成的套件版本互相衝突，**一定要分開**（見「踩過的坑」）。

## 四、安裝

1. **通話引擎的 venv**（例：`D:\butler-asr\venv`，Python 3.12）：
   ```
   pip install faster-whisper onnxruntime av edge-tts opencc-python-reimplemented fastapi "uvicorn[standard]" websockets pillow httpx httpx-sse anthropic numpy nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
   ```
   最後兩個是顯卡辨識要的 CUDA 12 函式庫。電腦沒另外裝 CUDA 也沒關係，`asr_engine.py` 會自己去 venv 裡找；少了它們會在第一次辨識時報 `cublas64_12.dll is not found`。
2. **辨識模型**：Hugging Face 上已經有人轉好的 `SoybeanMilk/faster-whisper-Breeze-ASR-25`（CTranslate2 格式，3 GB），下載到例如 `D:\butler-asr\models\breeze-asr-25-ct2`，再設環境變數 `BUTLER_ASR_MODEL` 指過去。
3. **設定**（`engine\config.py`，多數可以用環境變數改）：
   - `BUTLER_CALL_VAD_MODEL`：Silero VAD 的 onnx 檔，就用 faster-whisper 內附的 `faster_whisper\assets\silero_vad_v6.onnx`。
   - `BUTLER_URL`：butler 的位址（預設 `http://127.0.0.1:47362`）。
   - `engine\token.txt`：butler 的裝置權杖，一行（不進版控）。
   - `BUTLER_UPLOAD_DIR`：butler 的 `server\data\uploads`，視訊截圖放這裡給助理看。
   - `BUTLER_CALL_HOTWORDS`：你的常用詞，用空白分隔。
   - `FFMPEG`：ffmpeg.exe 的位置（不在 PATH 時才要設）。
4. **butler 端（伺服器）**：把 `butler-kit端\server\` 裡的檔案照原路徑放進 butler 的 `server\`。
   - butler 的 venv 補裝兩個套件。原版 butler-kit 的 requirements 沒有它們，少了 `websockets` 連 WebSocket 都接不了：
     ```
     pip install websockets psutil
     ```
   - 在 `server\transport\app.py` 加兩行 `include_router`（`call_proxy.router`、`callsvc_api.router`）。
   - 驗證：在 `server\` 底下各跑一次，最後印「全部通過」、exit 0 才算裝好。
     ```
     python tests\test_call_proxy.py
     ```
     ```
     python tests\test_callsvc.py
     ```
     原版 butler-kit（GitHub 上 2026-10-10 那版）和另一套從它衍生的版本都實際放進去跑過，兩支都過。沒有網頁版或訪客金鑰的 butler，測試會印「這段不適用」，那是正常的。
   - 改完要重啟 butler 一次。
5. **butler 端（網頁版，選用）**：`call.js`、`call-worklet.js` 是接在這邊自己加的網頁版上的。原版 butler-kit 沒有網頁版，不用放；有自己網頁版的，在標題列放一顆鈕呼叫 `call.js` 開通話。
6. **butler 端（App）**：老實講，這部分**不能直接放進別人的 App 編**，只在這邊自己的 App 上編過、實機用過。
   - 只依賴 Android、CameraX、OkHttp，可以直接沿用的：`CallProtocol.kt`、`CallAudio.kt`、`TtsPlayer.kt`、`FrameGate.kt`、`CallCamera.kt`。
   - 用到 App 本身東西的：`CallClient.kt`、`CallViewModel.kt` 用 `Prefs`（拿 host、token），`CallClient.kt` 另用 `AccessAuth`（Cloudflare Access 標頭）。原版 butler-kit 兩個都有；改過套件名或類別的 App 要換成自己的。
   - 要自己改寫的：`CallPet.kt` 是這邊自己做的布丁狗動畫（`Purin*`），原版沒有，換成自己的吉祥物或拿掉。`CallActivity.kt` 呼叫 `ButlerTheme(scenes = false)`，原版的 `ButlerTheme` 沒有 `scenes` 參數，拿掉就好。`CallScreen.kt` 用的 `Palette`、`Radii`、`Space`、`Type` 原版也有，但裡面的欄位名可能不一樣，沒實際編過。
   - Manifest 註冊 `CallActivity`，加 `RECORD_AUDIO`、`CAMERA`、`MODIFY_AUDIO_SETTINGS` 三個權限，依賴加 CameraX 1.4.2。
   - 進入點：在對話頂欄放電話、視訊鈕，用 Intent 開 `CallActivity`，帶上 `conv_id`。
   - 套件名是 `dev.butlerkit.app`，換到別的套件名要一起改。
7. **開機自動啟動**：工作排程器建兩個登入時觸發的工作。動作都是 `wscript.exe start-call.vbs`／`start-purin-tts.vbs`（隱藏視窗）。
   bat 裡寫的 python 路徑要改成你的 venv。bat 是重啟迴圈，程式掛了 5～10 秒後會自己重開。
   **工作名稱要叫 `butler-call` 和 `purin-tts`**，引擎資料夾也要叫 `butler-call`：`callsvc_api.py`（工具頁的通話服務開關）是用這兩個名字 `schtasks /run` 開回來、用 `butler-call\server.py` 認行程的，名字不一樣開關就沒作用。
8. **測試**：
   ```
   python engine\tests\test_unit.py
   ```
   這是單元測試，不載模型，幾秒跑完，失敗會回傳非 0。
   ```
   python engine\tests\ping_ready.py
   ```
   這支連正式服務確認收得到 ready。另外 `e2e_client.py` 會真的跑一整通電話，大約 2～3 分鐘。

---

## 五、換成自己的聲音

聲音有三種做法，由簡單到客製：

### A. 最簡單：edge-tts 換一個聲音（不用顯卡）
不開 `purin_tts_server.py` 就會自動用 edge-tts。換聲音改 `config.py` 的 `TTS_VOICE`，例如 `zh-TW-HsiaoChenNeural`、`zh-TW-YunJheNeural`。可用的聲音列表：
```
edge-tts --list-voices
```
缺點是聲音固定，很像導航。

### B. 照參考音念（目前用的）：Qwen3-TTS Base＋faster-qwen3-tts
給它**一段幾秒的參考錄音**和**那段錄音一字不差的原文**，它就用那個音色念任何句子，可以串流，第一段約 0.7 秒就出聲。

1. 另建一個 venv（例：`D:\qwen-tts\venv-fast`），裝 torch（CUDA 版）、`faster-qwen3-tts`，然後**把 transformers 釘在 5.15.1**：
   ```
   pip install "transformers==5.15.1"
   ```
   用 5.23 載入會出現 `AttributeError ... upsample`。
2. 準備參考音，有兩種來源：
   - **自己錄**：5～15 秒、安靜環境、口齒清楚、情緒跟你想要的一樣（想要有起伏就錄有起伏的）。轉成 wav：
     ```
     ffmpeg -i 錄音.m4a -ac 1 -ar 24000 voice_ref.wav
     ```
   - **用文字描述生一個新聲音**（不必找人錄）：`tools\make_voice.py`，用 Qwen3-TTS 1.7B VoiceDesign。
     ```
     python tools\make_voice.py --desc "日本兒童動畫吉祥物角色的配音表演，聲音高亢明亮又軟萌，語調起伏非常大、情緒豐富，開心時尾音往上揚" --text "哇！你回來啦～嘿嘿，我等你好久了耶。咦？這個是什麼呀？好厲害喔！" --takes 3
     ```
     同一個描述每次生出來都不一樣，所以多生幾次自己挑。加 `--hz 450 --range 10.5` 會照音高幫你挑最接近的那次。
     原版的「布丁狗」聲音就是用上面這段描述生出來的（音高約 550 Hz、起伏 11 半音）。
3. 把參考音放進 `engine\voice\`，再設兩個環境變數（或直接改 `purin_tts_server.py` 的預設值）：
   - `PURIN_REF_WAV`：參考音的路徑。
   - `PURIN_REF_TEXT`：參考音的原文，**要一字不差**，差了就容易念錯字。
4. 可以調的參數：
   - `PURIN_TTS_MODEL`：預設 0.6B。換成 `Qwen/Qwen3-TTS-12Hz-1.7B-Base` 錯字少一半，但聲音會變低，長句尤其明顯，顯存也多 2 GB。
   - `PURIN_TTS_TEMP`：預設 0.5。量過 0.9 錯字比較多；調低也救不回 1.7B 偏低的音高。
5. **驗字**：換完聲音跑 `tools\check_voice.py`。它叫語音服務念幾句，再用 Breeze 轉回文字算字錯率，順便檢查參考音原文有沒有打錯。
   ```
   python tools\check_voice.py --ref engine\voice\voice_ref.wav --ref-text "參考音原文"
   ```
   原版 0.6B 約 0.10～0.16，1.7B 約 0.05。這支沒做國字數字轉換，「一千」被辨識成「1000」也會算錯，所以看相對高低就好。
   2026-11-19 用 `make_voice.py` 新生的參考音（音高 529 Hz）實跑過一次，平均 0.170。

> 拿別人的錄音（例如配音員、朋友）當參考音前，先取得對方同意。原版刻意用文字描述生聲音，沒有複製任何真人的聲音。

### C. 雲端：OpenAI gpt-4o-mini-tts
`engine\tts_openai.py`，可以用文字指定語氣。要 OpenAI 金鑰（`engine\openai_key.txt`）。實測中文腔調不像卡通角色，最後沒採用，程式留著。

---

## 六、踩過的坑（一行一條）

**辨識與大腦**
- 手機內建 Android 語音辨識錯太多，不能用，改成在電腦上辨識。
- Qwen3-ASR 原版每句 1.86 秒，比 Breeze 慢一倍。原因是 transformers 逐字生成的 Python 開銷，不是模型大小。自己錄 CUDA graph 可以降到 0.34 秒，但 1.7B 跟語音一起開會超過 8 GB。
- Breeze 用 beam 1 只快 0.14 秒，錯字卻變多，所以維持預設。
- 對話歷史裡的舊圖換成文字 `[畫面]` 之後，下一輪 API 回 400 `Invalid signature in thinking block`：thinking 區塊的簽章綁住前面整段對話。解法是 `thinking.block_binding.prefix_mismatch_behavior="drop_block"`，還要一起帶 beta `thinking-binding-controls-2026-08-01`，不帶也是 400。
- Claude 很常用「好，」開頭，只有 1 個字，到不了切句門檻，要等下一個標點才開始念。所以每輪第一段的門檻降到 4 個字。
- 原本以為「第一次合成比較慢」，實測不成立，所以沒有加暖機。

**語音**
- edge-tts、OpenAI、WORLD 變聲做卡通化，聽起來都不像卡通角色。最後改成「文字描述生參考音，再用 Base 模型照念」。
- VoiceDesign 直接拿來通話不行：太慢（1.7B 要 6 倍實時），而且每句音色都不一樣。
- faster-qwen3-tts 裝進原本的 venv 會把 transformers 升到 5.23，弄壞原本的 qwen-tts，所以另建 venv，並把 transformers 釘在 5.15.1。
- 1.7B 長句音調會掉（量到 300～390 Hz，參考音 545），聽起來像普通 AI。調隨機性也救不回來，最後換回 0.6B。
- 事後變調（+3、+5 半音）聽起來很怪，不採用。
- 在中文句子裡加日系口癖（「的說」「欸嘿嘿～」），也沒有比較像。

**服務與 Windows**
- 關掉服務時只殺 python 沒用：bat 是重啟迴圈，5～10 秒後又會拉起來。要整棵關掉，得連 wscript（start-*.vbs）一起結束；或用工具頁的通話服務開關。
- 不要用名稱整批殺 python：butler 本身也是 python。要用 `netstat -ano | findstr :8790` 查到 PID 再殺。
- 同一個服務不小心起了兩份，模型會載兩次、浪費顯存。所以 bat 開頭會先檢查埠有沒有人在聽。
- 工作排程器用 `-WindowStyle Hidden` 還是會閃一下視窗，要用 wscript＋VBS 才完全隱藏。
- 主控台是 cp950 時，印「≥」這類字會 UnicodeEncodeError。子行程要設 `PYTHONIOENCODING=utf-8`，測試檔開頭要 `reconfigure`。
- `python x.py | tail` 拿到的 exit code 是 tail 的，要驗測試結果先導到檔案再看。

**網頁版與 App**
- 網頁版的 `call.js` 把 Uint8Array 也拿去 JSON.stringify，結果音訊全部變成文字送出，引擎什麼都收不到。二進位要原樣送。
- 網頁的 token 不要放在網址上，改放 WebSocket 子協定 `["butler-call","bearer.<token>"]`。通話中每 15 秒重新驗一次，撤銷裝置就會斷線。
- 麥克風只有 https 網址開得了。用 http 的 Tailscale 位址打不了電話，畫面會講原因。
- 窄螢幕的標題列放不下電話和視訊兩顆鈕（量過全部按鈕 421px），600px 以下合併成一顆。
- App 整包測試時，有一支選單渲染測試會偶發 AppNotIdleException（滑桿動畫沒跑完），推 5 秒時鐘後就穩定了。

---

## 七、已知限制
- 半雙工：要插話得按打斷鈕。
- 視訊只有最新一句帶圖，更早的圖會換成 `[畫面]`。所以視訊時讀不到歷史快取，長視訊會越來越慢。
- 從講完到聽到第一個字約 3～6 秒，比豆包、ChatGPT 慢：那些是模型直接聽聲音、直接出聲音，這裡多了辨識和合成兩段。
- iPhone 沒有實機測過。

## 授權
MIT（同 butler-kit，見 LICENSE）。Breeze-ASR-25、Qwen3-TTS、Silero VAD、edge-tts 各依原授權。
