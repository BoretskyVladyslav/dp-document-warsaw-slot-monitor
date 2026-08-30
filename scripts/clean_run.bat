@echo off
setlocal
set "CDP_PORT=%~1"
if "%CDP_PORT%"=="" set "CDP_PORT=9222"
for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "DB_PATH=%~2"
if "%DB_PATH%"=="" set "DB_PATH=%REPO_ROOT%\data\monitor.db"
set "PYTHON=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0clean_run.ps1" -Port %CDP_PORT%
if errorlevel 1 exit /b 1

timeout /t 1 /nobreak >nul

echo Resetting monitor_state and pending outbox (subscribers preserved)...
"%PYTHON%" "%~dp0reset_monitor_state.py" "%DB_PATH%"
if errorlevel 1 exit /b 1

echo.
echo Cleanup complete. Next:
echo   1. scripts\start_chrome_cdp.bat
echo   2. Complete Cloudflare in the opened target tab if shown.
echo   3. .venv\Scripts\python.exe -m src.main
endlocal
