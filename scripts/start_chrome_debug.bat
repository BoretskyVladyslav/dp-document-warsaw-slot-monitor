@echo off
setlocal
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo chrome_not_found: install Google Chrome or edit this script
  exit /b 1
)

set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=%LOCALAPPDATA%\Google\Chrome\User Data\CDP_Profile"
set "TARGET_URL=%~2"
if "%TARGET_URL%"=="" set "TARGET_URL=https://warszawa.pasport.org.ua/solutions/e-queue"
set "CDP_PORT=%~3"
if "%CDP_PORT%"=="" set "CDP_PORT=9222"

powershell -NoProfile -Command "try { $tabs=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$env:CDP_PORT+'/json/list') -TimeoutSec 1; $target=([uri]$env:TARGET_URL).Host; $hosts=@($tabs | ForEach-Object { try { ([uri]$_.url).Host } catch {} }); if ($hosts -contains $target) { exit 10 }; exit 11 } catch { exit 0 }"
if errorlevel 11 (
  echo cdp_port_in_use_by_another_process port=%CDP_PORT%
  echo Stop that process or pass another port as the third argument and update CDP_URL.
  exit /b 1
)
if errorlevel 10 goto cdp_ready

echo Starting dedicated Chrome with remote debugging on port %CDP_PORT%
echo chrome="%CHROME%"
echo profile="%PROFILE%"
echo target="%TARGET_URL%"
start "" "%CHROME%" --remote-debugging-address=127.0.0.1 --remote-debugging-port=%CDP_PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check "%TARGET_URL%"

powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(15); $target=([uri]$env:TARGET_URL).Host; do { try { $tabs=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$env:CDP_PORT+'/json/list') -TimeoutSec 1; $hosts=@($tabs | ForEach-Object { try { ([uri]$_.url).Host } catch {} }); if ($hosts -contains $target) { exit 0 } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
  echo cdp_target_tab_not_visible
  exit /b 1
)
:cdp_ready
echo CDP_URL=http://127.0.0.1:%CDP_PORT%
echo Complete any Cloudflare challenge in the opened target tab before starting the bot.
endlocal
