@echo off
setlocal EnableExtensions
title Jarvis - Health check
set "APP_DIR=%USERPROFILE%\Jarvis"
if not exist "%APP_DIR%\.venv\Scripts\python.exe" (
  echo Jarvis is not installed in %APP_DIR%.
  pause
  exit /b 1
)
echo.
echo  Jarvis health check
echo  ---------------------
echo  Please CLOSE Jarvis first if it is open.
echo  This checks everything, then starts Jarvis for about 40 seconds to read what it says,
echo  then closes it again. It takes a few minutes. No keys or passwords are shown.
echo.
pause
"%APP_DIR%\.venv\Scripts\python.exe" "%~dp0diagnose.py" "%APP_DIR%"
echo.
echo  Open the report file named above, copy ALL of it, and paste it into the chat.
pause
