@echo off
setlocal enabledelayedexpansion

:: =======================================================
::   ESP32 Universal Flash Tool (Firmware + Filesystem)
:: =======================================================
:: Instructions:
:: 1. This script will automatically try to find 'arduino-cli'.
:: 2. Set the correct PORT below.
:: =======================================================

set "PORT=COM3"
set "FQBN=esp32:esp32:esp32"
set "SKETCH_DIR=ev_charging_esp32_flash"
set "DATA_DIR=%SKETCH_DIR%\data"
set "OFFSET=0x290000"
set "FS_SIZE=1441792"

:: Detect Local Python Venv
set "VENV_PYTHON="
if exist "venv\Scripts\python.exe" set "VENV_PYTHON=venv\Scripts\python.exe"

:: [SEARCH] Locate arduino-cli
echo [+] Locating arduino-cli...
set "ARDUINO_CLI=arduino-cli"
where arduino-cli >nul 2>&1
if !errorlevel! neq 0 (
    if exist "C:\ArduinoCLI\arduino-cli.exe" (
        set "ARDUINO_CLI=C:\ArduinoCLI\arduino-cli.exe"
        echo [+] Found arduino-cli in C:\ArduinoCLI
    ) else (
        :: Fallback to searching LocalAppData
        set "FOUND="
        for /d %%d in ("%LOCALAPPDATA%\Arduino15\packages\arduino\tools\arduino-cli\*") do (
            if exist "%%d\arduino-cli.exe" (
                set "ARDUINO_CLI=%%d\arduino-cli.exe"
                set "FOUND=1"
            )
        )
        if "!FOUND!"=="" (
            echo [X] ERROR: 'arduino-cli' not found.
            pause
            exit /b 1
        )
    )
)

echo [+] Target Port: %PORT%
echo [+] Board: %FQBN%
echo.

:: 1. Gzip Assets for Performance
echo [1/4] Optimizing Static Assets (Gzip)...
if "!VENV_PYTHON!" neq "" (
    "!VENV_PYTHON!" -c "import gzip, shutil, os; src='%DATA_DIR%/chart.umd.min.js'; dst=src+'.gz'; (open(dst, 'wb').write(gzip.compress(open(src, 'rb').read())) if os.path.exists(src) else print('Skipping...'))"
) else (
    python -c "import gzip, shutil, os; src='%DATA_DIR%/chart.umd.min.js'; dst=src+'.gz'; (open(dst, 'wb').write(gzip.compress(open(src, 'rb').read())) if os.path.exists(src) else print('Skipping...'))"
)

:: 2. Compile and Upload Firmware
echo [2/4] Compiling and Uploading Firmware...
"%ARDUINO_CLI%" compile --upload -p %PORT% -b %FQBN% %SKETCH_DIR%\ev_charging_sim.ino
if %errorlevel% neq 0 (
    echo [X] ERROR: Firmware flash failed.
    pause
    exit /b 1
)

:: 3. Locate Tools (mklittlefs)
echo [3/4] Locating FileSystem Tools...
set "MKLITTLEFS="
where mklittlefs >nul 2>&1
if !errorlevel! equ 0 (
    for /f "delims=" %%i in ('where mklittlefs') do set "MKLITTLEFS=%%i"
) else (
    for /d %%d in ("%LOCALAPPDATA%\Arduino15\packages\esp32\tools\mklittlefs\*") do (
        if exist "%%d\mklittlefs.exe" set "MKLITTLEFS=%%d\mklittlefs.exe"
    )
)

if "!MKLITTLEFS!"=="" (
    echo [X] ERROR: 'mklittlefs' not found.
    pause
    exit /b 1
)

:: 4. Build and Flash LittleFS
echo [4/4] Building and Flashing LittleFS Image...
"!MKLITTLEFS!" -c "%DATA_DIR%" -p 256 -b 4096 -s %FS_SIZE% littlefs.bin
if %errorlevel% neq 0 (
    echo [X] ERROR: Failed to create LittleFS image.
    pause
    exit /b 1
)

:: Flashing LittleFS using esptool
echo [+] Preparing to flash Filesystem...
set "ESPTOOL_CMD="

:: Priority 1: Use the esptool version that comes with the ESP32 core via arduino-cli
for /d %%d in ("%LOCALAPPDATA%\Arduino15\packages\esp32\tools\esptool_py\*") do (
    if exist "%%d\esptool.exe" set "ESPTOOL_CMD="%%d\esptool.exe""
)

:: Priority 2: Use venv python -m esptool
if "!ESPTOOL_CMD!"=="" (
    if "!VENV_PYTHON!" neq "" (
        set "ESPTOOL_CMD="!VENV_PYTHON!" -m esptool"
    )
)

:: Priority 3: Global esptool
if "!ESPTOOL_CMD!"=="" (
    where esptool >nul 2>&1
    if !errorlevel! equ 0 set "ESPTOOL_CMD=esptool"
)

if "!ESPTOOL_CMD!"=="" (
    echo [!] Warning: Could not find esptool. Attempting to install it in venv...
    if "!VENV_PYTHON!" neq "" (
        "!VENV_PYTHON!" -m pip install esptool
        set "ESPTOOL_CMD="!VENV_PYTHON!" -m esptool"
    ) else (
        echo [X] ERROR: No python/esptool found to flash filesystem.
        pause
        exit /b 1
    )
)

echo [+] Executing: %ESPTOOL_CMD% --chip esp32 --port %PORT% --baud 921600 write_flash %OFFSET% littlefs.bin
%ESPTOOL_CMD% --chip esp32 --port %PORT% --baud 921600 write_flash %OFFSET% littlefs.bin

if %errorlevel% neq 0 (
    echo [X] ERROR: Filesystem flash failed.
    pause
    exit /b 1
)

echo.
echo =======================================================
echo   SUCCESS: ESP32 is Fully Flashed and Optimized!
echo =======================================================
echo.
pause
