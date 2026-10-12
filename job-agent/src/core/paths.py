"""
Where things live once this is an installed application rather than a checkout.

A frozen app cannot write next to its executable. On macOS the .app bundle is
read-only (and signed, so writing into it breaks the signature); on Windows
Program Files is not writable by a standard user. Every path that gets written
to -- config, the database, browser profiles, generated resumes, logs -- has to
resolve into the per-user data directory instead.

Read-only assets that ship inside the bundle resolve separately, via
`bundled_dir()`, which knows about both PyInstaller and Nuitka layouts.

In a plain source checkout both roots collapse back to the repo, so running
`python launcher.py` during development behaves exactly as it always did.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "JobAgent"

# Repo root when running from source: src/core/paths.py -> up three.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def is_frozen() -> bool:
    """True inside a PyInstaller or Nuitka build."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals() or bool(
        getattr(sys, "_MEIPASS", None)
    )


def bundled_dir() -> Path:
    """
    Root for read-only files shipped with the app (default configs, UI assets).

    PyInstaller unpacks to `sys._MEIPASS`; Nuitka standalone places data next to
    the executable. Falls back to the repo root from source.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _REPO_ROOT


def user_data_dir() -> Path:
    """
    Per-user writable root, honouring each platform's convention.

    `JOB_AGENT_HOME` overrides it, which is what the test suite and a support
    engineer reproducing a customer issue both want.
    """
    override = os.getenv("JOB_AGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()

    if not is_frozen():
        # From a checkout, keep using the repo so existing data/ and config/
        # keep working and a developer's runs stay self-contained.
        return _REPO_ROOT

    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (base / APP_NAME).resolve()


def config_dir() -> Path:
    return user_data_dir() / "config"


def data_dir() -> Path:
    return user_data_dir() / "data"


def resumes_dir() -> Path:
    return user_data_dir() / "resumes"


def generated_resumes_dir() -> Path:
    """Where tailored resumes are written. The one directory the app creates on
    every run, so it is the first thing to fail when a default resolves into the
    read-only bundle."""
    return resumes_dir() / "generated"


def logs_dir() -> Path:
    return user_data_dir() / "logs"


def profile_path() -> Path:
    return config_dir() / "profile.yaml"


def app_config_path() -> Path:
    return config_dir() / "config.yaml"


def database_path() -> Path:
    return data_dir() / "jobs.db"


def browser_profile_dir(platform: str = "linkedin") -> Path:
    name = "browser_profile" if platform == "linkedin" else f"{platform}_browser_profile"
    return data_dir() / name


def master_resume(configured: Optional[str]) -> Optional[Path]:
    """
    The candidate's own resume, or None if there isn't one on disk.

    Onboarding copies the uploaded document under `resumes_dir()` and records an
    absolute path, so normally this just hands that back. It matters for the
    profiles that still carry the shipped relative default: resolving that
    against the working directory pointed inside the app bundle, where the file
    does not exist, and both upload paths silently applied with no resume
    attached rather than saying so.
    """
    if not configured:
        return None
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        cleaned = Path(*[p for p in candidate.parts if p not in (".", "..")])
        candidate = user_data_dir() / cleaned
    return candidate if candidate.exists() else None


def ensure_dirs() -> None:
    for path in (config_dir(), data_dir(), generated_resumes_dir(), logs_dir()):
        path.mkdir(parents=True, exist_ok=True)


def is_first_run() -> bool:
    """No profile yet means the onboarding wizard has never completed."""
    return not profile_path().exists()


def resolve_writable(configured: Optional[str], fallback: Path) -> Path:
    """
    Turn a config.yaml path into somewhere actually writable.

    Shipped configs carry repo-relative paths like "./data/jobs.db". Honouring
    those literally in a frozen app puts the database next to the executable,
    which fails outright on macOS and silently lands in a VirtualStore on
    Windows. Relative paths are therefore re-rooted under the user data
    directory; absolute paths are a deliberate operator choice and are kept.

    `None` means "caller expressed no preference" and takes the fallback, which
    is what lets every write path default to somewhere writable instead of to a
    repo-relative literal that only works from a checkout.
    """
    if not configured:
        return fallback
    candidate = Path(configured).expanduser()
    if candidate.is_absolute():
        return candidate
    # Always relative to `user_data_dir()`, never to the repo directly: that
    # function already collapses to the repo during development, and going
    # through it is what makes JOB_AGENT_HOME work everywhere rather than only
    # in a frozen build -- which is exactly what a support engineer reproducing
    # a customer's state needs it to do.
    cleaned = Path(*[part for part in candidate.parts if part not in (".", "..")])
    return (user_data_dir() / cleaned).resolve()
