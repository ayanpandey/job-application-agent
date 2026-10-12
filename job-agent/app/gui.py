"""
Desktop shell: a native window around the onboarding wizard and run dashboard.

Two processes, deliberately.

The window is one process and an actual run is another, launched as a child of
this same executable with `--agent-run`. That split is what makes Stop work: the
run drives a Playwright browser through deeply nested async code with no
cancellation points, so there is no cooperative way to interrupt it from a
thread. Terminating a child process ends the run and takes the browser with it,
immediately and from any state. It also means a crash inside a run leaves the
window standing, with the traceback in the log pane instead of on a dead app.

The child speaks plain log lines on stdout (see JOB_AGENT_PLAIN_LOGS); the
parent reads them on a pump thread into a bounded deque that the UI polls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# A frozen build starts here rather than at the repo root, so make `src` and
# `app` importable regardless of how the executable was invoked.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# -- crash visibility ---------------------------------------------------------
#
# The shipped binary is linked for the windowing subsystem: on Windows via
# --windows-console-mode, on macOS as a .app bundle. Neither has a console, so
# an uncaught exception writes a traceback to a stderr that goes nowhere. The
# process starts, dies in milliseconds, and the customer sees *nothing happen* --
# with no message, no log and nothing to report back.
#
# So the very first thing this program does, before importing anything of its
# own that could fail, is install somewhere for a crash to land. Everything
# below is stdlib-only and wrapped, because a crash handler that can itself
# crash just restores the silence it was written to remove.


# Invocations with nobody sitting at the screen. A modal dialog in any of these
# does not report a problem, it wedges the process: `MessageBoxW` blocks until
# someone clicks OK, and in CI or a background run there is no one. That is
# exactly how a crashed self-test survived 300 seconds and was killed by its
# harness rather than exiting with a diagnosis.
_HEADLESS_FLAGS = ("--agent-run", "--self-test", "--self-test-child", "--install-browser")


def _is_headless() -> bool:
    return any(flag in sys.argv for flag in _HEADLESS_FLAGS)


def _force_utf8_stdio() -> None:
    """
    Make this process's own stdout and stderr UTF-8, whatever the locale says.

    The child of a run gets this through PYTHONIOENCODING, and the parent
    decodes the pipe as UTF-8 -- but neither covers *this* process's own output.
    On a Windows console or a redirected file that defaults to cp1252, so
    printing anything non-ASCII raises UnicodeEncodeError. It bit twice: the
    self-test echoing its child's output, and it would bite `_report_crash`
    writing a traceback that happens to name a non-ASCII path -- a crash
    reporter that dies while reporting.

    errors="replace" because a mangled character is always better than a lost
    message. Wrapped and None-guarded: a GUI-subsystem process may have no
    stdio at all, and this must never be the thing that stops startup.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()


def _crash_log_dir() -> "Path":
    """First writable candidate. Deliberately does not import src.core.paths --
    that module resolving wrongly is one of the things this has to survive."""
    import tempfile

    candidates = []
    override = os.getenv("JOB_AGENT_HOME")
    if override:
        candidates.append(Path(override).expanduser() / "logs")
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "JobAgent" / "logs")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "JobAgent" / "logs")
    else:
        candidates.append(Path.home() / ".local" / "share" / "JobAgent" / "logs")
    candidates.append(Path(tempfile.gettempdir()))

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return path
        except Exception:
            continue
    return Path(tempfile.gettempdir())


def _show_error_dialog(title: str, body: str) -> None:
    """A native dialog, because the alternative the customer sees is silence."""
    try:
        if sys.platform == "win32":
            import ctypes

            # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(None, body, title, 0x10 | 0x10000)
        elif sys.platform == "darwin":
            # ensure_ascii=False matters: AppleScript understands \" \\ \n \t
            # but has no \uXXXX escape, so an ASCII-escaped string would be a
            # syntax error for any traceback naming a non-ASCII path -- and a
            # dialog that fails to open is the silence this exists to end.
            script = (
                'display dialog {} with title {} buttons {{"OK"}} '
                "default button 1 with icon stop"
            ).format(
                json.dumps(body, ensure_ascii=False),
                json.dumps(title, ensure_ascii=False),
            )
            subprocess.run(["osascript", "-e", script], check=False, timeout=60)
    except Exception:
        pass


