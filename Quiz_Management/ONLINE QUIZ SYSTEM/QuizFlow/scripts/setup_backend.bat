@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   QuizFlow - Backend Environment Setup
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
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        echo [*] Setting up virtual environment via uv...
        "%USERPROFILE%\.local\bin\uv.exe" venv "%~dp0..\.venv" --python 3.12
        set "PY_EXE=%~dp0..\.venv\Scripts\python.exe"
    ) else (
        echo [ERROR] Python was not found in your system PATH.
        echo Please install Python 3.10+ from https://www.python.org/
        pause
        exit /b 1
    )
)

echo [*] Using Python: !PY_EXE!
echo [*] Installing dependencies...

if exist "%USERPROFILE%\.local\bin\uv.exe" (
    "%USERPROFILE%\.local\bin\uv.exe" pip install --python "!PY_EXE!" -r "%~dp0..\backend\requirements.txt" -r "%~dp0..\desktop\requirements.txt"
) else (
    "!PY_EXE!" -m pip install -r "%~dp0..\backend\requirements.txt" -r "%~dp0..\desktop\requirements.txt"
)

echo.
echo [*] Initializing database and bootstrapping administrator...
cd backend
"!PY_EXE!" -c "from app.database.init_db import init_db, bootstrap_admin; init_db(); bootstrap_admin()"

echo.
echo ===================================================
echo   Setup Completed Successfully!
echo   You can now launch QuizFlow via START_QUIZFLOW.bat
echo ===================================================
echo.
pause
