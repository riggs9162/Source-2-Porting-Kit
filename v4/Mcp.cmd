@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Agent.ps1" -Mode Mcp %*
exit /b %errorlevel%
