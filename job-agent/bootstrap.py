#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous AI Job Agent Bootstrap & Environment Engine
Handles automatic environment creation, dependency resolution, Playwright setup,
and execution across Windows, Mac, and Linux with zero manual steps.
"""

import sys
import os
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def log(msg, symbol="[*]"):
    print(f"{symbol} {msg}")

def error(msg):
    print(f"\n[ERROR] {msg}")

def check_python_version():
    if sys.version_info < (3, 10):
        error(f"Python 3.10 or newer is required. You are running Python {sys.version.split()[0]}.")
        error("Please install Python 3.10, 3.11, 3.12, or 3.13 from https://www.python.org/downloads/")
        input("\nPress Enter to exit...")
        sys.exit(1)

def get_venv_python():
    if sys.platform == "win32":
        return BASE_DIR / "venv" / "Scripts" / "python.exe"
    return BASE_DIR / "venv" / "bin" / "python"

def get_venv_pip():
    if sys.platform == "win32":
        return BASE_DIR / "venv" / "Scripts" / "pip.exe"
    return BASE_DIR / "venv" / "bin" / "pip"

def ensure_venv():
    venv_py = get_venv_python()
    if not venv_py.exists():
        log("Creating isolated virtual environment (venv)...")
        import venv
        venv.create(BASE_DIR / "venv", with_pip=True)
        if not venv_py.exists():
            # Fallback to subprocess creation
            subprocess.run([sys.executable, "-m", "venv", str(BASE_DIR / "venv")], check=True)
        log("Virtual environment created successfully.")
    return venv_py

def ensure_dependencies(venv_py):
    log("Verifying installed libraries...")
    check_code = "import rich, playwright, google.genai, yaml, pydantic, httpx, dotenv, requests"
    res = subprocess.run([str(venv_py), "-c", check_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        log("First-time setup: Installing required dependencies from requirements.txt (1-2 minutes)...")
        subprocess.run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], check=False)
        req_file = BASE_DIR / "requirements.txt"
        pip_res = subprocess.run([str(venv_py), "-m", "pip", "install", "-r", str(req_file)], check=False)
        if pip_res.returncode != 0:
            error("Failed to install Python packages. Please check your internet connection and retry.")
            input("\nPress Enter to exit...")
            sys.exit(1)
        log("Dependencies installed successfully.")

def ensure_browser(venv_py):
    log("Checking Playwright Chromium browser binary...")
    check_pw = "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()"
    res = subprocess.run([str(venv_py), "-c", check_pw], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        log("Downloading Playwright Chromium browser (one-time setup)...")
        subprocess.run([str(venv_py), "-m", "playwright", "install", "chromium"], check=False)

def main():
    check_python_version()
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    
    venv_py = ensure_venv()
    ensure_dependencies(venv_py)
    ensure_browser(venv_py)
    
    if action == "verify":
        log("Launching Full System Verification Suite...\n")
        verify_script = BASE_DIR / "verify_system.py"
        subprocess.run([str(venv_py), str(verify_script)])
    else:
        log("Launching Interactive Menu Dashboard...\n")
        launcher_script = BASE_DIR / "launcher.py"
        subprocess.run([str(venv_py), str(launcher_script)])

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
    except Exception as e:
        error(f"Unexpected exception during bootstrap: {e}")
        input("\nPress Enter to exit...")
        sys.exit(1)
