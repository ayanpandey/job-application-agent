# Windows PowerShell 1-Click Launcher for the AI Job Agent
Write-Host "==============================================================================" -ForegroundColor Cyan
Write-Host "       Autonomous AI Job Search, Apply and Outreach Agent" -ForegroundColor Green
Write-Host "==============================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Multi-path Python Detection
$pythonCmd = $null
$candidates = @("python", "py", "python3")
foreach ($c in $candidates) {
    $found = (Get-Command $c -ErrorAction SilentlyContinue)
    if ($found) {
        $pythonCmd = $found.Source
        break
    }
}

if (-not $pythonCmd) {
    $searchPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python310\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe"
    )
    foreach ($p in $searchPaths) {
        if (Test-Path $p) {
            $pythonCmd = $p
            break
        }
    }
}

if (-not $pythonCmd) {
    Write-Host "[ERROR] Python was not found on your system." -ForegroundColor Red
    Write-Host "Please install Python 3.10-3.13 from https://www.python.org/downloads/ and check 'Add python.exe to PATH'." -ForegroundColor Yellow
    Read-Host "Press Enter to exit..."
    exit
}

Write-Host "[*] Python interpreter located: $pythonCmd" -ForegroundColor Gray

# 2. Create virtualenv if not present
if (-not (Test-Path "venv\Scripts\python.exe")) {
    Write-Host "[*] Creating isolated Python virtual environment (venv)..." -ForegroundColor Yellow
    & $pythonCmd -m venv venv
}

# 3. Verify dependencies & install
$testDep = & .\venv\Scripts\python.exe -c "import rich, playwright, google.genai, yaml, pydantic, httpx, dotenv, requests" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Installing required libraries from requirements.txt (1-2 minutes)..." -ForegroundColor Yellow
    & .\venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
    & .\venv\Scripts\pip.exe install -r requirements.txt
}

# 4. Ensure Playwright Chromium browser binary is present
$testBrowser = & .\venv\Scripts\python.exe -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Downloading Playwright Chromium browser..." -ForegroundColor Yellow
    & .\venv\Scripts\playwright.exe install chromium
}

Write-Host "[OK] Environment verified and ready." -ForegroundColor Green
Write-Host ""

# 5. Launch application
& .\venv\Scripts\python.exe launcher.py
Read-Host "Press Enter to exit..."

