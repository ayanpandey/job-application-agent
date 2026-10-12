# AI Job Agent Launcher (PowerShell)
$Host.UI.RawUI.WindowTitle = "Autonomous AI Job Agent"
Write-Host "==============================================================================" -ForegroundColor Green
Write-Host "       Autonomous AI Job Search, Apply and Outreach Agent" -ForegroundColor Green
Write-Host "==============================================================================" -ForegroundColor Green
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

if (-not (Test-Path "$ScriptDir\\launcher.py")) {
    Write-Host "[ERROR] System files could not be found in: $ScriptDir" -ForegroundColor Red
    Write-Host "Please extract the zip archive before running!" -ForegroundColor Yellow
    Read-Host "Press Enter to exit..."
    exit 1
}

# Python Discovery
$PythonCmd = $null
$PyCommands = @("py", "python", "python3")
foreach ($cmd in $PyCommands) {
    try {
        $out = & $cmd -c "import sys; print(sys.version)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonCmd = $cmd
            break
        }
    } catch {}
}

if (-not $PythonCmd) {
    $SearchPaths = @(
        "$env:LOCALAPPDATA\\Programs\\Python\\Python314\\python.exe",
        "$env:LOCALAPPDATA\\Programs\\Python\\Python313\\python.exe",
        "$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe",
        "$env:LOCALAPPDATA\\Programs\\Python\\Python311\\python.exe",
        "$env:LOCALAPPDATA\\Programs\\Python\\Python310\\python.exe",
        "$env:ProgramFiles\\Python314\\python.exe",
        "$env:ProgramFiles\\Python313\\python.exe",
        "$env:ProgramFiles\\Python312\\python.exe",
        "$env:ProgramFiles\\Python311\\python.exe",
        "$env:ProgramFiles\\Python310\\python.exe",
        "C:\\Python314\\python.exe",
        "C:\\Python313\\python.exe",
        "C:\\Python312\\python.exe",
        "C:\\Python311\\python.exe",
        "C:\\Python310\\python.exe"
    )
    foreach ($p in $SearchPaths) {
        if (Test-Path $p) {
            $PythonCmd = $p
            break
        }
    }
}

if (-not $PythonCmd) {
    Write-Host "[ERROR] Python was not found on this computer!" -ForegroundColor Red
    Write-Host "Please install Python 3.10+ from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "*IMPORTANT*: Check the box 'Add python.exe to PATH' during installation." -ForegroundColor Yellow
    Read-Host "Press Enter to exit..."
    exit 1
}

Write-Host "[*] Located Python: $PythonCmd" -ForegroundColor Cyan

# Virtual Environment
$VenvPy = "$ScriptDir\\venv\\Scripts\\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "[*] Creating isolated virtual environment (venv)..." -ForegroundColor Cyan
    & $PythonCmd -m venv "$ScriptDir\\venv"
}

# Dependencies
Write-Host "[*] Verifying Python libraries..." -ForegroundColor Cyan
& $VenvPy -c "import rich, playwright, google.genai, yaml, pydantic, httpx, dotenv, requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Installing required dependencies (one-time setup)..." -ForegroundColor Cyan
    & $VenvPy -m pip install --upgrade pip setuptools wheel
    & "$ScriptDir\\venv\\Scripts\\pip.exe" install -r "$ScriptDir\\requirements.txt"
}

# Browser
Write-Host "[*] Checking Playwright Chromium browser..." -ForegroundColor Cyan
& $VenvPy -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Downloading Playwright browser binary..." -ForegroundColor Cyan
    & $VenvPy -m playwright install chromium
}

Write-Host "`n[*] Launching Interactive Menu...`n" -ForegroundColor Green
& $VenvPy "$ScriptDir\\launcher.py"

Read-Host "Press Enter to close..."
