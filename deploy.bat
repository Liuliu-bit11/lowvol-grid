@echo off
setlocal
title Push lowvol-grid to GitHub

set "USER_NAME=%~1"
set "REPO=%~2"
if "%USER_NAME%"=="" set "USER_NAME=Liuliu-bit11"
if "%REPO%"=="" set "REPO=lowvol-grid"

set "GIT_NAME=%USER_NAME%"
set "GIT_EMAIL=%USER_NAME%@users.noreply.github.com"

echo ==================================================
echo   Target: github.com/%USER_NAME%/%REPO%
echo ==================================================
echo.

where git >nul 2>nul
if errorlevel 1 goto NOGIT

if not exist ".git" (
  echo [1/4] git init ...
  git init
) else (
  echo [1/4] repo already initialized
)

git branch -M main

echo [2/4] staging all files ...
git add -A

echo [3/4] committing ...
git -c user.name="%GIT_NAME%" -c user.email="%GIT_EMAIL%" commit -m "feat: lowvol grid full market"
git rev-parse --verify HEAD >nul 2>nul
if errorlevel 1 goto NOHEAD

echo [4/4] pushing ...
git remote remove origin >nul 2>nul
git remote add origin https://github.com/%USER_NAME%/%REPO%.git

echo.
echo   ------------------------------------------------
echo   You will be asked for credentials:
echo     Username: %USER_NAME%
echo     Password: paste your Personal Access Token
echo               NOT your GitHub login password
echo   ------------------------------------------------
echo.

git push -u origin main
if errorlevel 1 goto PUSHFAIL

echo.
echo ==================================================
echo   PUSH OK
echo ==================================================
echo   Next step (manual, 1 minute):
echo     https://github.com/%USER_NAME%/%REPO%/settings/pages
echo     Source: Deploy from a branch
echo     Branch: main    Folder: /docs    then Save
echo.
echo   Then open:
echo     https://%USER_NAME%.github.io/%REPO%/
echo.
pause
exit /b 0

:NOGIT
echo [ERROR] Git is not installed on this computer.
echo   Download: https://git-scm.com/download/win
echo   Install with default options, then run this file again.
pause
exit /b 1

:NOHEAD
echo.
echo [ERROR] Commit failed, so there is no branch to push.
echo   Read the git message above. Most common cause:
echo   Git identity missing. This script passes it automatically,
echo   so if you still see this, please copy the full output to me.
pause
exit /b 1

:PUSHFAIL
echo.
echo [PUSH FAILED] Common causes:
echo   1. The repository does not exist on GitHub yet, or the name differs.
echo      Create it at https://github.com/new  (no README, no .gitignore)
echo   2. Password was your login password. It must be a Personal Access Token
echo      with "repo" scope: https://github.com/settings/tokens
echo   3. The remote already has commits (README was auto-created). Then run:
echo        git pull origin main --allow-unrelated-histories
echo        git push -u origin main
echo.
pause
exit /b 1
