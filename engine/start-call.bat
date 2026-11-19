@echo off
rem butler-call engine: restart loop. Started hidden by start-call.vbs (task "butler-call").
rem Exits with python -> wait 5 s -> start again. Output goes to log\stdout.log.
rem If port 8790 is already listening (another copy is running), do not start a second one:
rem loading Breeze twice would waste GPU memory and the second copy fails to bind anyway.
setlocal
cd /d "%~dp0"
if not exist "%~dp0log" mkdir "%~dp0log"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
:loop
netstat -ano | findstr /r /c:"127\.0\.0\.1:8790 .*LISTENING" >nul
if not errorlevel 1 (
  timeout /t 30 /nobreak >nul
  goto loop
)
echo [%date% %time%] start server.py >> "%~dp0log\stdout.log"
"D:\butler-asr\venv\Scripts\python.exe" "%~dp0server.py" >> "%~dp0log\stdout.log" 2>&1
echo [%date% %time%] server.py exited code %errorlevel%, restart in 5 s >> "%~dp0log\stdout.log"
timeout /t 5 /nobreak >nul
goto loop
