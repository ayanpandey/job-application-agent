"""
First-run provisioning of the Playwright browser.

Chromium is ~150MB and cannot sensibly go inside the installer, so it is
downloaded once on the customer's machine. Two things make that work in a
frozen build:

`PLAYWRIGHT_BROWSERS_PATH` is pinned to the user data directory. Playwright's
default is a cache folder next to the package, which inside a macOS .app is
read-only and inside Program Files needs admin rights -- so the download would
fail, or silently land in a Windows VirtualStore that a later launch cannot see.

The install runs through Playwright's own Python entry point rather than a
`python -m playwright` subprocess, because a frozen app has no interpreter to
call. The driver ships in the bundle (see build.py's --include-package-data).

This must be called before anything imports a browser, and `ensure_env()` is
therefore called at import time by the desktop shell.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

from src.core import paths


def browsers_dir() -> Path:
    return paths.user_data_dir() / "browsers"


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    elif sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    return Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "ms-playwright"


def ensure_env() -> None:
    """Point Playwright at a writable browser cache for frozen builds or custom directory."""
    if paths.is_frozen():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_dir()))
    elif browsers_dir().exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_dir()))


def is_installed() -> bool:
    """
    True when a Chromium build is already present.
    Checks custom browsers_dir() or standard Playwright cache.
    """
    root = browsers_dir()
    if root.exists() and any(child.name.startswith("chromium") for child in root.iterdir()):
        return True
    default_root = default_cache_dir()
    if default_root.exists() and any(child.name.startswith("chromium") for child in default_root.iterdir()):
        return True
    return False


def install(progress: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """
    Downloads Chromium. Returns (ok, message).

    Reports progress through the callback so the UI can show something during a
    download that routinely takes a couple of minutes.
    """
    if paths.is_frozen():
        ensure_env()
        browsers_dir().mkdir(parents=True, exist_ok=True)

    def say(message: str) -> None:
        if progress:
            progress(message)

    say("Downloading the browser Chromium (one-time, around 150MB)…")

    argv = sys.argv[:]
    try:
        from playwright.__main__ import main as playwright_main

        sys.argv = ["playwright", "install", "chromium"]
        try:
            playwright_main()
        except SystemExit as exc:
            # Playwright's CLI exits rather than returning; 0 and None are both
            # success, anything else is a real failure worth surfacing.
            if exc.code not in (0, None):
                return False, f"Browser download failed (exit code {exc.code})."
    except Exception as exc:
        return False, f"Browser download failed: {exc}"
    finally:
        sys.argv = argv

    if not is_installed():
        return False, "The browser did not install correctly. Check your internet connection and try again."

    say("Browser ready.")
    return True, "Browser installed."

