@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   QuizFlow - Starting CustomTkinter Desktop Client
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

cd /d "%~dp0\..\desktop"
if not exist "main.py" (
    echo [ERROR] Could not find desktop main.py in %cd%
    pause
    exit /b 1
)

echo [*] Launching desktop application with !PY_EXE!...
echo [*] Ensure the backend server is running in another window (or let desktop auto-launch it).
echo.

"!PY_EXE!" main.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [NOTE] Desktop application exited with code %ERRORLEVEL%.
    pause
)
