@echo off
title Build Executable for Windows
echo [*] Installing pyinstaller in virtual environment...
venv\Scripts\pip.exe install pyinstaller
echo [*] Building Standalone Executable...
venv\Scripts\python.exe build_exe.py
echo [OK] Build Complete! Check dist/JobAgent/JobAgent.exe
pause
