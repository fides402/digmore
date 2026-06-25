@echo off
title DIGMORE
echo.
echo  DIGMORE  ^|  http://127.0.0.1:8099
echo  -----------------------------------------------
echo  Chiudo istanza precedente su porta 8099...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8099 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

cd /d "%~dp0"

REM Usa il venv di samplehunter (stesso ambiente Python con CLAP/librosa/yt-dlp)
set VENV=%~dp0..\samplehunter\venv\Scripts\activate.bat
if not exist "%VENV%" set VENV=%~dp0..\samplehunter\.venv\Scripts\activate.bat

echo  Attivo venv...
call "%VENV%" 2>nul

echo  Avvio engine DIGMORE...
start "" /B python -m uvicorn app:app --app-dir "%~dp0engine" --host 127.0.0.1 --port 8099

echo  Attendo avvio (6s)...
timeout /t 6 /nobreak >nul

echo  Apro browser...
start "" http://127.0.0.1:8099
echo.
echo  Server attivo. Premi un tasto per fermarlo.
pause >nul

echo  Chiudo server...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8099 "') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo  Fatto.
