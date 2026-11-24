@echo off
rem Gets the latest Clipwise version (if installed with Git) and refreshes packages.
rem Your clips, settings and API keys in the "data" folder are never touched.
title Clipwise update
cd /d "%~dp0"
if exist ".git" (
  echo  Downloading the latest version...
  git pull --ff-only || ( echo. & echo  Update failed - check your internet connection. & pause & exit /b 1 )
)
if exist ".venv\Scripts\python.exe" (
  echo  Updating packages...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt >nul
  ".venv\Scripts\python.exe" -m pip install --upgrade "yt-dlp[default]" deno >nul
  copy /y requirements.txt ".venv\requirements.installed" >nul
)
echo.
set /p V=<VERSION
echo  Clipwise %V% is up to date. Start it with start.bat.
pause
