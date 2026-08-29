@echo off
setlocal
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo chrome_not_found
  exit /b 1
)
set "PROFILE=%LOCALAPPDATA%\Google\Chrome\User Data\CDP_Profile"
set "TARGET_URL=%~1"
if "%TARGET_URL%"=="" set "TARGET_URL=https://warszawa.pasport.org.ua/solutions/e-queue"
set "CDP_PORT=%~2"
if "%CDP_PORT%"=="" set "CDP_PORT=9222"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0free_cdp_port.ps1" -Port %CDP_PORT%
if errorlevel 1 (
  echo failed_to_free_cdp_port port=%CDP_PORT%
  exit /b 1
)

echo Starting dedicated CDP Chrome
echo profile="%PROFILE%"
echo target="%TARGET_URL%"
start "" "%CHROME%" --remote-debugging-address=127.0.0.1 --remote-debugging-port=%CDP_PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check "%TARGET_URL%"

powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(15); $target=([uri]$env:TARGET_URL).Host; do { try { $tabs=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$env:CDP_PORT+'/json/list') -TimeoutSec 1; $hosts=@($tabs | ForEach-Object { try { ([uri]$_.url).Host } catch {} }); if ($hosts -contains $target) { exit 0 } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
  echo cdp_target_tab_not_visible
  exit /b 1
)
echo CDP_URL=http://127.0.0.1:%CDP_PORT%
echo Complete any Cloudflare challenge in the opened target tab before starting the bot.
endlocal
