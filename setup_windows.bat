@echo off
setlocal enabledelayedexpansion

:: Get current directory with trailing backslash
set "BASE_DIR=%~dp0"

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
    echo [+] Found Python 3.12
) else (
    py -3.13 --version >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=py -3.13"
        echo [+] Found Python 3.13
    ) else (
        python --version >nul 2>&1
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=python"
            echo [!] Using default python
        )
    )
)

if "%PYTHON_CMD%" == "" (
    echo [X] ERROR: No Python found.
    echo Please install Python 3.12 or 3.13 from python.org.
    pause
    exit /b 1
)

:: 2. Detect Version and Set Dependencies
for /f "tokens=*" %%v in ('%PYTHON_CMD% -c "import sys; print(sys.version_info.minor)"') do set "PY_MINOR=%%v"

set "TORCH_SPEC=torch<2.4.0"
set "VISION_SPEC=torchvision<0.19.0"
set "NUMPY_SPEC=numpy<2.0.0"

if "%PY_MINOR%" == "13" (
    echo [!] Detected Python 3.13 - Adjusting constraints...
    set "TORCH_SPEC=torch>=2.6.0"
    set "VISION_SPEC=torchvision>=0.21.0"
    set "NUMPY_SPEC=numpy>=2.1.0"
)

:: 3. Create Virtual Environment
echo.
echo Creating Virtual Environment...
if exist "%BASE_DIR%venv" (
    echo [!] Attempting to refresh existing venv folder...
    rmdir /s /q "%BASE_DIR%venv" 2>nul
)

%PYTHON_CMD% -m venv "%BASE_DIR%venv"
if !errorlevel! neq 0 (
    echo.
    echo [X] ERROR: Failed to create venv.
    echo [TIP] Close VS Code and other terminals, then try again.
    pause
    exit /b 1
)

:: Verify venv
echo.
echo Verifying venv...
if not exist "%BASE_DIR%venv\Scripts\python.exe" goto :broken_venv
"%BASE_DIR%venv\Scripts\python.exe" --version
if !errorlevel! neq 0 goto :broken_venv

:: 4. Install Dependencies
echo.
echo Install Dependencies...

"%BASE_DIR%venv\Scripts\python.exe" -m pip install --upgrade pip
if !errorlevel! neq 0 echo [!] Pip upgrade failed, continuing...

"%BASE_DIR%venv\Scripts\python.exe" -m pip install "%NUMPY_SPEC%" "opencv-python==4.9.0.80" "supervision" "flask" "flask-cors" --no-cache-dir
"%BASE_DIR%venv\Scripts\python.exe" -m pip install "%TORCH_SPEC%" "%VISION_SPEC%" "ultralytics<8.3.0" --extra-index-url https://download.pytorch.org/whl/cpu --no-cache-dir

if !errorlevel! neq 0 goto :install_failed

echo.
echo Verifying installation...
"%BASE_DIR%venv\Scripts\python.exe" -c "import cv2, torch, ultralytics; print('[+] All core libraries loaded successfully.')"
if !errorlevel! neq 0 (
    echo [X] ERROR: Verification failed.
)

echo.
echo =======================================================
echo   SUCCESS: Environment Setup Complete!
echo =======================================================
echo To run the application, use: .\run.bat
echo.
pause
exit /b 0

:broken_venv
echo [X] ERROR: Virtual environment is broken or inaccessible.
echo Close all other programs and try again.
pause
exit /b 1

:install_failed
echo [X] ERROR: Installation failed.
pause
exit /b 1
