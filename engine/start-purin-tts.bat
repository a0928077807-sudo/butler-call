@echo off
rem purin voice TTS (purin_tts_server.py): restart loop. Started hidden by start-purin-tts.vbs (task "purin-tts").
rem Exits with python -> wait 10 s -> start again. Output goes to log\purin_tts.log.
rem If port 8792 is already listening (another copy is running), do not start a second one:
rem a second copy would load the model twice and waste GPU memory.
setlocal
cd /d "%~dp0"
if not exist "%~dp0log" mkdir "%~dp0log"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set HF_HOME=D:\qwen-tts\hf
:loop
netstat -ano | findstr /r /c:"127\.0\.0\.1:8792 .*LISTENING" >nul
if not errorlevel 1 (
  timeout /t 30 /nobreak >nul
  goto loop
)
echo [%date% %time%] start purin_tts_server.py >> "%~dp0log\purin_tts.log"
"D:\qwen-tts\venv-fast\Scripts\python.exe" "%~dp0purin_tts_server.py" >> "%~dp0log\purin_tts.log" 2>&1
echo [%date% %time%] purin_tts_server.py exited code %errorlevel%, restart in 10 s >> "%~dp0log\purin_tts.log"
timeout /t 10 /nobreak >nul
goto loop
