@echo off
cd /d "%~dp0"
echo Starting AI Job Agent Diagnostic...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_windows.ps1"
if errorlevel 1 (
    echo.
    echo [Fallback] Running via CMD batch...
    call "%~dp0verify_windows.bat"
)
pause
