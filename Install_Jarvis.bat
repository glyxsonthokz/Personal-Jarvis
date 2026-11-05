@echo off
setlocal EnableExtensions
title Jarvis - Installer
echo.
echo  ==========================================
echo    JARVIS  -  one-click installer
echo    (with walkthrough + phone-link add-ons)
echo  ==========================================
echo.

if not exist "%~dp0addons\install_addons.py" (
  echo The "addons" folder is missing next to this file.
  echo Extract the WHOLE zip first, then run the installer from the extracted folder.
  pause
  exit /b 1
)

set "APP_DIR=%USERPROFILE%\Jarvis"
set "ZIP_URL=https://github.com/FatihMakes/Mark-LVI/archive/refs/heads/main.zip"
set "TMP_DIR=%TEMP%\mark-lvi-install"
set "PY="
set "OLD_DIR=%USERPROFILE%\Mark-LVI"

rem ---- Coming from the earlier "Mark LVI" install? Bring the user's own data across (never the old code or venv).
if exist "%OLD_DIR%\main.py" if not exist "%APP_DIR%\main.py" (
  echo Found your earlier install in %OLD_DIR%
  echo Bringing your settings, memory and downloads across to %APP_DIR% ...
  for %%D in (config memory models tools) do (
    if exist "%OLD_DIR%\%%D" robocopy "%OLD_DIR%\%%D" "%APP_DIR%\%%D" /E /NFL /NDL /NJH /NJS /NP >nul
  )
)

echo [1/6] Looking for Python 3.11 - 3.13...
for %%V in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  echo Python was not found. Installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  echo.
  echo Python is installed. Please CLOSE this window and run the installer again.
  pause
  exit /b 1
)
echo       Using: %PY%

echo [2/6] Downloading Jarvis from GitHub...
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
mkdir "%TMP_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest '%ZIP_URL%' -OutFile '%TMP_DIR%\app.zip'; Expand-Archive '%TMP_DIR%\app.zip' '%TMP_DIR%' -Force"
if errorlevel 1 (
  echo Download failed. Check your internet connection and try again.
  pause
  exit /b 1
)

echo [3/6] Copying files to %APP_DIR%  (your API key and memory are kept on re-install)...
robocopy "%TMP_DIR%\Mark-LVI-main" "%APP_DIR%" /E /XF api_keys.json long_term.json /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo Copy failed.
  pause
  exit /b 1
)
rmdir /s /q "%TMP_DIR%"

echo [4/6] Adding the add-ons: walkthroughs + phone link...
%PY% "%~dp0addons\install_addons.py" "%APP_DIR%"
if errorlevel 1 (
  echo The add-on could not be applied - see the messages above.
  pause
  exit /b 1
)

echo [5/6] Installing dependencies - this takes a few minutes the first time...
cd /d "%APP_DIR%"
if not exist ".venv\Scripts\python.exe" %PY% -m venv ".venv"
if not exist ".venv\Scripts\python.exe" (
  echo Could not create the virtual environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" setup.py
if errorlevel 1 (
  echo Setup reported an error - see the messages above.
  pause
  exit /b 1
)

echo [6/6] Creating shortcuts...
(
  echo @echo off
  echo cd /d "%APP_DIR%"
  echo ".venv\Scripts\python.exe" main.py
  echo pause
) > "%APP_DIR%\Run with console.bat"

powershell -NoProfile -Command "foreach($f in 'Desktop','Programs'){ $d=[Environment]::GetFolderPath($f); $s=(New-Object -ComObject WScript.Shell).CreateShortcut($d+'\Jarvis.lnk'); $s.TargetPath='%APP_DIR%\.venv\Scripts\pythonw.exe'; $s.Arguments='main.py'; $s.WorkingDirectory='%APP_DIR%'; $s.Description='Jarvis voice assistant'; $s.Save() }"

rem ---- Remove the old "Mark LVI" shortcuts so there is only one icon.
powershell -NoProfile -Command "foreach($f in 'Desktop','Programs'){ $p=[Environment]::GetFolderPath($f)+'\Mark LVI.lnk'; if(Test-Path $p){ Remove-Item $p -Force } }"
if exist "%OLD_DIR%\main.py" (
  echo.
  echo  Your old folder is still at %OLD_DIR% - once Jarvis works for you, you can delete it.
)
echo.
echo  Done!  Double-click "Jarvis" on your Desktop or in the Start menu.
echo  On first launch, paste your free Gemini API key when asked:
echo  https://aistudio.google.com/app/apikey
echo.
echo  Try: "walk me through changing my wallpaper"  or  "set up my phone".
echo.
start "" /d "%APP_DIR%" "%APP_DIR%\.venv\Scripts\pythonw.exe" main.py
pause
