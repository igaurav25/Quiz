@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   QuizFlow - Seeding Demo Quizzes & Questions
echo ===================================================
echo.

cd /d "%~dp0\.."

set "PY_EXE="
if exist "%~dp0..\.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0..\.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe" (
    set "PY_EXE=%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"
) else (
    python --version >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        set "PY_EXE=python"
    )
)

if not defined PY_EXE (
    echo [ERROR] Python 3.10+ was not found.
    pause
    exit /b 1
)

cd /d "%~dp0\..\backend"
echo [*] Seeding demo quiz and questions into quizflow.db...
echo.
"!PY_EXE!" -m app.cli seed-demo

echo.
pause