def _report_crash(exc_type, exc_value, exc_tb, thread_name: str = "") -> None:
    import traceback

    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    where = f" in thread {thread_name}" if thread_name else ""
    stamp = datetime.now().isoformat(timespec="seconds")
    log_path = None

    try:
        log_path = _crash_log_dir() / "startup-crash.log"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {stamp}{where} =====\n")
            fh.write(f"executable: {sys.executable}\n")
            fh.write(f"argv: {sys.argv}\n")
            fh.write(f"platform: {sys.platform}\n\n")
            fh.write(text)
    except Exception:
        pass

    # Still try stderr: when the exe is run from a terminal with output
    # redirected, this is where a support engineer actually reads it.
    try:
        stream = sys.stderr or sys.__stderr__
        if stream is not None:
            stream.write(text)
            stream.flush()
    except Exception:
        pass

    # No dialog when nobody is watching. The `--agent-run` child's stderr is
    # piped into the window's log pane so its errors are already visible, and
    # the other headless modes are automated -- in all of them a modal box
    # blocks forever instead of failing, turning a crash into a hang.
    if _is_headless():
        return

    summary = f"{exc_type.__name__}: {exc_value}"

    # A customer can act on "Windows blocked a file"; nobody can act on
    # "Failed to resolve Python.Runtime.Loader.Initialize". The app already
    # tries to clear this itself (see _unblock_bundled_assemblies), so reaching
    # here means it could not -- almost always an install in a directory the
    # user cannot write to.
    hint = ""
    if "Python.Runtime" in text or "clr_loader" in text:
        hint = (
            "\n\nWindows appears to have blocked one of the app's own files "
            "because it came from a download.\n\n"
            "To fix it, right-click the folder you unzipped, open PowerShell "
            "there, and run:\n"
            "    Get-ChildItem -Recurse | Unblock-File\n\n"
            "Then start Job Agent again. If it still fails, move the folder "
            "somewhere you can write to, such as your Desktop."
        )

    body = (
        "Job Agent could not start.\n\n"
        f"{summary}"
        + hint
        + (f"\n\nThe full report was saved to:\n{log_path}" if log_path else "")
    )
    _show_error_dialog("Job Agent", body)


def _install_crash_handler() -> None:
    def hook(exc_type, exc_value, exc_tb):
        _report_crash(exc_type, exc_value, exc_tb)

    def thread_hook(args):
        # A failure inside pywebview's GUI thread ends the window just as dead
        # as one on the main thread, and never reaches sys.excepthook.
        if args.exc_type is SystemExit:
            return
        _report_crash(
            args.exc_type, args.exc_value, args.exc_traceback,
            getattr(args.thread, "name", ""),
        )

    sys.excepthook = hook
    threading.excepthook = thread_hook


_install_crash_handler()

from src.core import licensing, paths  # noqa: E402
from src.core.logger import logger  # noqa: E402
from app import onboarding, runtime  # noqa: E402

# Must happen before anything touches Playwright: it pins the browser cache to a
# writable location, and the default would be inside the read-only app bundle.
runtime.ensure_env()

# Rich markup leaks into log messages ("[green]✓ Connected[/green]"). The pane
# renders text, so strip the tags rather than teaching the UI to parse them.
_RICH_TAG = re.compile(r"\[/?[a-zA-Z0-9 _#]+\]")

MAX_LOG_LINES = 800

# Unattended runs. One agent session is 25-70 minutes and ends on its own, so an
# hourly attempt lands roughly one session per waking hour and leaves real gaps
# between them. The floor exists because a shorter interval would relaunch the
# browser more often than a session lasts.
AUTO_RUN_DEFAULT_MINUTES = 60
AUTO_RUN_MIN_MINUTES = 20


def ui_dir() -> Path:
    return paths.bundled_dir() / "app" / "ui"


