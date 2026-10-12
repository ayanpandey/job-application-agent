"""
Licence key custody and activation on the client side.

The key is the only credential the app holds, and it is worth exactly what the
operator's Vertex AI bill says it is -- so it goes in the OS keychain (Keychain
on macOS, Credential Manager on Windows), not in a config file the customer
might paste into a screenshot or sync to a shared drive.

When no keyring backend is available -- a headless Linux box, or a locked-down
Windows image -- it falls back to a 0600 file in the user data directory. That
is weaker, and `storage_backend()` says so, so the UI can tell the truth about
where the key ended up rather than implying a guarantee it did not get.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.paths import config_dir, ensure_dirs

SERVICE_NAME = "JobAgent"
ACCOUNT_NAME = "licence"

# Baked in at build time by build.py, which generates src/core/build_info.py.
# Absent from a source checkout, where the environment variable supplies it.
try:
    from src.core.build_info import API_URL as _BUILT_API_URL
    from src.core.build_info import VERSION as _BUILT_VERSION
except Exception:
    _BUILT_API_URL = ""
    _BUILT_VERSION = ""


def api_base_url() -> str:
    """Environment first, so a support engineer can point a shipped build at a
    staging proxy without a rebuild."""
    return (os.getenv("JOB_AGENT_API_URL") or _BUILT_API_URL).rstrip("/")


def app_version() -> str:
    """
    The version this copy actually is.

    JOB_AGENT_VERSION is a *build-time* variable: build.py reads it to stamp
    build_info.py and the Nuitka product version, and it is not present in the
    customer's environment afterwards. Reading it at runtime therefore always
    fell through to its default, so every shipped build reported "1.0.0" no
    matter which one it was -- which made the version field in telemetry and
    activation worthless for answering "what is this customer running?".

    Build info first for that reason; the environment stays ahead of nothing
    but the "dev" fallback, so a source checkout can still label itself.
    """
    return _BUILT_VERSION or os.getenv("JOB_AGENT_VERSION") or "dev"


# -- key storage -------------------------------------------------------------


def _keyring():
    try:
        import keyring

        # An unusable backend still imports. `fail` and `null` backends both
        # accept writes and return nothing on read, which would look like the
        # customer's key vanishing every restart.
        backend = keyring.get_keyring()
        name = type(backend).__module__.lower()
        if "fail" in name or "null" in name:
            return None
        return keyring
    except Exception:
        return None


def _fallback_file() -> Path:
    return config_dir() / ".licence"


def storage_backend() -> str:
    """'keychain' or 'file' -- surfaced in the UI so the user knows which."""
    return "keychain" if _keyring() is not None else "file"


def get_license_key() -> Optional[str]:
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE_NAME, ACCOUNT_NAME)
            if value:
                return value.strip()
        except Exception:
            pass

    path = _fallback_file()
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
    return None


def set_license_key(key: str) -> str:
    """Stores the key and returns the backend that accepted it."""
    key = (key or "").strip()
    if not key:
        raise ValueError("Licence key is empty.")

    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE_NAME, ACCOUNT_NAME, key)
            return "keychain"
        except Exception:
            pass

    ensure_dirs()
    path = _fallback_file()
    path.write_text(key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        # Windows ignores POSIX modes; ACLs already restrict the profile dir.
        pass
    return "file"


def clear_license_key() -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        except Exception:
            pass
    path = _fallback_file()
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass


# -- device identity ---------------------------------------------------------


def device_id() -> str:
    """
    A stable per-installation identifier, used for the seat limit.

    Generated once and kept in the user data directory rather than derived from
    a MAC address or serial number: hardware identifiers are personal data, they
    change under VPNs and docking stations, and this only needs to distinguish
    one install from another, not prove anything about the machine.

    Must be stable or not exist at all. It used to fall back to returning an
    unsaved id when the file could not be read or written, which looks harmless
    and is not: the server appends each unrecognised id to the licence's seat
    list, so a locked or unreadable `.device` produced a fresh id on *every*
    call and burned through `max_devices` in about three requests. The customer
    was then locked out by "This licence is already active on 2 device(s)" --
    an accusation of sharing, caused by a file permission, that they cannot
    fix themselves. Refusing loudly is the cheaper failure.
    """
    ensure_dirs()
    path = config_dir() / ".device"
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError as exc:
            raise ActivationError(
                f"Could not read this installation's device file ({path}). "
                f"Check that you can read and write that folder, then try again. ({exc})",
                "device_identity",
            ) from exc

    generated = uuid.uuid4().hex
    try:
        path.write_text(generated, encoding="utf-8")
        # Read back rather than trust the write: a full disk, a synced folder or
        # an antivirus hold can all accept the call and keep nothing.
        if path.read_text(encoding="utf-8").strip() != generated:
            raise OSError("the device file did not keep what was written to it")
    except OSError as exc:
        raise ActivationError(
            f"Could not save this installation's device file ({path}). "
            f"Check that the folder is writable and the disk is not full, then "
            f"try again. ({exc})",
            "device_identity",
        ) from exc

    return generated


# -- activation --------------------------------------------------------------


class ActivationError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


def activate(key: Optional[str] = None, timeout: float = 20.0) -> Dict[str, Any]:
    """
    Validates a key against the proxy and returns its licence summary.

    Called when the customer first enters a key and again on every app start,
    so a revoked or expired licence surfaces in the UI rather than as a wall of
    failed job evaluations halfway through a run.
    """
    import httpx

    key = (key or get_license_key() or "").strip()
    if not key:
        raise ActivationError("No licence key has been entered yet.", "missing_licence")

    base = api_base_url()
    if not base:
        # No endpoint means no way to check anything, and the only safe answer is
        # to refuse. This used to fall through to a "local direct mode" that
        # accepted any string of ten or more alphabet characters as a full
        # 365-day licence -- so a build made before JOB_AGENT_API_URL was set
        # was free to anyone who typed letters into the box. Activation is the
        # entire commercial boundary of this product; it fails closed.
        raise ActivationError(
            "This build has no licence server configured, so it cannot be "
            "activated. Please contact support for a correctly built copy.",
            "missing_api_url",
        )

    # Resolved before the request, not inside it: device_id() raises its own
    # ActivationError when the installation's id cannot be read or written, and
    # inside the try below that would be caught and reported as a network
    # failure -- sending the customer to check their internet over a file
    # permission.
    this_device = device_id()

    try:
        response = httpx.post(
            f"{base}/v1/activate",
            headers={"Authorization": f"Bearer {key}"},
            json={"device_id": this_device, "app_version": app_version()},
            timeout=timeout,
        )
    except Exception as exc:
        raise ActivationError(
            f"Could not reach the licence server at {base}. Check your internet connection. ({exc})",
            "network",
        )

    if response.status_code == 200:
        return response.json()

    try:
        detail = response.json().get("detail", {})
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        code = detail.get("code", "error") if isinstance(detail, dict) else "error"
    except Exception:
        message, code = response.text[:200], "error"
    raise ActivationError(message or f"Activation failed ({response.status_code}).", code)

