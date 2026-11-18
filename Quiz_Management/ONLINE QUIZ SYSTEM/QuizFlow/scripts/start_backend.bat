@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   QuizFlow - Starting FastAPI Backend Server
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
    echo [ERROR] Python 3.10+ was not found. Please install Python or set up the virtual environment.
    pause
    exit /b 1
)

cd /d "%~dp0\..\backend"
if not exist "app\main.py" (
    echo [ERROR] Could not find app\main.py in %cd%
    pause
    exit /b 1
)

echo [*] Server will listen on http://127.0.0.1:8000
echo [*] Interactive API Docs: http://localhost:8000/docs
echo [*] Health Check URL:    http://localhost:8000/health
echo.
echo Press Ctrl+C in this terminal to stop the server.
echo ===================================================
echo.

"!PY_EXE!" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
