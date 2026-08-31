@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python не знайдено.
  echo Встановіть Python з https://www.python.org/downloads/
  echo і обов'язково позначте "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Створюю віртуальне середовище .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo Не вдалося створити віртуальне середовище.
    pause
    exit /b 1
  )
)

echo Встановлюю залежності...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo Не вдалося встановити залежності з requirements.txt.
  pause
  exit /b 1
)

echo Встановлюю браузер Playwright Chromium...
.venv\Scripts\playwright.exe install chromium
if errorlevel 1 (
  echo Не вдалося встановити Playwright Chromium.
  pause
  exit /b 1
)

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo Створено файл .env з .env.example. Відкрийте його і вставте BOT_TOKEN.
)

echo.
echo Встановлення успішно завершено! Тепер запустіть START_BOT.bat
pause
endlocal
