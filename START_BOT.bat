@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Спочатку запустіть setup.bat
  pause
  exit /b 1
)

echo Запускаю dedicated Chrome з CDP...
start "" /min cmd /c "scripts\start_chrome_cdp.bat"
timeout /t 3 /nobreak >nul

echo Запускаю бота...
.venv\Scripts\python.exe -m src.main
pause
endlocal
