@echo off
setlocal enabledelayedexpansion

echo =======================================================
echo   Smart EV Charging - Universal Windows Setup
echo =======================================================
echo.

:: 1. Detect Stable Python Version
set "PYTHON_CMD="
echo Investigating installed Python versions...

py -3.12 --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PYTHON_CMD=py -3.12"
    echo [+] Found Python 3.12 (Highly Recommended)
) else (
    py -3.13 --version >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=py -3.13"
        echo [+] Found Python 3.13 (Stable)
    ) else (
        python --version >nul 2>&1
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=python"
            echo [!] Using default 'python' command. 
            echo [!] WARNING: If this is Python 3.14+, installation may fail.
        )
    )
)

if "%PYTHON_CMD%" == "" (
    echo [X] ERROR: No Python interpreter found. Please install Python 3.12.
    pause
    exit /b 1
)

:: 2. Create Virtual Environment
echo.
echo Creating Virtual Environment (venv) using %PYTHON_CMD%...
if exist venv (
    echo [!] Removing existing venv folder...
    rmdir /s /q venv
)
%PYTHON_CMD% -m venv venv
if !errorlevel! neq 0 (
    echo [X] ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

:: Verify venv version
echo.
echo Verifying venv Python version...
.\venv\Scripts\python --version
if !errorlevel! neq 0 (
    echo [X] ERROR: venv is broken.
    pause
    exit /b 1
)

:: 3. Install Dependencies
echo.
echo Installing Stable Dependency Stack...
echo This may take several minutes (downloading ~500MB)...
echo.

.\venv\Scripts\python -m pip install --upgrade pip
.\venv\Scripts\pip install "numpy<2.0.0" "opencv-python==4.9.0.80" --no-cache-dir
.\venv\Scripts\pip install "torch<2.4.0" "torchvision<0.19.0" "ultralytics<8.3.0" --index-url https://download.pytorch.org/whl/cpu --no-cache-dir

if !errorlevel! equ 0 (
    echo.
    echo =======================================================
    echo   SUCCESS: Environment Setup Complete!
    echo =======================================================
    echo To run the application, use: .\run.bat
    echo.
) else (
    echo.
    echo [X] ERROR: Installation failed. Please check your internet connection.
)

pause
