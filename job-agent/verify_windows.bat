@echo off
cd /d "%~dp0"
title AI Job Agent Diagnostic
color 0A

py -3 "%~dp0bootstrap.py" verify
if %errorlevel% equ 0 goto :DONE

python "%~dp0bootstrap.py" verify
if %errorlevel% equ 0 goto :DONE

python3 "%~dp0bootstrap.py" verify
if %errorlevel% equ 0 goto :DONE

if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%ProgramFiles%\Python313\python.exe" (
    "%ProgramFiles%\Python313\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%ProgramFiles%\Python312\python.exe" (
    "%ProgramFiles%\Python312\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "%ProgramFiles%\Python311\python.exe" (
    "%ProgramFiles%\Python311\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "C:\Python313\python.exe" (
    "C:\Python313\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)
if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" "%~dp0bootstrap.py" verify
    if %errorlevel% equ 0 goto :DONE
)

echo.
echo [ERROR] Python was not found on this computer.
echo.
echo Please install Python 3.10+ from:
echo   https://www.python.org/downloads/
echo.
echo *IMPORTANT*: During installation, make sure to check:
echo   [X] "Add python.exe to PATH"
echo.

:DONE
echo.
echo Press any key to close this window...
pause >nul
