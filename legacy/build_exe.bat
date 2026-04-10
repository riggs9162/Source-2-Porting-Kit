@echo off
echo Building Source 2 Porting Kit Executable...
echo.

REM Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found! Please install Python 3.8 or higher.
    echo.
    pause
    exit /b 1
)

REM Check if PyInstaller is installed
python -c "import PyInstaller" >nul 2>&1
if %errorlevel% neq 0 (
    echo PyInstaller not found. Installing...
    python -m pip install pyinstaller
)

REM Install required dependencies
echo Installing dependencies...
python -m pip install -r requirements.txt

REM Clean previous builds
echo Cleaning previous builds...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "__pycache__" rmdir /s /q "__pycache__"

REM Build the executable
echo Building executable...
python -m PyInstaller porter.spec

if %errorlevel% equ 0 (
    echo.
    echo Build completed successfully!
    echo Executable location: dist\"Source 2 Porting Kit.exe"
    echo.
    echo You can now distribute the files in the 'dist' folder.
    echo The executable includes all necessary dependencies.
) else (
    echo.
    echo Build failed! Check the output above for errors.
)

echo.
pause
