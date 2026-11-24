@echo off
setlocal
title Clipwise
cd /d "%~dp0"

rem ---- 1. Find Python 3.10-3.12 -------------------------------------------
set "PY="
for %%V in (3.12 3.11 3.10) do (
  if not defined PY ( py -%%V -c "pass" >nul 2>&1 && set "PY=py -%%V" )
)
if not defined PY ( python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,12) else 1)" >nul 2>&1 && set "PY=python" )
if not defined PY (
  echo.
  echo  Python 3.12 is required. Installing it now with winget...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo  Done. Please close this window and double-click start.bat again.
  pause
  exit /b 1
)

rem ---- 2. Create the virtual environment + install packages (first run only)
if not exist ".venv\Scripts\python.exe" (
  echo  Creating virtual environment...
  %PY% -m venv .venv || goto :fail
)
set "NEED_INSTALL="
if not exist ".venv\requirements.installed" set "NEED_INSTALL=1"
if not defined NEED_INSTALL ( fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1 || set "NEED_INSTALL=1" )
if defined NEED_INSTALL (
  echo  Installing packages ^(first run or after an update, a few minutes^)...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  copy /y requirements.txt ".venv\requirements.installed" >nul
  echo ok> ".venv\.installed"
)

rem ---- 3. Run ----------------------------------------------------------------
".venv\Scripts\python.exe" -m app
goto :eof

:fail
echo.
echo  Setup failed. Check your internet connection and try again.
pause
exit /b 1
