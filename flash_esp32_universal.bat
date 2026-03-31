@echo off
setlocal enabledelayedexpansion

:: =======================================================
::   ESP32 Universal Flash Tool (Firmware + Filesystem)
:: =======================================================
:: Instructions:
:: 1. Ensure 'arduino-cli' is in your PATH.
:: 2. Set the correct PORT below.
:: =======================================================

set "PORT=COM3"
set "FQBN=esp32:esp32:esp32"
set "SKETCH_DIR=ev_charging_sim"
set "DATA_DIR=%SKETCH_DIR%\data"
set "OFFSET=0x290000"
set "FS_SIZE=1441792"

echo [+] Target Port: %PORT%
echo [+] Board: %FQBN%
echo.

:: 1. Gzip Assets for Performance
echo [1/4] Optimizing Static Assets (Gzip)...
python -c "import gzip, shutil, os; src='ev_charging_sim/data/chart.umd.min.js'; dst=src+'.gz'; (open(dst, 'wb').write(gzip.compress(open(src, 'rb').read())) if os.path.exists(src) else print('Skipping...'))"
if %errorlevel% neq 0 echo [!] Warning: Gzip optimization skipped (Python/Gzip missing).

:: 2. Compile and Upload Firmware
echo [2/4] Compiling and Uploading Firmware...
arduino-cli compile --upload -p %PORT% -b %FQBN% %SKETCH_DIR%\ev_charging_sim.ino
if %errorlevel% neq 0 (
    echo [X] ERROR: Firmware flash failed. Check if board is in BOOT mode.
    pause
    exit /b 1
)

:: 3. Locate Tools (mklittlefs and esptool)
echo [3/4] Locating FileSystem Tools...
set "MKLITTLEFS="
for /f "delims=" %%i in ('where mklittlefs 2^>nul') do set "MKLITTLEFS=%%i"
if "!MKLITTLEFS!"=="" (
    :: Try common Arduino location
    for /d %%d in ("%LOCALAPPDATA%\Arduino15\packages\esp32\tools\mklittlefs\*") do (
        if exist "%%d\mklittlefs.exe" set "MKLITTLEFS=%%d\mklittlefs.exe"
    )
)

if "!MKLITTLEFS!"=="" (
    echo [X] ERROR: 'mklittlefs' not found. Please add it to your PATH.
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

:: Find esptool
arduino-cli upload -p %PORT% -b %FQBN% --upload-properties "UploadSpeed=921600" --verify %SKETCH_DIR%\ev_charging_sim.ino >nul 2>&1
:: We use esptool directly for the partition offset
esptool --chip esp32 --port %PORT% --baud 921600 write_flash %OFFSET% littlefs.bin
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
