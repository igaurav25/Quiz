@echo off
setlocal

title QuizFlow Enterprise Assessment Platform
cd /d "%~dp0"

echo ===================================================
echo        QUIZFLOW ENTERPRISE ASSESSMENT SUITE
echo ===================================================
echo.
echo [*] Initializing QuizFlow environment...

:: 1. Locate Python Executable
set "PY_EXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PY_EXE if exist "%~dp0..\.venv\Scripts\python.exe" set "PY_EXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PY_EXE if exist "%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe" set "PY_EXE=%USERPROFILE%\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"

if not defined PY_EXE (
    python --version >nul 2>&1
    if %ERRORLEVEL% EQU 0 set "PY_EXE=python"
)

if not defined PY_EXE (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        echo [*] Auto-setting up Python virtual environment via uv...
        "%USERPROFILE%\.local\bin\uv.exe" venv "%~dp0.venv" --python 3.12
        "%USERPROFILE%\.local\bin\uv.exe" pip install --python "%~dp0.venv\Scripts\python.exe" -r "%~dp0backend\requirements.txt" -r "%~dp0desktop\requirements.txt"
        set "PY_EXE=%~dp0.venv\Scripts\python.exe"
    )
)

if not defined PY_EXE (
    echo.
    echo [ERROR] Python 3.10+ was not found on your system!
    echo Please install Python from https://www.python.org/
    echo.
    pause
    exit /b 1
)

echo [*] Using Python: %PY_EXE%

:: 2. Ensure database integrity and demo accounts
echo [*] Checking database integrity and demo accounts...
"%PY_EXE%" -c "import sys; sys.path.insert(0, 'backend'); from app.database.init_db import init_db, bootstrap_admin, bootstrap_demo_accounts; init_db(); bootstrap_admin(); bootstrap_demo_accounts()"

:: 3. Launch Desktop Application (which auto-launches backend)
echo [*] Launching QuizFlow Desktop UI...
cd /d "%~dp0desktop"
"%PY_EXE%" main.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [NOTE] QuizFlow exited with code %ERRORLEVEL%.
    pause
)

exit /b 0
