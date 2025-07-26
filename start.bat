@echo off
echo Starting Source 2 Porting Kit...
echo.

REM Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found! Please install Python 3.6 or higher.
    echo.
    pause
    exit /b 1
)

REM Check for required packages
echo Checking dependencies...
python -c "import PIL" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing Pillow (Python Imaging Library)...
    python -m pip install Pillow
)

python -c "import tkinterdnd2" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing tkinterdnd2 (for drag and drop support)...
    python -m pip install tkinterdnd2
)

python -c "import pydub" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing pydub (for audio processing)...
    python -m pip install pydub
    echo Note: For audio conversion, you may need to install ffmpeg separately.
)

echo.
echo Starting Modular Source 2 Porting Kit...
python porter_modular.py
pause
