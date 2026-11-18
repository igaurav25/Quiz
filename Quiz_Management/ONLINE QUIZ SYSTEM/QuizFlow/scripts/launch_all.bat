@echo off
setlocal enabledelayedexpansion

title QuizFlow Launcher
cd /d "%~dp0\.."

echo ===================================================
echo   QuizFlow - Unified Platform Launcher
echo ===================================================
echo.

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

echo [*] Starting FastAPI Backend Server...
start "QuizFlow Backend Server" /min cmd /c "cd /d "%~dp0..\backend" && "!PY_EXE!" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

ping -n 3 127.0.0.1 >nul

echo [*] Launching Desktop Application...
cd /d "%~dp0..\desktop"
"!PY_EXE!" main.py
