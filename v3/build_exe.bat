@echo off
setlocal
cd /d "%~dp0"

rem Builds one self-contained .exe in dist\. Arguments are forwarded, so:
rem   build_exe.bat --onedir    folder build with faster startup
rem   build_exe.bat --console   keep a console window for log output
where py >nul 2>&1
if %errorlevel% equ 0 (
    py -3 build_exe.py %*
) else (
    python build_exe.py %*
)

exit /b %errorlevel%
