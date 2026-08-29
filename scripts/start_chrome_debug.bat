@echo off
setlocal
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo chrome_not_found: install Google Chrome or edit this script
  exit /b 1
)

set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=%LOCALAPPDATA%\Google\Chrome\User Data"

echo Starting Chrome with remote debugging on port 9222
echo chrome="%CHROME%"
echo profile="%PROFILE%"
echo Close other Chrome windows first if this profile is already in use.
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%"
echo CDP_URL=http://localhost:9222
endlocal
