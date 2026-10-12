"""
Reports what this agent did back to the licence proxy.

The operator sells this binary and needs to answer, for each customer, "is it
running, what has it applied to, and with which resume". None of that is
knowable from the proxy's own records -- it sees model calls, not outcomes --
and the answers live in a SQLite file on a machine the operator will never
touch. This module is the only path by which they leave.

Three properties matter more than completeness here.

It never blocks the agent. Events go onto an in-memory queue and a daemon thread
posts them; a slow or unreachable proxy delays a report, never a job
application. The hot path's cost is an append under a lock.

It never raises. Every public function swallows its own errors, because the
worst possible outcome of a reporting bug is a customer's run dying halfway
through with a stack trace about analytics. If reporting breaks, reporting is
what stops -- nothing else.

It survives a crash. Events not yet delivered are spooled to disk and reloaded
on the next start, so closing a laptop mid-run costs the operator visibility for
one interval rather than the whole session.
"""

from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from src.core.logger import logger
from src.core.paths import config_dir, ensure_dirs

# Matches the server's cap. Batching beyond this just gets truncated there.
MAX_BATCH = 500

# How long the sender sleeps between flushes. Short enough that the operator's
# board feels live, long enough that an idle agent is not chattering at the
# proxy every second.
FLUSH_INTERVAL_S = 30.0

# Ceiling on the in-memory queue. An agent that runs for hours without a network
# must not grow its own footprint without bound; oldest events are dropped first
# because recent activity is what the board is actually showing.
MAX_QUEUED = 5000

SPOOL_NAME = "telemetry-spool.jsonl"


def _enabled() -> bool:
    """
    Reporting is on unless the customer turns it off.

    Kept as a plain environment check rather than a config-file setting so that
    a customer who wants it off can be told a single line to run, and so that
    the answer cannot depend on config having loaded successfully.
    """
    value = (os.getenv("JOB_AGENT_TELEMETRY") or "").strip().lower()
    return value not in {"off", "0", "false", "no", "disabled"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Reporter:
    def __init__(self) -> None:
        self._queue: Deque[Dict[str, Any]] = deque(maxlen=MAX_QUEUED)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False
        self._run_id = ""
        self._counts: Dict[str, int] = {"job_applied": 0, "job_discovered": 0, "error": 0}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._started or not _enabled():
            return
        self._started = True
        self._load_spool()
        self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)
        self._thread.start()
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        """Flush once on the way out, then spool whatever did not make it."""
        if not self._started:
            return
        self._stop.set()
        try:
            self._flush()
        except Exception:
            pass
        self._save_spool()

    # -- emit ----------------------------------------------------------------

    def emit(self, kind: str, **fields: Any) -> None:
        if not _enabled():
            return
        try:
            event = {"type": kind, "ts": _now(), **fields}
            if self._run_id and "run_id" not in event:
                event["run_id"] = self._run_id
            with self._lock:
                self._queue.append(event)
                # Counted here rather than by the caller, so the totals on
                # `run_finished` cannot disagree with the events that were
                # actually reported for the same run.
                if kind in self._counts:
                    self._counts[kind] += 1
            if not self._started:
                self.start()
        except Exception:
            # Reporting must never be the reason a run fails.
            pass

    def run_started(self, mode: str = "", keywords: Optional[List[str]] = None) -> str:
        self._run_id = f"{int(time.time())}-{os.getpid()}"
        with self._lock:
            self._counts = {k: 0 for k in self._counts}
        self.emit("run_started", mode=mode, keywords=keywords or [])
        return self._run_id

    def run_finished(self, applied: int = -1, discovered: int = -1, errors: int = 0) -> None:
        """Closes the run out. Counts default to what this run actually reported."""
        with self._lock:
            counted = dict(self._counts)
        self.emit(
            "run_finished",
            applied=counted["job_applied"] if applied < 0 else applied,
            discovered=counted["job_discovered"] if discovered < 0 else discovered,
            errors=counted["error"] + errors,
        )
        try:
            self._flush()
        except Exception:
            pass
        self._run_id = ""

    # -- delivery ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(FLUSH_INTERVAL_S):
            try:
                self._flush()
            except Exception:
                pass

    def _take(self) -> List[Dict[str, Any]]:
        with self._lock:
            batch = list(self._queue)[:MAX_BATCH]
            for _ in range(len(batch)):
                self._queue.popleft()
        return batch

    def _requeue(self, batch: List[Dict[str, Any]]) -> None:
        """Undelivered events go back on the *left*, keeping chronological order
        so a later flush does not report today's work before yesterday's."""
        with self._lock:
            self._queue.extendleft(reversed(batch))

    def _flush(self) -> None:
        """
        Posts one batch. A heartbeat is sent even with nothing queued, because an
        agent that is running but has found nothing yet still needs to appear on
        the operator's board as running.
        """
        if not _enabled():
            return

        from src.core import licensing

        base = licensing.api_base_url()
        key = licensing.get_license_key()
        if not base or not key:
            # No proxy configured (local direct mode) -- there is nowhere to
            # report to, so drop rather than spool forever.
            with self._lock:
                self._queue.clear()
            return

        batch = self._take()
        if not batch and not self._run_id:
            return

        import httpx

        try:
            response = httpx.post(
                f"{base}/v1/telemetry",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "events": batch,
                    "device_id": licensing.device_id(),
                    "app_version": licensing.app_version(),
                },
                timeout=15.0,
            )
        except Exception as exc:
            logger.debug(f"Telemetry flush failed, will retry: {exc}")
            self._requeue(batch)
            return

        if response.status_code >= 500 or response.status_code == 429:
            # Ours to retry: the service is unwell or asking us to slow down.
            self._requeue(batch)
            return
        if response.status_code >= 400:
            # A 4xx means this batch will never be accepted -- a revoked licence,
            # or events this server does not understand. Retrying it forever
            # would block every later batch behind it, so it is dropped.
            logger.debug(f"Telemetry batch rejected ({response.status_code}); dropping.")

    # -- spool ---------------------------------------------------------------

    def _spool_path(self) -> Path:
        return config_dir() / SPOOL_NAME

    def _save_spool(self) -> None:
        try:
            with self._lock:
                pending = list(self._queue)
            if not pending:
                path = self._spool_path()
                if path.exists():
                    path.unlink()
                return
            ensure_dirs()
            self._spool_path().write_text(
                "\n".join(json.dumps(e, separators=(",", ":")) for e in pending),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_spool(self) -> None:
        try:
            path = self._spool_path()
            if not path.exists():
                return
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.append(json.loads(line))
                except Exception:
                    continue
            path.unlink()
        except Exception:
            pass


_reporter: Optional[Reporter] = None


def reporter() -> Reporter:
    global _reporter
    if _reporter is None:
        _reporter = Reporter()
        _reporter.start()
    return _reporter


def emit(kind: str, **fields: Any) -> None:
    """Module-level shorthand -- the form every caller should use."""
    try:
        reporter().emit(kind, **fields)
    except Exception:
        pass
