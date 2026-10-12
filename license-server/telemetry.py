"""
Telemetry ingestion: what a licensed agent did, recorded against its licence.

The dashboard question this exists to answer is "who is running right now, what
have they applied to, and with which resume" -- none of which the proxy could
answer before, because it only ever saw model calls. Application records lived
in a SQLite file on the customer's own laptop and never left it.

Two rules shape everything below.

Events are append-only, one object per batch. GCS objects cannot be appended to,
and the obvious alternative -- read the day's file, add to it, write it back --
turns every reporting agent into a compare-and-swap contender against every
other. Batches are instead written to their own object under a day prefix, so
concurrent writers never contend at all and a day's activity is a `list_blobs`
away. Nothing here needs the CAS machinery the quota counters need, because
nothing here is a limit that must not be exceeded.

Ingestion never consumes model quota. A customer who reports diligently must not
run out of the allowance they paid for sooner than one who reports nothing; that
would make the honest client the expensive one. Abuse is bounded by payload caps
and a per-day event ceiling instead, which cost the customer nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import secrets
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from google.api_core import exceptions as gexc


def _content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"

PREFIX_TELEMETRY = "telemetry"
PREFIX_HEARTBEAT = "heartbeat"
PREFIX_RESUMES = "resumes"

# A tailored resume is a two-page PDF or its LaTeX source. Anything an order of
# magnitude past that is not a resume, and the bucket is the operator's to pay
# for, so it is refused at the door rather than stored and reasoned about later.
MAX_RESUME_BYTES = 4 * 1024 * 1024
RESUME_SUFFIXES = {".pdf", ".tex", ".json", ".txt", ".docx"}

# Event kinds the server will store. Anything else is dropped rather than
# rejected: a newer client emitting a kind this server has not learned yet
# should not have its whole batch fail, and an older server should not become a
# deployment blocker for a client rollout.
EVENT_KINDS = {
    "run_started",
    "run_finished",
    "job_discovered",
    "job_applied",
    "job_status_changed",
    "job_skipped",
    "outreach_sent",
    "outreach_reply",
    "resume_generated",
    "error",
}

# Fields copied off an event, per kind. Everything not listed is discarded at the
# door. This is a whitelist and not a blacklist on purpose: the client is a
# binary the operator ships, but it runs on a machine the operator does not
# control, and a field that is never stored cannot become a liability later.
EVENT_FIELDS = {
    "ts", "run_id", "job_id", "title", "company", "location", "url",
    "match_score", "match_reason", "posted", "source", "status",
    "external_status", "resume_file", "resume_uri", "deviation_pct",
    "fidelity_score", "recipient", "recipient_title", "profile_url",
    "reply_text", "mode", "keywords", "applied", "discovered", "errors",
    "message", "duration_s",
}

MAX_EVENTS_PER_BATCH = 500
MAX_EVENTS_PER_DAY = 20000
MAX_FIELD_CHARS = 2000


def _clip(value: Any) -> Any:
    """Bounds a single field. Long strings are truncated rather than refused --
    a 4000-character job description should cost the customer a shortened record,
    not a failed batch."""
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return str(value)[:MAX_FIELD_CHARS]


def sanitise(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reduces a client batch to storable records, dropping unknown kinds."""
    out: List[Dict[str, Any]] = []
    for raw in list(events)[:MAX_EVENTS_PER_BATCH]:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "").strip()
        if kind not in EVENT_KINDS:
            continue
        record = {"type": kind}
        for field in EVENT_FIELDS:
            if field in raw:
                record[field] = _clip(raw[field])
        record.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
        out.append(record)
    return out


