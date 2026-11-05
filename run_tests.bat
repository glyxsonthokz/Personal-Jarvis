@echo off
set "APP_DIR=%USERPROFILE%\Jarvis"
if not exist "%APP_DIR%\.venv\Scripts\python.exe" (
  echo Jarvis is not installed in %APP_DIR% - run Install_Jarvis.bat first.
  pause
  exit /b 1
)
set "JARVIS_DIR=%APP_DIR%"
"%APP_DIR%\.venv\Scripts\python.exe" "%~dp0tests\run_all.py"
pause
