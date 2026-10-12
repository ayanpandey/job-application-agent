@echo off
cd /d "%~dp0"
echo Starting AI Job Agent Launcher...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_windows.ps1"
if errorlevel 1 (
    echo.
    echo [Fallback] Running via CMD batch...
    call "%~dp0start_windows.bat"
)
pause
