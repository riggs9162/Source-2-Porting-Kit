@echo off
setlocal
cd /d "%~dp0"

rem With no arguments, build one self-contained .exe with no side files.
rem Any arguments given are forwarded verbatim instead, so:
rem   build_exe.bat --onedir    folder distribution (faster startup)
rem   build_exe.bat --release   distribution build (clean + isolated venv)
set "BUILD_ARGS=%*"
if "%~1"=="" set "BUILD_ARGS=--onefile"

where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 build_exe.py %BUILD_ARGS%
) else (
    python build_exe.py %BUILD_ARGS%
)

exit /b %errorlevel%
