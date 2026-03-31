@echo off
echo Starting Smart EV Charging Slot Detection...
call venv\Scripts\activate
if %errorlevel% neq 0 (
    echo Failed to activate virtual environment. Is it created?
    pause
    exit /b
)
python main.py
pause
