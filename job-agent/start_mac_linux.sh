#!/usr/bin/env bash
cd "$(dirname "$0")"

if [ ! -f "venv/bin/python" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
fi

venv/bin/python -c "import rich, playwright, google.genai, yaml, pydantic, httpx, dotenv, requests" >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "[*] Installing dependencies..."
    venv/bin/pip install --upgrade pip setuptools wheel
    venv/bin/pip install -r requirements.txt
fi

venv/bin/python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "[*] Downloading Playwright Chromium browser..."
    venv/bin/playwright install chromium
fi

./venv/bin/python launcher.py

