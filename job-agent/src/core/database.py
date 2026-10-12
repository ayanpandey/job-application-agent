"""
SQLite database persistence layer for LinkedIn jobs, applications, and outreach records.
"""

import sqlite3
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from src.core.models import JobListing, JobStatus, OutreachRecord, OutreachStatus
from src.core.logger import logger
from src.core import paths, telemetry


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = paths.resolve_writable(db_path, paths.database_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Jobs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    url TEXT NOT NULL,
                    is_easy_apply BOOLEAN DEFAULT 1,
                    description TEXT,
                    posted_date_text TEXT,
                    recruiter_name TEXT,
                    recruiter_profile_url TEXT,
                    status TEXT NOT NULL,
                    match_score INTEGER,
                    match_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    applied_at TIMESTAMP
                )
            """)

            # Outreach table (Option 3)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS outreach (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    company TEXT NOT NULL,
                    recipient_name TEXT NOT NULL,
                    recipient_title TEXT NOT NULL,
                    profile_url TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sent_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
                )
            """)

            # What the job board reports back about an application after we
            # submitted it -- viewed, rejected, moved to interview. Kept apart
            # from jobs.status because that column tracks what *this agent* did,
            # and conflating the two would lose the distinction between "we
            # applied" and "they responded".
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job_external_status (
                    job_id TEXT PRIMARY KEY,
                    external_status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'linkedin',
                    seen_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
                )
            """)

            # Daily Activity Log (Rate Limit Guardrail)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_type TEXT NOT NULL, -- 'apply' or 'outreach'
                    target_id TEXT NOT NULL,
                    created_date DATE DEFAULT (DATE('now')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()

    def is_job_seen(self, job_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))
            return cursor.fetchone() is not None

    def get_job(self, job_id: str) -> Optional[JobListing]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return JobListing(
                id=row["id"],
                job_id=row["job_id"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                url=row["url"],
                is_easy_apply=bool(row["is_easy_apply"]),
                description=row["description"] or "",
                posted_date_text=row["posted_date_text"] or "",
                recruiter_name=row["recruiter_name"],
                recruiter_profile_url=row["recruiter_profile_url"],
                status=JobStatus(row["status"]),
                match_score=row["match_score"],
                match_reason=row["match_reason"],
            )

    def save_job(self, job: JobListing) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO jobs (
                    job_id, title, company, location, url, is_easy_apply,
                    description, posted_date_text, recruiter_name,
                    recruiter_profile_url, status, match_score, match_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    description = excluded.description,
                    match_score = excluded.match_score,
                    match_reason = excluded.match_reason,
                    status = excluded.status
            """, (
                job.job_id, job.title, job.company, job.location, job.url,
                job.is_easy_apply, job.description, job.posted_date_text,
                job.recruiter_name, job.recruiter_profile_url,
                job.status.value, job.match_score, job.match_reason
            ))
            conn.commit()
            inserted = cursor.lastrowid or 0

        # Reported here rather than from each discovery module: every path that
        # learns about a job ends at this method, so one call covers LinkedIn,
        # Naukri and anything added later without them each remembering to.
        telemetry.emit(
            "job_discovered",
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            match_score=job.match_score,
            match_reason=job.match_reason,
            posted=job.posted_date_text,
            status=job.status.value,
        )
        return inserted

    def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        match_score: Optional[int] = None,
        match_reason: Optional[str] = None
    ):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Score and reason are updated independently. They used to be one
            # branch keyed on the score, so every caller that passed a reason
            # *without* a score -- which is every skip path, e.g.
            # match_reason="Not Easy Apply" -- had its reason silently dropped
            # and the row kept the stale reason from evaluation. The operator
            # then had no way to tell why a job was skipped.
            if match_score is not None and match_reason is not None:
                cursor.execute(
                    "UPDATE jobs SET status = ?, match_score = ?, match_reason = ? WHERE job_id = ?",
                    (status.value, match_score, match_reason, job_id),
                )
            elif match_score is not None:
                cursor.execute(
                    "UPDATE jobs SET status = ?, match_score = ? WHERE job_id = ?",
                    (status.value, match_score, job_id),
                )
            elif match_reason is not None:
                cursor.execute(
                    "UPDATE jobs SET status = ?, match_reason = ? WHERE job_id = ?",
                    (status.value, match_reason, job_id),
                )
            else:
                cursor.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status.value, job_id))
            conn.commit()

        if status in (JobStatus.SKIPPED, JobStatus.FAILED, JobStatus.EVALUATED_REJECT):
            telemetry.emit(
                "job_skipped",
                job_id=job_id,
                status=status.value,
                match_reason=match_reason or "",
                match_score=match_score,
            )

    def mark_job_applied(self, job_id: str, resume_file: str = ""):
        """
        `resume_file` is the tailored resume actually uploaded, when the caller
        knows it. It is optional because two call sites -- the "already applied"
        branch and the Naukri path -- mark a job applied without having chosen a
        resume in that same scope, and an application recorded without its
        resume is still worth recording.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                UPDATE jobs SET status = ?, applied_at = ? WHERE job_id = ?
            """, (JobStatus.APPLIED.value, now, job_id))

            cursor.execute("""
                INSERT INTO activity_log (activity_type, target_id) VALUES ('apply', ?)
            """, (job_id,))
            conn.commit()

        job = self.get_job(job_id)
        telemetry.emit(
            "job_applied",
            job_id=job_id,
            title=job.title if job else "",
            company=job.company if job else "",
            location=job.location if job else "",
            url=job.url if job else "",
            match_score=job.match_score if job else None,
            resume_file=resume_file,
        )

    def get_daily_activity_count(self, activity_type: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM activity_log
                WHERE activity_type = ? AND created_date = DATE('now')
            """, (activity_type,))
            return cursor.fetchone()[0]

    def save_outreach(self, outreach: OutreachRecord) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO outreach (
                    job_id, company, recipient_name, recipient_title, profile_url, note_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                outreach.job_id, outreach.company, outreach.recipient_name,
                outreach.recipient_title, outreach.profile_url, outreach.note_text,
                outreach.status.value
            ))
            conn.commit()
            return cursor.lastrowid or 0

    def mark_outreach_sent(self, outreach_id: int):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                UPDATE outreach SET status = ?, sent_at = ? WHERE id = ?
            """, (OutreachStatus.SENT.value, now, outreach_id))

            cursor.execute("""
                INSERT INTO activity_log (activity_type, target_id) VALUES ('outreach', ?)
            """, (str(outreach_id),))

            cursor.execute(
                "SELECT job_id, company, recipient_name, recipient_title, profile_url "
                "FROM outreach WHERE id = ?",
                (outreach_id,),
            )
            row = cursor.fetchone()
            conn.commit()

        telemetry.emit(
            "outreach_sent",
            job_id=row["job_id"] if row else "",
            company=row["company"] if row else "",
            recipient=row["recipient_name"] if row else "",
            recipient_title=row["recipient_title"] if row else "",
            profile_url=row["profile_url"] if row else "",
        )

    # -- external status (what LinkedIn says happened after we applied) -------

    def record_external_status(
        self, job_id: str, external_status: str, source: str = "linkedin"
    ) -> bool:
        """
        Records what the job board reports about a submitted application.

        Returns True only when this is a *change*. The tracker re-reads the whole
        applied list on every sweep, so without that check every sweep would
        report every application again and the operator's board would show a
        stream of events where nothing actually happened.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT external_status FROM job_external_status WHERE job_id = ?", (job_id,)
            )
            row = cursor.fetchone()
            if row and row["external_status"] == external_status:
                return False

            now = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT INTO job_external_status (job_id, external_status, source, seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    external_status = excluded.external_status,
                    source = excluded.source,
                    seen_at = excluded.seen_at
            """, (job_id, external_status, source, now))
            conn.commit()

        job = self.get_job(job_id)
        telemetry.emit(
            "job_status_changed",
            job_id=job_id,
            external_status=external_status,
            source=source,
            title=job.title if job else "",
            company=job.company if job else "",
        )
        return True

    def get_applied_jobs(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Submitted applications, newest first, with any external status known."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT j.job_id, j.title, j.company, j.url, j.applied_at,
                       s.external_status, s.seen_at
                FROM jobs j
                LEFT JOIN job_external_status s ON s.job_id = j.job_id
                WHERE j.status = ?
                ORDER BY j.applied_at DESC
                LIMIT ?
            """, (JobStatus.APPLIED.value, limit))
            return [dict(r) for r in cursor.fetchall()]

    def get_unapplied_matches(self, limit: int = 200) -> List[JobListing]:
        """
        Postings judged a match that were never applied to, strongest first.

        These accumulate whenever a session ends between the evaluation and the
        application -- the rhythm stopping the run, the browser dropping, a quota
        reached mid-page. Before applications moved inside the scan they were
        simply abandoned: nothing ever revisited a match from a previous session,
        so the work of finding and scoring them was thrown away.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM jobs
                WHERE status = ? AND applied_at IS NULL
                ORDER BY match_score DESC, created_at DESC
                LIMIT ?
            """, (JobStatus.EVALUATED_MATCH.value, limit))
            rows = cursor.fetchall()

        out: List[JobListing] = []
        for row in rows:
            out.append(JobListing(
                id=row["id"],
                job_id=row["job_id"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                url=row["url"],
                is_easy_apply=bool(row["is_easy_apply"]),
                description=row["description"] or "",
                posted_date_text=row["posted_date_text"] or "",
                recruiter_name=row["recruiter_name"],
                recruiter_profile_url=row["recruiter_profile_url"],
                status=JobStatus(row["status"]),
                match_score=row["match_score"],
                match_reason=row["match_reason"],
            ))
        return out

    def get_pending_outreach(self) -> List[OutreachRecord]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM outreach WHERE status = ?", (OutreachStatus.QUEUED.value,))
            rows = cursor.fetchall()
            return [
                OutreachRecord(
                    id=row["id"],
                    job_id=row["job_id"],
                    company=row["company"],
                    recipient_name=row["recipient_name"],
                    recipient_title=row["recipient_title"],
                    profile_url=row["profile_url"],
                    note_text=row["note_text"],
                    status=OutreachStatus(row["status"]),
                )
                for row in rows
            ]

    def get_statistics(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM jobs")
            total_jobs = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (JobStatus.APPLIED.value,))
            applied_jobs = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (JobStatus.EVALUATED_MATCH.value,))
            matched_jobs = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM outreach WHERE status = ?", (OutreachStatus.SENT.value,))
            sent_outreach = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM outreach WHERE status = ?", (OutreachStatus.QUEUED.value,))
            queued_outreach = cursor.fetchone()[0]

            return {
                "total_jobs_found": total_jobs,
                "matches_found": matched_jobs,
                "applied_jobs": applied_jobs,
                "today_applications": self.get_daily_activity_count("apply"),
                "today_outreach": self.get_daily_activity_count("outreach"),
                "outreach_sent": sent_outreach,
                "outreach_queued": queued_outreach,
            }

    def get_recent_jobs(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Jobs for the desktop Applications tab, most actionable first.

        `skipped` outranks `evaluated_reject` because a skip happens partway
        through an apply -- after a resume was tailored for the job -- whereas a
        reject never got that far. Sorting skips down with the rejects pushed the
        rows that actually have a resume attached out of the result window.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM jobs
                ORDER BY
                    CASE
                        WHEN status = 'applied' THEN 1
                        WHEN status = 'evaluated_match' THEN 2
                        WHEN status = 'failed' THEN 3
                        WHEN status = 'skipped' THEN 4
                        WHEN status = 'evaluated_reject' THEN 5
                        ELSE 6
                    END,
                    id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "title": row["title"],
                    "company": row["company"],
                    "location": row["location"],
                    "url": row["url"],
                    "is_easy_apply": bool(row["is_easy_apply"]),
                    "status": row["status"],
                    "match_score": row["match_score"],
                    "match_reason": row["match_reason"],
                    "created_at": row["created_at"],
                    "applied_at": row["applied_at"],
                }
                for row in rows
            ]

