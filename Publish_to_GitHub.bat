@echo off
setlocal EnableExtensions
title Jarvis - publish to GitHub
cd /d "%~dp0"
echo.
echo  Publish this folder to a new GitHub repository
echo  ----------------------------------------------
where git >nul 2>&1
if errorlevel 1 (
  echo  Git is not installed. Get it from https://git-scm.com/download/win and run this again.
  pause
  exit /b 1
)
if not exist ".git" (
  echo  This folder is not a git repository - it should contain a .git folder.
  pause
  exit /b 1
)
git remote get-url origin >nul 2>&1
if not errorlevel 1 (
  echo  A remote called origin already exists. Pushing to it...
  git push -u origin main
  pause
  exit /b 0
)
where gh >nul 2>&1
if errorlevel 1 (
  echo  The GitHub command-line tool was not found, so do it by hand - it takes a minute:
  echo.
  echo    1. On github.com click New repository, name it Jarvis, leave it EMPTY, and create it.
  echo    2. Then run these two commands in this folder:
  echo         git remote add origin https://github.com/YOUR-NAME/Jarvis.git
  echo         git push -u origin main
  echo.
  echo  Details are in docs\PUBLISH.md
  pause
  exit /b 0
)
gh auth status >nul 2>&1
if errorlevel 1 (
  echo  Signing in to GitHub first...
  gh auth login
)
echo  Creating a PRIVATE repository called Jarvis and pushing...
gh repo create Jarvis --private --source . --remote origin --push
if errorlevel 1 (
  echo.
  echo  That did not work. Is there already a repository called Jarvis on your account?
  echo  See docs\PUBLISH.md for the manual way.
) else (
  echo.
  echo  Done. To make it public later: gh repo edit --visibility public
)
pause
