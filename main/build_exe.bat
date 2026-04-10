@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 build_exe.py %*
) else (
    python build_exe.py %*
)

exit /b %errorlevel%
