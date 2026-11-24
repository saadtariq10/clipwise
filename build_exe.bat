@echo off
rem Builds dist\Clipwise.exe (single file, no Python needed on the target PC).
cd /d "%~dp0"
if not exist ".venv\.installed" (
  echo  Run start.bat once first so the environment is set up.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller || goto :fail
".venv\Scripts\python.exe" build_exe.py || goto :fail
echo.
echo  Built dist\Clipwise.exe - copy it anywhere and double-click to run.
pause
exit /b 0

:fail
echo.
echo  Build failed - see the messages above.
pause
exit /b 1
