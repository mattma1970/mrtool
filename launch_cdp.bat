@echo off
rem launch_cdp.bat - open Chrome the way a human would, with a local debug port,
rem so mrtool.py --cdp can attach and drive it. Cloudflare sees a normal,
rem human-launched Chrome (no automation flags at launch) and does not
rem challenge it.
rem
rem Keep the Chrome window this opens ALREADY OPEN while you run mrtool
rem commands with --cdp.
setlocal
set "PROFILE=%~dp0profile-cdp"
set "PORT=9222"

set "CHROME="
if exist "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe" set "CHROME=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not defined CHROME (
  echo Chrome was not found in the standard install locations.
  echo Start it manually instead:
  echo   chrome.exe --user-data-dir="%PROFILE%" --remote-debugging-port=%PORT% --no-first-run https://mastersrankings.com
  exit /b 1
)

start "" "%CHROME%" --user-data-dir="%PROFILE%" --remote-debugging-port=%PORT% --no-first-run --no-default-browser-check https://mastersrankings.com

echo.
echo Chrome opened with debug port %PORT% (profile: profile-cdp).
echo WAIT for mastersrankings.com to load normally in that window, then run:
echo   .venv\Scripts\python mrtool.py --cdp check
echo
echo While that Chrome window stays open you can run, e.g.:
echo   .venv\Scripts\python mrtool.py --cdp search "Jane Smith"
echo   .venv\Scripts\python mrtool.py --cdp refresh --store
echo.
echo TIP: if mrtool says it cannot attach to port 9222, close ALL other
echo      Chrome windows first (Chrome can swallow the debug-port flag into an
echo      existing instance) and run this launcher again.
