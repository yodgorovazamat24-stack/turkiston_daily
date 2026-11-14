@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo .env fayli topilmadi.
    echo Loyihadagi .env faylini tekshiring.
    pause
    exit /b 1
)

findstr /x /c:"BOT_TOKEN=YOUR_BOT_TOKEN_HERE" .env >nul
if not errorlevel 1 (
    echo .env faylini ochib BOT_TOKEN qatoriga BotFather tokenini kiriting.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Virtual muhit yaratilmoqda...
    python -m venv .venv
    if errorlevel 1 goto failed
)

echo Kerakli paketlar tekshirilmoqda...
".venv\Scripts\python.exe" -m pip install -r requirements-bot.txt
if errorlevel 1 goto failed

echo Telegram Mini App uchun HTTPS tunnel va bot ishga tushmoqda.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_local.ps1"
if errorlevel 1 goto failed
goto end

:failed
echo Ishga tushirishda xato yuz berdi. Xato matnini yuqoridan tekshiring.
pause

:end
endlocal