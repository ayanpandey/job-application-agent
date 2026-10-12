"""
Persistent cache for LLM responses.

Two workloads in this agent ask the model the same thing over and over:

  * Job matching. The keyword grid overlaps heavily -- a posting that surfaces
    under "Audit" surfaces again under "IT Audit" and again under "SOX
    Compliance" -- so the same description was being scored three or four times
    per session at full latency.

  * Screening questions. "How many years of experience do you have with SOX?"
    and "What is your notice period?" appear on a large fraction of Easy Apply
    forms. Every one of those was a fresh round-trip, in the middle of a form,
    with the modal open and the clock running.

Both are keyed on content, not on job id, so the cache hits across postings.
The candidate profile is part of every key: edit the profile and every stored
answer is invalidated, which is the behaviour you want when the thing being
cached is a claim made on the candidate's behalf.

Stored in the same SQLite file as everything else so a cache survives restarts
and needs no separate lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any, Dict, Optional

from src.core import paths
from src.core.logger import logger


def profile_fingerprint(profile: Any) -> str:
    """
    Short digest of the candidate profile.

    Keying on this means a profile edit silently retires every cached answer
    derived from the old one, rather than the agent confidently repeating a
    stale salary expectation for the next ninety days.
    """
    try:
        payload = json.dumps(profile.model_dump(), sort_keys=True, default=str)
    except Exception:
        payload = repr(profile)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_key(kind: str, *parts: Any) -> str:
    joined = "\x1f".join(str(p) for p in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


class LLMCache:
    """
    Content-addressed store for model responses, with a per-kind expiry.

    Every method swallows its own errors. A cache that raises would take down a
    working application flow to protect an optimisation, which is backwards.
    """

    def __init__(self, db_path: Optional[str] = None, enabled: bool = True):
        self.db_path = paths.resolve_writable(db_path, paths.database_path())
        self.enabled = enabled
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS llm_cache (
                        cache_key   TEXT PRIMARY KEY,
                        kind        TEXT NOT NULL,
                        payload     TEXT NOT NULL,
                        hit_count   INTEGER DEFAULT 0,
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_cache_kind ON llm_cache(kind)")
                conn.commit()
        except Exception as exc:
            logger.debug(f"LLM cache unavailable, running without it: {exc}")
            self.enabled = False

    def get(self, key: str, max_age_days: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            with self._lock, self._connect() as conn:
                if max_age_days is None:
                    row = conn.execute(
                        "SELECT payload FROM llm_cache WHERE cache_key = ?", (key,)
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT payload FROM llm_cache WHERE cache_key = ? "
                        "AND created_at >= datetime('now', ?)",
                        (key, f"-{max_age_days} days"),
                    ).fetchone()
                if row is None:
                    self.misses += 1
                    return None
                conn.execute(
                    "UPDATE llm_cache SET hit_count = hit_count + 1 WHERE cache_key = ?", (key,)
                )
                conn.commit()
                self.hits += 1
                return json.loads(row["payload"])
        except Exception as exc:
            logger.debug(f"LLM cache read skipped: {exc}")
            return None

    def put(self, key: str, kind: str, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO llm_cache (cache_key, kind, payload, created_at) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(cache_key) DO UPDATE SET "
                    "payload = excluded.payload, created_at = CURRENT_TIMESTAMP",
                    (key, kind, json.dumps(payload, default=str)),
                )
                conn.commit()
        except Exception as exc:
            logger.debug(f"LLM cache write skipped: {exc}")

    def stats(self) -> str:
        total = self.hits + self.misses
        if not total:
            return "cache: no lookups"
        return f"cache: {self.hits}/{total} hits ({self.hits / total:.0%})"