class Api:
    """Everything the page can call. Method names are the JS API surface."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lines: deque = deque(maxlen=MAX_LOG_LINES)
        self._lock = threading.Lock()
        self._window = None
        self._auto: Optional[Dict[str, Any]] = None
        self._auto_stop = threading.Event()
        self._auto_thread: Optional[threading.Thread] = None

    def attach(self, window) -> None:
        self._window = window

    # -- state ---------------------------------------------------------------

    def get_state(self) -> Dict[str, Any]:
        licence: Dict[str, Any] = {"valid": False}
        stored = licensing.get_license_key()
        if stored:
            try:
                licence = licensing.activate(stored)
                licence["valid"] = True
            except licensing.ActivationError as exc:
                licence = {"valid": False, "error": exc.message, "code": exc.code}
        return {
            "first_run": paths.is_first_run(),
            "has_key": bool(stored),
            "licence": licence,
            "storage": licensing.storage_backend(),
            "running": self.is_running(),
            "browser_ready": runtime.is_installed(),
            "data_dir": str(paths.user_data_dir()),
        }

    def prepare_browser(self) -> Dict[str, Any]:
        """
        Downloads Chromium if it is not already present.

        Runs on the calling thread rather than in the background: the UI shows a
        blocking screen for this anyway, and there is nothing useful to do until
        it finishes. pywebview dispatches JS API calls off the UI thread, so the
        window stays responsive.
        """
        if runtime.is_installed():
            return {"ok": True, "already": True}
        ok, message = runtime.install(progress=self._append)
        return {"ok": ok, "message": message}

    def activate_licence(self, key: str) -> Dict[str, Any]:
        try:
            info = licensing.activate(key)
        except licensing.ActivationError as exc:
            return {"ok": False, "error": exc.message, "code": exc.code}
        backend = licensing.set_license_key(key)
        return {"ok": True, "licence": info, "storage": backend}

    def sign_out(self) -> Dict[str, Any]:
        licensing.clear_license_key()
        return {"ok": True}

    # -- profile -------------------------------------------------------------

    def get_profile(self) -> Dict[str, Any]:
        return onboarding.load_existing()

    def save_profile(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            written = onboarding.save(data or {})
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **written}

    def pick_resume(self) -> Dict[str, Any]:
        """Native file picker, so the customer never types a path."""
        import webview

        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("PDF resume (*.pdf)", "All files (*.*)"),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not result:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": result[0]}

    def set_base_resume(self, path: str) -> Dict[str, Any]:
        """Adopt a new master resume from the Settings screen."""
        try:
            written = onboarding.set_base_resume(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **written}

    def reimport_cv(self) -> Dict[str, Any]:
        """
        Rebuild the profile's career sections from the CV already on file.

        Exists because a profile can predate CV parsing entirely: builds before
        this shipped copied the CV to disk without ever reading it, and the
        wizard has no field for job bullet points, so those profiles render a
        resume with an empty experience section. This is how an existing
        customer repairs one without re-uploading anything.
        """
        try:
            stored = onboarding._read_profile().get("resume_path")
            if not stored:
                return {"ok": False, "error": "No base resume on file. Upload your CV first."}
            written = onboarding.set_base_resume(stored)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if written.get("import_error"):
            return {"ok": False, "error": written["import_error"]}
        return {"ok": True, **written}

    # -- pacing and limits ---------------------------------------------------
    #
    # These were honoured all along and exposed nowhere. A customer whose run
    # stopped with "outside the configured active window" had no way to find
    # the window, let alone change it, without knowing that a config.yaml
    # existed and where the app kept it.

    PACING_FIELDS = (
        ("rhythm", "enabled", bool),
        ("rhythm", "active_hours_start", int),
        ("rhythm", "active_hours_end", int),
        ("rhythm", "session_minutes_min", float),
        ("rhythm", "session_minutes_max", float),
        ("rhythm", "rest_day_probability", float),
        ("rhythm", "daily_quota_jitter", float),
        ("limits", "max_applications_per_day", int),
        ("limits", "max_outreach_messages_per_day", int),
        # min_match_score lives under `search`, not `matching` -- `matching`
        # holds the pre-filter knobs. Naming the wrong section here reads as an
        # empty box in the UI and writes to a section nothing consults.
        ("search", "min_match_score", int),
        ("search", "external_apply", str),
        ("matching", "prefilter_reject_below", int),
    )

    def get_pacing(self) -> Dict[str, Any]:
        """Current pacing settings, straight from the user's own config.yaml."""
        import yaml

        try:
            path = paths.app_config_path()
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
            out: Dict[str, Any] = {}
            for section, key, _cast in self.PACING_FIELDS:
                value = (data.get(section) or {}).get(key)
                if value is not None:
                    out[f"{section}.{key}"] = value
            return {"ok": True, "values": out, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def save_pacing(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Write pacing settings back, updating config.yaml in place.

        In place because the wizard, the expanded keywords and everything else
        with no field on this screen has to survive the save.
        """
        import yaml

        try:
            path = paths.app_config_path()
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}

            casts = {f"{s}.{k}": c for s, k, c in self.PACING_FIELDS}
            for dotted, raw in (values or {}).items():
                cast = casts.get(dotted)
                if cast is None:
                    continue  # not a field this screen owns
                section, key = dotted.split(".", 1)
                try:
                    data.setdefault(section, {})[key] = cast(raw)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{dotted} is not a valid {cast.__name__}."}

            rhythm = data.get("rhythm") or {}
            start, end = rhythm.get("active_hours_start", 0), rhythm.get("active_hours_end", 24)
            # `start <= hour < end`, so start == end matches no hour at all and
            # would silently stop every run. Always-on is 0 to 24.
            if start == end:
                return {"ok": False, "error":
                        "Start and end hour cannot be equal - that blocks every hour. "
                        "For always-on use 0 and 24."}
            if not (0 <= start <= 24 and 0 <= end <= 24):
                return {"ok": False, "error": "Hours must be between 0 and 24."}

            paths.ensure_dirs()
            path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
                encoding="utf-8",
            )
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_folder(self, which: str = "root") -> Dict[str, Any]:
        target = {
            "root": paths.user_data_dir(),
            "resumes": paths.resumes_dir() / "generated",
            "logs": paths.logs_dir(),
        }.get(which, paths.user_data_dir())
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target)}

    # -- resumes & applications -----------------------------------------------

    def _candidate_slug(self) -> str:
        from src.core.cloud_storage import candidate_slug

        profile = onboarding.load_existing()
        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        return candidate_slug(name)

    def get_resumes(self) -> List[Dict[str, Any]]:
        """
        Every master and tailored resume, newest first, with its audit metadata.

        Ordering matters downstream: get_applications() falls back to matching a
        job by company, and takes the first hit as the most recent one.
        """
        items: List[Dict[str, Any]] = []
        gen_dir = paths.resumes_dir() / "generated"
        if gen_dir.exists():
            for meta_file in sorted(gen_dir.glob("*.metadata.json"), key=os.path.getmtime, reverse=True):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    pdf_path = Path(data.get("pdf_path", ""))
                    data["pdf_exists"] = pdf_path.exists()
                    data["is_master"] = False
                    # `gcs_pdf_uri` is left absent unless an upload actually
                    # returned one. It used to be derived from a deterministic
                    # bucket path, which meant the UI showed a cloud location
                    # for every resume including the ones whose upload had
                    # failed -- and on a customer install, that was all of them.
                    items.append(data)
                except Exception:
                    continue

        # Master resumes
        #
        # The label under a master CV is the customer's own target, not a fixed
        # string. It used to read "AI / ML Engineer" for everyone, which is the
        # author's role -- an auditor who installed this saw someone else's job
        # title printed over their own CV.
        resumes_root = paths.resumes_dir()
        if resumes_root.exists():
            try:
                _profile = onboarding.load_existing()
                _titles = _profile.get("job_titles") or []
                master_role = (
                    (_titles[0] if _titles else "")
                    or _profile.get("current_role")
                    or "Your master CV"
                )
            except Exception:
                master_role = "Your master CV"
            for pdf in sorted(resumes_root.glob("*.pdf"), key=os.path.getmtime, reverse=True):
                items.append({
                    "job_title": "Master Base Resume",
                    "company": "Canonical Profile",
                    "generated_at": datetime.fromtimestamp(pdf.stat().st_mtime).isoformat(),
                    "fidelity_score": 100.0,
                    "deviation_percent": 0.0,
                    "is_within_10_percent_limit": True,
                    "auditor_verdict": "APPROVED",
                    "auditor_findings": "Original verified canonical master resume.",
                    "target_role": master_role,
                    "pdf_file": pdf.name,
                    "pdf_path": str(pdf),
                    "pdf_exists": True,
                    "is_master": True,
                })
        return items

    def get_applications(self) -> List[Dict[str, Any]]:
        """
        Every job the agent has processed, each paired with the resume it used.

        Pairing is by job_id only. Falling back to the company name would attach
        a resume to listings it was never generated for -- a job the agent
        rejected without applying would claim a tailored resume it never had,
        which is the one thing this view must not get wrong.
        """
        try:
            from src.core.config import load_app_config
            from src.core.database import Database

            db = Database(load_app_config().general.database_path)
            jobs = db.get_recent_jobs()

            # get_resumes() is newest-first, so setdefault keeps the newest entry
            # per job -- a regenerated resume supersedes the earlier attempt.
            resumes_by_job_id: Dict[str, Dict[str, Any]] = {}
            for r in self.get_resumes():
                if not r.get("is_master") and r.get("job_id"):
                    resumes_by_job_id.setdefault(str(r["job_id"]), r)

            for job in jobs:
                job["resume"] = resumes_by_job_id.get(str(job.get("job_id", "")))
            return jobs
        except Exception as exc:
            logger.warning(f"Failed to fetch applications: {exc}")
            return []

    def open_resume(self, file_path: str) -> Dict[str, Any]:
        """Opens a resume PDF or text file with system viewer."""
        target = Path(file_path).resolve()
        if not target.exists():
            return {"ok": False, "error": f"File does not exist: {file_path}"}
        try:
            if sys.platform == "win32":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_url(self, url: str) -> Dict[str, Any]:
        """Opens an external URL in default web browser."""
        import webbrowser
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def sync_resumes_to_gcs(self) -> Dict[str, Any]:
        """
        Uploads every master and generated resume through the licence proxy.

        Previously this built a GCS client on the customer's machine, which
        needs a service-account key that is never present there, so the button
        reported success on a sync that had uploaded nothing.
        """
        from src.core.cloud_storage import backup_resume

        profile = onboarding.load_existing()
        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()

        uploaded: List[str] = []
        failed: List[str] = []
        candidates = list(paths.resumes_dir().glob("*.pdf"))
        gen_dir = paths.resumes_dir() / "generated"
        if gen_dir.exists():
            for pattern in ("*.pdf", "*.tex", "*.metadata.json"):
                candidates.extend(gen_dir.glob(pattern))

        for path in candidates:
            try:
                if backup_resume(path, name):
                    uploaded.append(path.name)
                else:
                    failed.append(path.name)
            except Exception:
                failed.append(path.name)

        return {
            "ok": not failed,
            "result": {"uploaded": uploaded, "failed": failed, "total": len(candidates)},
            "error": (
                f"{len(failed)} file(s) could not be uploaded. Check your licence and connection."
                if failed else ""
            ),
        }

    def clear_logs(self) -> Dict[str, Any]:
        """Clears the live log output buffer."""
        with self._lock:
            self._lines.clear()
        return {"ok": True}

    # -- runs ----------------------------------------------------------------

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start_run(self, platform: str = "linkedin", live: bool = False) -> Dict[str, Any]:
        if self.is_running():
            return {"ok": False, "error": "A run is already in progress."}
        if paths.is_first_run():
            return {"ok": False, "error": "Finish setting up your profile first."}
        if not licensing.get_license_key():
            return {"ok": False, "error": "Enter your licence key first."}

        with self._lock:
            self._lines.clear()

        argv = _child_argv(["--agent-run", platform, "live" if live else "dry"])
        env = _child_env()

        try:
            self._proc = subprocess.Popen(argv, **_child_stdio(), env=env, cwd=str(_ROOT))
        except Exception as exc:
            return {"ok": False, "error": f"Could not start the run: {exc}"}

        threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
        self._append(f"Started {platform} run ({'LIVE' if live else 'dry run'}).")
        return {"ok": True}

    def stop_run(self) -> Dict[str, Any]:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return {"ok": False, "error": "Nothing is running."}
        self._append("Stopping…")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # The browser can wedge on a modal dialog and ignore SIGTERM. Nothing
            # is written mid-record here -- the database commits per job -- so
            # killing outright loses at most the listing in flight.
            proc.kill()
        self._append("Run stopped.")
        return {"ok": True}

    # -- unattended operation -------------------------------------------------

    def get_schedule(self) -> Dict[str, Any]:
        auto = self._auto
        return {
            "enabled": auto is not None,
            "platform": (auto or {}).get("platform", "linkedin"),
            "live": bool((auto or {}).get("live", False)),
            "every_minutes": (auto or {}).get("every_minutes", AUTO_RUN_DEFAULT_MINUTES),
            "next_run": (auto or {}).get("next_run"),
            "runs_started": (auto or {}).get("runs_started", 0),
        }

    def set_schedule(
        self,
        enabled: bool,
        platform: str = "linkedin",
        live: bool = False,
        every_minutes: int = AUTO_RUN_DEFAULT_MINUTES,
    ) -> Dict[str, Any]:
        """
        Turns unattended running on or off.

        The scheduler only decides *when to try*. Whether a run is allowed to
        proceed is still the agent's own `SessionRhythm` preflight in the child
        process -- active hours, rest days, the jittered daily quota. Duplicating
        any of that here would give two disagreeing opinions about whether today
        is a rest day, and the one that protects the account is the agent's.
        """
        self.stop_schedule()

        if not enabled:
            self._append("Automatic runs turned off.")
            return {"ok": True, **self.get_schedule()}

        if paths.is_first_run():
            return {"ok": False, "error": "Finish setting up your profile first."}
        if not licensing.get_license_key():
            return {"ok": False, "error": "Enter your licence key first."}

        # A floor, not a preference: below this the agent would relaunch the
        # browser more often than a session lasts, which is both pointless and
        # exactly the cadence a platform notices.
        minutes = max(AUTO_RUN_MIN_MINUTES, int(every_minutes or AUTO_RUN_DEFAULT_MINUTES))

        auto = {
            "platform": platform,
            "live": bool(live),
            "every_minutes": minutes,
            "next_run": time.time(),
            "runs_started": 0,
        }
        stop = threading.Event()
        self._auto = auto
        self._auto_stop = stop
        # The event and the schedule are handed to the thread rather than read
        # off self: stop_schedule joins with a timeout, so a previous loop can
        # still be inside its sleep when this one is armed. Reading self would
        # let that straggler pick up the *new* event -- which is not set -- and
        # keep launching runs forever alongside its replacement.
        self._auto_thread = threading.Thread(target=self._auto_loop, args=(auto, stop), daemon=True)
        self._auto_thread.start()

        self._append(
            f"Automatic runs on: {platform} "
            f"({'LIVE' if live else 'dry run'}) every {minutes} minutes, "
            f"within the active hours in your settings."
        )
        return {"ok": True, **self.get_schedule()}

    def stop_schedule(self) -> Dict[str, Any]:
        self._auto_stop.set()
        thread = self._auto_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._auto = None
        self._auto_thread = None
        return {"ok": True, **self.get_schedule()}

    def _auto_loop(self, auto: Dict[str, Any], stop: threading.Event) -> None:
        """
        Starts a run whenever one is due and none is in flight.

        Deliberately dumb: it never queues, never retries faster on failure, and
        never overlaps two runs. A run that the rhythm declines exits in a second
        or two having printed why, and the next attempt is a full interval later
        -- so a rest day costs a handful of no-op launches, not a busy loop.
        """
        while not stop.wait(5):
            # Someone else armed a newer schedule while this loop was sleeping.
            if self._auto is not auto:
                return
            if self.is_running():
                continue
            if time.time() < auto.get("next_run", 0):
                continue

            auto["next_run"] = time.time() + auto["every_minutes"] * 60
            auto["runs_started"] = auto.get("runs_started", 0) + 1
            result = self.start_run(auto["platform"], auto["live"])
            if not result.get("ok"):
                self._append(f"Scheduled run could not start: {result.get('error')}")

    def poll(self) -> Dict[str, Any]:
        with self._lock:
            lines = list(self._lines)
        return {
            "running": self.is_running(),
            "lines": lines,
            "stats": self._stats(),
            "schedule": self.get_schedule(),
        }

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            self._append(raw.rstrip("\n"))
        code = proc.wait()
        self._append(f"Run finished (exit code {code}).")

    def _append(self, line: str) -> None:
        cleaned = _RICH_TAG.sub("", line).rstrip()
        if cleaned:
            with self._lock:
                self._lines.append(cleaned)

    def _stats(self) -> Dict[str, Any]:
        """Counters from the run database. Empty before the first run."""
        try:
            from src.core.config import load_app_config
            from src.core.database import Database

            db = Database(load_app_config().general.database_path)
            return db.get_statistics()
        except Exception:
            return {}


def _child_stdio() -> Dict[str, Any]:
    """
    How to wire a child process's stdio, and why stdin is not left to default.

    On Windows, redirecting *any* standard stream makes subprocess set
    STARTF_USESTDHANDLES, which obliges it to supply all three. For a stream
    left as None it uses the parent's, via GetStdHandle -- and this app has no
    console, because it is linked for the windowing subsystem so a double-click
    does not flash one up.

    CPython guards only against GetStdHandle returning NULL; it then invents a
    dummy pipe. A GUI-subsystem process instead hands back a non-NULL handle
    that is not valid, which sails past that check and dies one line later in
    _make_inheritable's DuplicateHandle:

        [WinError 6] The handle is invalid

    That is what "Could not start the run" was. It could only ever appear on a
    customer's machine -- from a terminal the app has a real stdin and the same
    call succeeds -- and it stayed hidden until 1.0.1, because before that the
    window never opened far enough to press Start.

    DEVNULL routes stdin to a genuine handle on NUL, so the None branch never
    runs. The run reads nothing from stdin; the child talks over its stdout
    pipe, which the log pane pumps.
    """
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
        # Both halves of the pipe must agree, and the default is not UTF-8.
        # `text=True` alone decodes using the *locale* encoding, which on an
        # Indian or Western European Windows install is cp1252 -- so the moment
        # the run logged a tick, an arrow or a box-drawing rule, the parent
        # raised UnicodeDecodeError while reading its own child. `errors` is
        # "replace" rather than "strict" deliberately: a log line is not worth
        # killing a run over, and a lost glyph is a better outcome than a
        # traceback in place of the output.
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        # The child is the same GUI-subsystem binary, so no console appears
        # regardless; this states the intent rather than relying on that.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _child_env() -> Dict[str, str]:
    """Environment for a spawned run."""
    env = dict(os.environ)
    env["JOB_AGENT_PLAIN_LOGS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Set in the environment, not just by reconfiguring the streams in the
    # child, because the interpreter fixes its stdio encoding before any of our
    # code runs -- anything printed during imports would already have been
    # encoded with the locale codec by the time we could intervene. Pairs with
    # the encoding on the parent's side of the pipe in _child_stdio().
    env["PYTHONIOENCODING"] = "utf-8:replace"
    return env


def _self_test() -> int:
    """
    Spawns a child exactly as a run does, and reports whether it worked.

    Exists because the WinError 6 above is invisible to every other kind of
    test: it needs a real frozen binary, on Windows, with no console --
    conditions no unit test on a developer's machine reproduces. This gives CI
    something it can actually assert on. See .github/workflows/diagnose-windows.yml.
    """
    print(f"self-test: frozen={paths.is_frozen()}")
    print(f"self-test: sys.executable={sys.executable}  (exists={Path(sys.executable).is_file()})")
    print(f"self-test: bundled_dir={paths.bundled_dir()}")
    argv = _child_argv(["--self-test-child"])
    print(f"self-test: spawning {argv}")
    try:
        proc = subprocess.Popen(argv, **_child_stdio(), env=_child_env(), cwd=str(_ROOT))
    except Exception as exc:
        print(f"self-test: FAILED to spawn: {type(exc).__name__}: {exc}")
        return 1

    try:
        out, _ = proc.communicate(timeout=180)
    except Exception as exc:
        proc.kill()
        print(f"self-test: FAILED reading the child: {type(exc).__name__}: {exc}")
        return 1

    code = proc.returncode
    got = (out or "").strip()
    print("self-test: ---- child output ----")
    for line in got.splitlines():
        print(f"  | {line}")
    print("self-test: ---- end ----")

    if code != 0:
        print(f"self-test: FAILED -- child exited {code}.")
        return 1
    # Every marker the child is expected to emit. Each one stands for a failure
    # that has actually shipped, so a missing marker is a real regression and
    # not a cosmetic one.
    required = {
        "CHILD-OK": "the child ran at all",
        "UNICODE-OK": "non-ASCII survived the pipe in both directions",
        "DRIVER-OK": "the bundled Playwright driver starts",
    }
    # Only demanded when a browser is present. On a machine that has one, they
    # are the difference between "the app launches" and "the app can do its job".
    if "BROWSER-SKIPPED" not in got:
        required["BROWSER-OK"] = "Chromium launches and renders"
        required["PDF-OK"] = "a resume PDF can be produced"
    missing = [why for marker, why in required.items() if marker not in got]
    if missing:
        for why in missing:
            print(f"self-test: FAILED -- {why}")
        return 1
    print("self-test: PASSED -- spawn, unicode and browser driver all work.")
    return 0


def _self_test_child() -> int:
    """
    The other half of the self-test. Runs as a spawned child, exactly as a run
    does, and exercises what has actually broken in shipped builds.
    """
    print("CHILD-OK", flush=True)

    # Non-ASCII through the shared Rich console -- the exact path that raised
    # UnicodeEncodeError on Windows, because Rich drove the Win32 console API on
    # a pipe and encoded to cp1252. The characters are the ones the run really
    # prints: the box rule from launcher.py and the tick used all over src/.
    try:
        from src.core.logger import console, logger

        console.print("[bold magenta]══ self-test ══[/bold magenta] ✓ ✗ → ± ₹ é")
        logger.info("[green]✓ logger path[/green] ══ → ₹")
        print("UNICODE-OK", flush=True)
    except Exception as exc:
        print(f"UNICODE-FAILED: {type(exc).__name__}: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        return 1

    # The bundled Playwright driver. Starting it launches node.exe and completes
    # a handshake, which is what proves the driver survived packaging -- and it
    # needs no downloaded browser, so it is cheap enough to gate every build on.
    try:
        import asyncio

        from playwright.async_api import async_playwright

        async def _probe() -> str:
            pw = await async_playwright().start()
            try:
                return pw.chromium.name
            finally:
                await pw.stop()

        name = asyncio.run(_probe())
        print(f"DRIVER-OK ({name})", flush=True)
    except Exception as exc:
        print(f"DRIVER-FAILED: {type(exc).__name__}: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        return 1

    # The two below need a downloaded Chromium, which the app fetches on first
    # run. Skipped rather than failed when it is absent, so the gate still works
    # on a bare machine; CI installs one so a release is checked against both.
    if not runtime.is_installed():
        print("BROWSER-SKIPPED (no Chromium downloaded)", flush=True)
        return 0

    try:
        import asyncio

        from playwright.async_api import async_playwright

        async def _browser() -> str:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    # Non-ASCII through the browser too: a resume carries names
                    # and currency symbols, and the PDF path below renders them.
                    await page.set_content("<h1>self-test ✓ ₹ é</h1>")
                    return await page.title()
                finally:
                    await browser.close()

        asyncio.run(_browser())
        print("BROWSER-OK", flush=True)
    except Exception as exc:
        print(f"BROWSER-FAILED: {type(exc).__name__}: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        return 1

    # The resume PDF path. A customer machine has no LaTeX, so
    # _compile_with_pdflatex always fails there and every real resume is
    # produced by this Chromium fallback -- making it the path that matters, and
    # the one never exercised before shipping.
    try:
        import asyncio
        import tempfile

        from playwright.async_api import async_playwright

        async def _pdf(target: Path) -> None:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content("<h1>Résumé ✓ ₹1,00,000</h1>")
                    await page.pdf(path=str(target), format="A4", print_background=True)
                finally:
                    await browser.close()

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "self-test.pdf"
            asyncio.run(_pdf(out))
            size = out.stat().st_size if out.exists() else 0
            if size < 1000:
                print(f"PDF-FAILED: wrote {size} bytes", flush=True)
                return 1
            print(f"PDF-OK ({size} bytes)", flush=True)
    except Exception as exc:
        print(f"PDF-FAILED: {type(exc).__name__}: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        return 1

    return 0


# Must match BINARY_NAME in build.py, which passes it as --output-filename.
# Nothing enforces that; the fallbacks below are what make a mismatch survivable
# rather than fatal.
_BINARY_NAME = "JobAgent"


def _frozen_executable() -> str:
    """
    This application's own binary. Deliberately not `sys.executable`.

    A Nuitka standalone build sets `sys.executable` to a `python` / `python.exe`
    *beside* the real binary rather than to the binary itself. On Windows no such
    file is produced at all, so spawning it raises

        FileNotFoundError: [WinError 2] The system cannot find the file specified

    and on macOS it resolves to the interpreter Nuitka bundled into
    Contents/MacOS, which starts and then fails on an --agent-run it has never
    heard of. Either way the run cannot start, on every frozen platform.

    This hid behind the WinError 6 in `_child_stdio`: Popen builds its handles
    before it calls CreateProcess, so the handle failure was raised first and
    the wrong path was never reached. Fixing that one exposed this one.

    `paths.bundled_dir()` is unaffected because it takes sys.executable's
    *directory*, which is right even when the filename is wrong -- which is why
    the UI, config and browser driver all load correctly while spawning fails.
    """
    candidates = [
        # Deterministic, and what build.py actually names the binary.
        paths.bundled_dir() / (f"{_BINARY_NAME}.exe" if sys.platform == "win32" else _BINARY_NAME),
        # Whatever actually launched us, in case the binary was renamed.
        Path(sys.argv[0]) if sys.argv and sys.argv[0] else None,
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
            if resolved.is_file():
                return str(resolved)
        except Exception:
            continue
    # Nothing resolved. Returning sys.executable keeps the failure a spawn error
    # the UI reports, rather than a TypeError somewhere less legible.
    return sys.executable


def _child_argv(extra: List[str]) -> List[str]:
    """
    How to re-invoke this application for a background run.

    Frozen, the flag goes straight to our own binary. From source `sys.executable`
    is the interpreter, so the script path has to come too.
    """
    if paths.is_frozen():
        return [_frozen_executable(), *extra]
    return [sys.executable, str(Path(__file__).resolve()), *extra]


# -- child mode --------------------------------------------------------------


def _run_agent(platform: str, mode: str) -> int:
    """
    Executes one run. This is the child process's whole job.

    The run is bracketed by `run_started` / `run_finished` telemetry. Those two
    events are what set the `running` flag on the operator's board, and nothing
    was emitting them: every licence showed as idle no matter how many were
    mid-run, and because the reporter only sends a heartbeat while a run id is
    set, an agent between jobs went silent entirely. `run_finished` is in a
    `finally` so a crash still closes the run out -- a licence stuck at
    "running" forever is worse than one that never showed as running at all.
    """
    import asyncio

    from src.core import telemetry

    live = mode == "live"
    try:
        from launcher import run_linkedin_flow, run_naukri_flow
    except Exception as exc:
        print(f"Could not load the agent: {exc}", flush=True)
        return 2

    flow = run_naukri_flow if platform == "naukri" else run_linkedin_flow
    reporter = telemetry.reporter()
    reporter.run_started(mode=f"{platform}:{'live' if live else 'dry'}")

    errors = 0
    try:
        asyncio.run(flow(live=live))
    except KeyboardInterrupt:
        print("Interrupted.", flush=True)
        return 130
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"Run failed: {exc}", flush=True)
        errors = 1
        telemetry.emit("error", message=str(exc)[:500], mode=platform)
        return 1
    finally:
        reporter.run_finished(errors=errors)
        reporter.shutdown()
    return 0


# -- window ------------------------------------------------------------------


def _unblock_bundled_assemblies() -> None:
    """
    Strip the Mark of the Web from our own .NET assemblies. Windows only.

    The desktop window is pywebview, whose Windows backend is pythonnet, which
    asks the .NET Framework to load `pythonnet/runtime/Python.Runtime.dll`. When
    a customer downloads the release zip, Windows tags it with a `Zone.Identifier`
    alternate data stream marking it as Internet-zone, and Explorer's "Extract
    All" copies that tag onto every extracted file. The CLR then refuses to load
    a managed assembly carrying it unless the host opts in via
    `loadFromRemoteSources`, which we do not control -- `ClrLoader.dll` is a
    native shim that swallows the .NET exception and returns NULL, so the whole
    failure surfaces as one unhelpful line:

        RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
        from ...\\pythonnet\\runtime\\Python.Runtime.dll

    ...and, before the crash handler above existed, as nothing at all.

    The customer-side cure is `Get-ChildItem -Recurse | Unblock-File`, which is
    not a thing to put in a setup guide and expect done. Deleting the stream is
    the same operation, so the app does it to itself at startup. Only the managed
    assemblies matter, so this stays a handful of file operations rather than a
    walk of a 230MB tree.

    Best-effort by design: an install under Program Files is not writable by a
    standard user, and there the honest outcome is the real error in a dialog
    rather than a silent no-op that pretends to have fixed something.
    """
    if sys.platform != "win32":
        return

    root = paths.bundled_dir()
    targets = [
        root / "pythonnet" / "runtime" / "Python.Runtime.dll",
        root / "clr_loader" / "ffi" / "dlls" / "amd64" / "ClrLoader.dll",
    ]
    lib = root / "webview" / "lib"
    try:
        targets.extend(p for p in lib.rglob("*.dll"))
    except Exception:
        pass

    for target in targets:
        try:
            stream = f"{target}:Zone.Identifier"
            if os.path.exists(stream):
                os.remove(stream)
                logger.info(f"Cleared the download mark on {target.name}")
        except Exception as exc:
            # Not fatal on its own: the assembly may not be marked, or may
            # already be loadable. If it genuinely is blocked and unwritable,
            # the import below raises and the crash handler reports it.
            logger.debug(f"Could not clear the download mark on {target}: {exc}")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--agent-run":
        platform = sys.argv[2] if len(sys.argv) > 2 else "linkedin"
        mode = sys.argv[3] if len(sys.argv) > 3 else "dry"
        return _run_agent(platform, mode)

    # The two halves of the spawn self-test. Handled before anything else so
    # the test exercises process creation and nothing more.
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test-child":
        return _self_test_child()
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        return _self_test()
    # Fetches Chromium exactly as onboarding does. Exposed as a flag so CI can
    # exercise the real download path in the real frozen binary -- both the
    # thing a customer does first, and the thing that guarantees the browser
    # revision matches the bundled driver rather than whatever pip would fetch.
    if len(sys.argv) > 1 and sys.argv[1] == "--install-browser":
        ok, message = runtime.install(progress=lambda m: print(m, flush=True))
        print(f"install-browser: {'OK' if ok else 'FAILED'} -- {message}", flush=True)
        return 0 if ok else 1

    # Before pywebview, which is what triggers the .NET assembly load.
    _unblock_bundled_assemblies()

    import webview

    paths.ensure_dirs()

    index = ui_dir() / "index.html"
    if not index.exists():
        print(f"UI assets are missing at {index}", file=sys.stderr)
        return 1

    api = Api()
    window = webview.create_window(
        "Job Agent",
        str(index),
        js_api=api,
        width=1040,
        height=760,
        min_size=(880, 620),
        # Painted before the page loads. Must match --chassis in app.css or the
        # window opens on a dark flash and then jumps to the light UI.
        background_color="#E4E2DD",
        text_select=True,
    )
    api.attach(window)
    webview.start()
    return 0



if __name__ == "__main__":
    sys.exit(main())
