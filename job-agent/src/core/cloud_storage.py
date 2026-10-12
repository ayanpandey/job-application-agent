"""
Google Cloud Storage integration for backing up and syncing master and tailored resumes to bucket `your-bucket-name`.
"""

from __future__ import annotations

import os
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core import paths
from src.core.logger import logger

DEFAULT_BUCKET = os.getenv("STORAGE_BUCKET", "your-bucket-name")


def candidate_slug(name: str) -> str:
    """
    The per-candidate folder name under resumes/candidates/.

    Every caller has to agree on this or the automatic per-resume backup and the
    manual Sync button write to two different folders for the same person.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "candidate"


def blob_name_for(file_name: str, candidate: str, is_tailored: bool = True) -> str:
    """The bucket path a resume file lands at, without needing a GCS client."""
    return f"resumes/candidates/{candidate}/{'tailored' if is_tailored else 'master'}/{file_name}"


def gcs_uri_for(file_name: str, candidate: str, is_tailored: bool = True,
                bucket: str = DEFAULT_BUCKET) -> str:
    return f"gs://{bucket}/{blob_name_for(file_name, candidate, is_tailored)}"


def backup_resume(local_path: Path | str, candidate_name: str) -> str:
    """
    Uploads one resume through the licence proxy. Returns its URI, or "".

    The proxy holds the only credentials that can write to the operator's
    bucket, exactly as it holds the only credentials that can call the model.
    This function therefore posts the file to `/v1/resume`, authenticated with
    the customer's licence key, and the server does the storing.

    It used to build a `storage.Client` here instead, from a service-account
    key found on disk. That could only ever work on the developer's own laptop:
    a customer install has no such key and never will, so every upload failed
    while the UI went on showing `gs://` URIs for files that did not exist.
    Shipping a key to make it work is the one thing this product may not do.
    """
    import httpx

    from src.core import licensing

    path = Path(local_path)
    if not path.exists():
        return ""

    base = licensing.api_base_url()
    key = licensing.get_license_key()
    if not base or not key:
        return ""

    try:
        with path.open("rb") as handle:
            response = httpx.post(
                f"{base}/v1/resume",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, handle, "application/octet-stream")},
                data={"candidate": candidate_slug(candidate_name), "device_id": licensing.device_id()},
                timeout=60.0,
            )
    except Exception as exc:
        logger.debug(f"Resume backup failed: {exc}")
        return ""

    if response.status_code != 200:
        logger.debug(f"Resume backup rejected ({response.status_code}).")
        return ""
    try:
        return str(response.json().get("uri") or "")
    except Exception:
        return ""


class CloudStorageManager:
    """
    Retained for local development only.

    Reaches GCS directly with whatever credentials the developer's environment
    already provides (ADC, or GOOGLE_APPLICATION_CREDENTIALS). It is never used
    by a customer install -- there, `backup_resume` above goes through the
    proxy. Note there is deliberately no hard-coded project id any more: this
    class used to default to the operator's own project, which is exactly the
    kind of value that must not be compiled into a shipped binary.
    """

    def __init__(self, bucket_name: str = DEFAULT_BUCKET, project_id: Optional[str] = None):
        self.bucket_name = bucket_name
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID") or None
        self._client = None
        self._bucket = None

    def _init_client(self) -> bool:
        if self._client is not None:
            return True
        try:
            from google.cloud import storage

            self._client = storage.Client(project=self.project_id)
            self._bucket = self._client.bucket(self.bucket_name)
            return True
        except Exception as exc:
            logger.warning(f"Could not initialize GCS storage client: {exc}")
            return False

    def upload_file(
        self,
        local_path: Path | str,
        remote_blob_name: str,
        content_type: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Uploads a single file to GCS."""
        local_path = Path(local_path).resolve()
        if not local_path.exists():
            return False, f"Local file not found: {local_path}"

        if not self._init_client():
            return False, "Google Cloud Storage client not available."

        try:
            blob = self._bucket.blob(remote_blob_name)
            if not content_type:
                guessed, _ = mimetypes.guess_type(str(local_path))
                content_type = guessed or "application/octet-stream"

            blob.upload_from_filename(str(local_path), content_type=content_type)
            gcs_uri = f"gs://{self.bucket_name}/{remote_blob_name}"
            logger.info(f"✓ Uploaded {local_path.name} -> {gcs_uri}")
            return True, gcs_uri
        except Exception as exc:
            err = f"Failed to upload {local_path.name} to GCS: {exc}"
            logger.error(err)
            return False, err

    def upload_resume(
        self,
        local_path: Path | str,
        candidate_slug: str = "candidate",
        is_tailored: bool = True,
    ) -> Tuple[bool, str]:
        """
        Uploads a resume PDF/TeX/JSON to candidate storage in your-bucket-name.
        Path pattern: resumes/candidates/{candidate_slug}/[tailored|master]/{filename}
        """
        p = Path(local_path)
        return self.upload_file(p, blob_name_for(p.name, candidate_slug, is_tailored))

    def sync_candidate_resumes(self, candidate_slug: str = "candidate") -> Dict[str, Any]:
        """
        Scans local resumes directory (master + generated) and uploads all files to GCS.
        """
        results = {"uploaded": [], "failed": [], "skipped": [], "total": 0}
        resumes_root = paths.resumes_dir()
        if not resumes_root.exists():
            return results

        # 1. Master resumes
        for f in resumes_root.glob("*.pdf"):
            results["total"] += 1
            ok, msg = self.upload_resume(f, candidate_slug=candidate_slug, is_tailored=False)
            if ok:
                results["uploaded"].append({"file": f.name, "uri": msg, "type": "master"})
            else:
                results["failed"].append({"file": f.name, "error": msg})

        # 2. Generated tailored resumes
        gen_dir = resumes_root / "generated"
        if gen_dir.exists():
            for ext in ("*.pdf", "*.tex", "*.metadata.json"):
                for f in gen_dir.glob(ext):
                    results["total"] += 1
                    ok, msg = self.upload_resume(f, candidate_slug=candidate_slug, is_tailored=True)
                    if ok:
                        results["uploaded"].append({"file": f.name, "uri": msg, "type": "tailored"})
                    else:
                        results["failed"].append({"file": f.name, "error": msg})

        return results

    def list_cloud_resumes(self, candidate_slug: str = "candidate") -> List[Dict[str, Any]]:
        """Lists all uploaded candidate resumes in GCS."""
        if not self._init_client():
            return []
        try:
            prefix = f"resumes/candidates/{candidate_slug}/"
            blobs = self._client.list_blobs(self.bucket_name, prefix=prefix)
            items = []
            for b in blobs:
                items.append({
                    "name": b.name.split("/")[-1],
                    "path": b.name,
                    "size": b.size,
                    "updated": b.updated.isoformat() if b.updated else "",
                    "gcs_uri": f"gs://{self.bucket_name}/{b.name}",
                    "type": "tailored" if "/tailored/" in b.name else "master",
                })
            return items
        except Exception as exc:
            logger.error(f"Failed to list GCS resumes: {exc}")
            return []