class TelemetryStore:
    """
    Writes activity records beside the licence records they belong to.

    Shares the bucket, prefix and credentials of the LicenceStore so that a
    product's licences and its activity stay together and a second product only
    needs a different prefix -- the same separation `LicenceStore` already makes.
    """

    def __init__(self, licence_store: Any):
        self._store = licence_store
        self._bucket = licence_store._bucket
        self._client = licence_store._client
        self.bucket_name = licence_store.bucket_name
        self.prefix = licence_store.prefix

    def _at(self, *parts: str) -> str:
        return "/".join(p for p in ((self.prefix,) + parts) if p)

    def _day_prefix(self, doc_id: str, day: str) -> str:
        return self._at(PREFIX_TELEMETRY, doc_id, day)

    def _heartbeat_path(self, doc_id: str) -> str:
        return self._at(PREFIX_HEARTBEAT, f"{doc_id}.json")

    # -- ingest --------------------------------------------------------------

    def record(
        self,
        doc_id: str,
        events: List[Dict[str, Any]],
        device_id: str = "",
        app_version: str = "",
    ) -> Dict[str, Any]:
        """
        Stores one batch and refreshes the licence's heartbeat.

        Returns a small receipt the client can log. The heartbeat is written even
        when the batch is empty, because an agent with nothing to report is still
        an agent that is running, and "currently working" is exactly what the
        operator's board is asking.
        """
        clean = sanitise(events)
        now = dt.datetime.now(dt.timezone.utc)
        day = now.date().isoformat()

        stored = 0
        if clean:
            if self._count_today(doc_id, day) + len(clean) <= MAX_EVENTS_PER_DAY:
                name = f"{int(now.timestamp() * 1000)}-{secrets.token_hex(4)}.json"
                path = f"{self._day_prefix(doc_id, day)}/{name}"
                payload = {
                    "received_at": now.isoformat(),
                    "device_id": _clip(device_id),
                    "app_version": _clip(app_version),
                    "events": clean,
                }
                self._bucket.blob(path).upload_from_string(
                    json.dumps(payload, separators=(",", ":")),
                    content_type="application/json",
                )
                stored = len(clean)

        self._touch_heartbeat(doc_id, now, clean, device_id, app_version)
        return {"accepted": stored, "dropped": len(events) - stored}

    def _count_today(self, doc_id: str, day: str) -> int:
        """Approximate day volume, from object count rather than content -- the
        ceiling is an abuse bound, not an accounting figure, so listing names is
        enough and reading every batch back would cost more than it protects."""
        try:
            blobs = self._client.list_blobs(
                self.bucket_name, prefix=self._day_prefix(doc_id, day) + "/", max_results=200
            )
            return sum(1 for _ in blobs) * MAX_EVENTS_PER_BATCH
        except Exception:
            return 0

    def _touch_heartbeat(
        self,
        doc_id: str,
        now: dt.datetime,
        events: List[Dict[str, Any]],
        device_id: str,
        app_version: str,
    ) -> None:
        """
        Last-seen state for one licence, as a single small object.

        Overwritten unconditionally rather than compare-and-swapped: this is a
        "most recent wins" fact, two devices racing to write it both hold a
        truthful answer, and losing one write costs nothing that the next
        heartbeat does not restore.
        """
        state = {"last_seen": now.isoformat(), "device_id": device_id, "app_version": app_version}

        prior, _ = self._store._read(self._heartbeat_path(doc_id))
        if prior:
            state["running"] = prior.get("running", False)
            state["run_id"] = prior.get("run_id", "")
            state["run_started_at"] = prior.get("run_started_at", "")

        for event in events:
            if event["type"] == "run_started":
                state["running"] = True
                state["run_id"] = event.get("run_id", "")
                state["run_started_at"] = event.get("ts", now.isoformat())
            elif event["type"] == "run_finished":
                state["running"] = False
                state["last_run_finished_at"] = event.get("ts", now.isoformat())
                state["last_run_applied"] = event.get("applied", 0)
                state["last_run_discovered"] = event.get("discovered", 0)

        try:
            self._bucket.blob(self._heartbeat_path(doc_id)).upload_from_string(
                json.dumps(state, separators=(",", ":")), content_type="application/json"
            )
        except gexc.GoogleAPIError:
            # A missed heartbeat degrades the board's freshness for one interval
            # and nothing else. It must never fail the batch that carried it.
            pass

    # -- resumes -------------------------------------------------------------

    def store_resume(
        self, doc_id: str, filename: str, content: bytes, candidate: str = ""
    ) -> str:
        """
        Stores one generated resume and returns its `gs://` URI.

        Filed under the licence's own hash rather than under the candidate name
        the client supplied. A client-supplied name is untrusted input that
        would otherwise decide a storage path -- two customers could collide on
        it, and a crafted one could climb out of the prefix entirely. The
        candidate slug is kept only as object metadata, where it is a label and
        not a location.
        """
        safe_name = Path(filename or "resume").name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in RESUME_SUFFIXES:
            raise ValueError(f"Unsupported resume file type: {safe_name}")
        if len(content) > MAX_RESUME_BYTES:
            raise ValueError("Resume exceeds the size limit.")

        # The object name is opaque. A resume's own filename is
        # "Priya_Sharma_Resume_Acme_1.pdf" -- candidate, employer and role, all
        # in a string that then appears in the bucket's access logs, which live
        # in a *second* bucket with its own readers. Anyone able to read those
        # logs could reconstruct every customer's job search without opening a
        # single file. Object paths are effectively public to anyone with any
        # read on either bucket, so nothing identifying goes in them; the real
        # filename rides in metadata, which access logs do not record.
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = self._at(PREFIX_RESUMES, doc_id, f"{stamp}-{secrets.token_hex(8)}{suffix}")
        blob = self._bucket.blob(path)
        blob.metadata = {"candidate": _clip(candidate), "filename": _clip(safe_name)}
        blob.upload_from_string(content, content_type=_content_type(safe_name))
        return f"gs://{self.bucket_name}/{path}"

    def resumes_for(self, doc_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Every resume stored against one licence, newest first."""
        out: List[Dict[str, Any]] = []
        for blob in self._client.list_blobs(
            self.bucket_name, prefix=self._at(PREFIX_RESUMES, doc_id) + "/", max_results=limit
        ):
            meta = blob.metadata or {}
            out.append({
                # The real filename comes from metadata now that the object name
                # is opaque, so the console still shows something a human can
                # recognise on a support call.
                "name": meta.get("filename") or blob.name.rsplit("/", 1)[-1],
                "object": blob.name.rsplit("/", 1)[-1],
                "path": blob.name,
                "size": blob.size,
                "updated": blob.updated.isoformat() if blob.updated else "",
                "uri": f"gs://{self.bucket_name}/{blob.name}",
                "candidate": meta.get("candidate", ""),
            })
        out.sort(key=lambda r: r["name"], reverse=True)
        return out

    # -- read side (used by the admin platform) ------------------------------

    def heartbeat(self, doc_id: str) -> Dict[str, Any]:
        data, _ = self._store._read(self._heartbeat_path(doc_id))
        return data or {}

    def events_for_day(self, doc_id: str, day: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for blob in self._client.list_blobs(
            self.bucket_name, prefix=self._day_prefix(doc_id, day) + "/"
        ):
            try:
                batch = json.loads(blob.download_as_bytes().decode("utf-8"))
            except Exception:
                continue
            for event in batch.get("events", []):
                event.setdefault("device_id", batch.get("device_id", ""))
                out.append(event)
        out.sort(key=lambda e: e.get("ts", ""))
        return out

    def batches_since(self, doc_id: str, after: str = "") -> List[Dict[str, Any]]:
        """
        Every batch object whose name sorts after `after`, oldest first.

        Object names begin with a millisecond timestamp, so lexical order is
        chronological order and a single string is a sufficient cursor. That is
        what lets the admin platform mirror new activity into its own database
        without re-reading history on every poll.
        """
        out: List[Dict[str, Any]] = []
        for blob in self._client.list_blobs(
            self.bucket_name, prefix=self._at(PREFIX_TELEMETRY, doc_id) + "/"
        ):
            if after and blob.name <= after:
                continue
            try:
                batch = json.loads(blob.download_as_bytes().decode("utf-8"))
            except Exception:
                continue
            batch["_object"] = blob.name
            out.append(batch)
        out.sort(key=lambda b: b["_object"])
        return out
