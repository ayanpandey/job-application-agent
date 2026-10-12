"""
Licence records and quota accounting, stored as objects in a single GCS bucket.

Everything this service persists lives under one bucket (`your-bucket-name` by
default), in the operator's own project, reached with the same mounted
service-account key used for Vertex AI. Nothing is stored in the project that
merely hosts the Cloud Run service.

Layout:

    licences/<key_hash>.json          one record per licence
    usage/<key_hash>/<YYYY-MM-DD>.json  that day's call count
    usage/<key_hash>/total.json         lifetime call count

Two things here are deliberate and worth not undoing.

Keys are never stored. Only the SHA-256 of the normalised key is written, so a
dump of the bucket yields no working licences -- the plaintext key exists
exactly once, in the operator's terminal at mint time. A lost key is reissued,
never recovered.

Quota is booked with a compare-and-swap loop rather than a plain read-then-write.
GCS has no transactions, but every object has a generation number and writes can
be made conditional on it, which gives the same exactness: a write whose
generation no longer matches is rejected and retried against fresh state. Two
concurrent searches would otherwise both read the same count and both pass a
check only one of them should, and since the operator pays for every call the
proxy makes, the counter has to be right.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from google.api_core import exceptions as gexc
from google.cloud import storage


def utc_today() -> dt.date:
    """
    Today in UTC, which is the only day this system has.

    Quota counters are stored as `usage/<id>/<YYYY-MM-DD>.json` and the limit
    message promises a reset "at midnight UTC". `dt.date.today()` returns the
    *local* date, so the day a counter was written under depended on the clock
    of whichever machine wrote it. The service runs in UTC and writes the right
    file; an operator in IST (UTC+5:30) running the admin tools between
    midnight and 05:30 local computed tomorrow's filename, read an object that
    did not exist yet, and was told every customer had used nothing today.

    That is a reporting failure that looks exactly like an idle customer, and
    it would also have made quota enforcement drift the moment anything ran
    outside UTC.
    """
    return dt.datetime.now(dt.timezone.utc).date()


# Crockford-style alphabet: no I, L, O or U, so a key read over a phone call or
# retyped from a screenshot cannot land on a neighbouring character.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUPS = 4
_GROUP_LEN = 5

# Printed tag that says which product a key belongs to. Every character of it is
# in the alphabet above, so it survives `normalise` and is part of what gets
# hashed -- two products can therefore never mint the same key, even by chance.
_TAG = "JAA"

PREFIX_LICENCES = "licences"
PREFIX_USAGE = "usage"

# How many times to retry a losing compare-and-swap before giving up. Contention
# is between a single customer's own concurrent calls, so collisions are rare
# and a handful of attempts is ample.
_CAS_ATTEMPTS = 8


# -- key handling ------------------------------------------------------------


def generate_key() -> str:
    """A fresh licence key, e.g. JAA-4K2WP-9XQ7M-B3NHT-Z8RVD."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_GROUPS * _GROUP_LEN))
    groups = [body[i : i + _GROUP_LEN] for i in range(0, len(body), _GROUP_LEN)]
    return f"{_TAG}-" + "-".join(groups)


def normalise(key: str) -> str:
    """
    Strip a key down to its significant characters.

    Clients paste keys with stray whitespace, lowercase them, or lose the
    dashes. All of those should authenticate; only the alphabet matters.
    """
    return "".join(ch for ch in (key or "").upper() if ch in _ALPHABET)


def key_hash(key: str) -> str:
    return hashlib.sha256(normalise(key).encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """
    Product tag plus the first group -- safe to print in listings, logs and
    support tickets.

    The tag is stripped before the group is taken. `normalise` keeps every
    character in the alphabet and "JAA" is made of three of them, so slicing the
    normalised string directly would return the tag again and label the key in
    the example above "JAA-JAA4K" -- which matches nothing the customer can see
    on their own screen, and so is worse than useless on a support call.
    """
    n = normalise(key)
    if not n:
        return f"{_TAG}-?????"
    body = n[len(_TAG):] if n.startswith(_TAG) else n
    return f"{_TAG}-{body[:_GROUP_LEN]}"


# -- records -----------------------------------------------------------------


@dataclass
class Licence:
    key_prefix: str
    name: str = ""
    email: str = ""
    created_at: str = ""
    expires_at: str = ""          # ISO date; "" means no expiry
    revoked: bool = False
    revoked_reason: str = ""
    daily_quota: int = 400
    total_quota: int = 0          # 0 means unlimited
    max_devices: int = 2
    devices: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Licence":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def expiry_date(self) -> Optional[dt.date]:
        if not self.expires_at:
            return None
        try:
            return dt.date.fromisoformat(self.expires_at)
        except ValueError:
            return None

    def days_remaining(self) -> Optional[int]:
        expiry = self.expiry_date()
        if expiry is None:
            return None
        return (expiry - utc_today()).days


class LicenceError(Exception):
    """Raised when a licence cannot be used. `code` maps onto an HTTP status."""

    def __init__(self, message: str, code: str = "invalid_licence", status: int = 403):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# -- store -------------------------------------------------------------------


class LicenceStore:
    def __init__(
        self,
        bucket: str,
        credentials: Any = None,
        project: Optional[str] = None,
        client: Optional[storage.Client] = None,
        prefix: str = "",
    ):
        """
        `prefix` namespaces every object this store writes, so one bucket can
        hold several products without them colliding. Passed as e.g.
        "resumes/job-agent"; leading and trailing slashes are tolerated.
        """
        self.bucket_name = bucket
        self.prefix = (prefix or "").strip("/")
        self._client = client or storage.Client(project=project, credentials=credentials)
        self._bucket = self._client.bucket(bucket)

    # -- object helpers ------------------------------------------------------

    def _at(self, *parts: str) -> str:
        return "/".join(p for p in ((self.prefix,) + parts) if p)

    def _licence_path(self, doc_id: str) -> str:
        return self._at(PREFIX_LICENCES, f"{doc_id}.json")

    def _daily_path(self, doc_id: str, day: str) -> str:
        return self._at(PREFIX_USAGE, doc_id, f"{day}.json")

    def _total_path(self, doc_id: str) -> str:
        return self._at(PREFIX_USAGE, doc_id, "total.json")

    def _read(self, path: str) -> Tuple[Optional[Dict[str, Any]], int]:
        """
        Returns (data, generation). Generation 0 means the object does not
        exist, which is also the precondition value for "create only if absent".
        """
        blob = self._bucket.blob(path)
        try:
            raw = blob.download_as_bytes()
        except gexc.NotFound:
            return None, 0
        return json.loads(raw.decode("utf-8")), int(blob.generation or 0)

    def _write(self, path: str, data: Dict[str, Any], if_generation: Optional[int] = None) -> None:
        blob = self._bucket.blob(path)
        blob.upload_from_string(
            json.dumps(data, indent=2, sort_keys=True),
            content_type="application/json",
            if_generation_match=if_generation,
        )

    # -- admin side ----------------------------------------------------------

    def mint(
        self,
        name: str,
        email: str = "",
        valid_days: int = 365,
        daily_quota: int = 400,
        total_quota: int = 0,
        max_devices: int = 2,
        notes: str = "",
    ) -> Tuple[str, Licence]:
        """
        Creates a licence and returns (plaintext_key, record).

        The plaintext key is returned exactly once and never persisted. Deliver
        it to the customer and keep no copy; reissue if it is lost.
        """
        key = generate_key()
        today = utc_today()
        licence = Licence(
            key_prefix=key_prefix(key),
            name=name,
            email=email,
            created_at=today.isoformat(),
            expires_at=(today + dt.timedelta(days=valid_days)).isoformat() if valid_days else "",
            daily_quota=daily_quota,
            total_quota=total_quota,
            max_devices=max_devices,
            notes=notes,
        )
        # if_generation_match=0 means "only if it does not already exist", so a
        # freak hash collision surfaces instead of overwriting a live licence.
        self._write(self._licence_path(key_hash(key)), licence.to_dict(), if_generation=0)
        return key, licence

    def list_all(self) -> List[Tuple[str, Licence]]:
        out: List[Tuple[str, Licence]] = []
        for blob in self._client.list_blobs(self.bucket_name, prefix=self._at(PREFIX_LICENCES) + "/"):
            if not blob.name.endswith(".json"):
                continue
            doc_id = blob.name.rsplit("/", 1)[-1][: -len(".json")]
            try:
                data = json.loads(blob.download_as_bytes().decode("utf-8"))
            except Exception:
                continue
            out.append((doc_id, Licence.from_dict(data)))
        return out

    def _resolve_id(self, key_or_hash: str) -> Optional[str]:
        """Accepts either a plaintext key or the stored hash, so admin commands
        work equally from a support ticket or from a `list` listing."""
        candidate = (key_or_hash or "").strip()
        if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate.lower()):
            doc_id = candidate.lower()
        else:
            doc_id = key_hash(candidate)
        data, _ = self._read(self._licence_path(doc_id))
        return doc_id if data is not None else None

    def get(self, key_or_hash: str) -> Optional[Tuple[str, Licence]]:
        doc_id = self._resolve_id(key_or_hash)
        if doc_id is None:
            return None
        data, _ = self._read(self._licence_path(doc_id))
        return doc_id, Licence.from_dict(data or {})

    def update_fields(self, key_or_hash: str, **fields: Any) -> bool:
        doc_id = self._resolve_id(key_or_hash)
        if doc_id is None:
            return False
        path = self._licence_path(doc_id)
        for _ in range(_CAS_ATTEMPTS):
            data, generation = self._read(path)
            if data is None:
                return False
            data.update(fields)
            try:
                self._write(path, data, if_generation=generation)
                return True
            except gexc.PreconditionFailed:
                continue
        raise LicenceError("Licence record is being modified concurrently.", "conflict", 409)

    def revoke(self, key_or_hash: str, reason: str = "") -> bool:
        return self.update_fields(key_or_hash, revoked=True, revoked_reason=reason)

    def reinstate(self, key_or_hash: str) -> bool:
        return self.update_fields(key_or_hash, revoked=False, revoked_reason="")

    # -- request path --------------------------------------------------------

    def authenticate(self, key: str, device_id: str = "") -> Tuple[str, Licence]:
        """
        Validates a key and binds the calling device. Raises LicenceError.

        Device binding is a soft anti-sharing measure, not a security boundary:
        it stops one purchased key being passed around an office, and nothing
        more. `max_devices=0` disables it.
        """
        if not normalise(key):
            raise LicenceError("No licence key supplied.", "missing_licence", 401)

        doc_id = key_hash(key)
        data, _ = self._read(self._licence_path(doc_id))
        if data is None:
            raise LicenceError("Licence key not recognised.", "invalid_licence", 401)

        licence = Licence.from_dict(data)

        if licence.revoked:
            raise LicenceError(
                licence.revoked_reason or "This licence has been revoked.", "revoked", 403
            )

        remaining = licence.days_remaining()
        if remaining is not None and remaining < 0:
            raise LicenceError(f"This licence expired on {licence.expires_at}.", "expired", 403)

        if device_id and licence.max_devices and device_id not in licence.devices:
            if len(licence.devices) >= licence.max_devices:
                raise LicenceError(
                    f"This licence is already active on {licence.max_devices} "
                    f"device(s). Contact support to move it.",
                    "device_limit",
                    403,
                )
            licence.devices.append(device_id)
            try:
                self.update_fields(key, devices=licence.devices)
            except LicenceError:
                # Losing this race costs nothing: the device is recorded on the
                # next call, and the seat check above already passed.
                pass

        return doc_id, licence

    def consume(self, doc_id: str, licence: Licence, units: int = 1) -> Dict[str, int]:
        """
        Books `units` against the daily and lifetime counters.

        Returns the post-call counters so the response can carry them back and
        the desktop app can show remaining quota without a second round-trip.
        """
        today = utc_today().isoformat()
        daily_limit = licence.daily_quota or 0
        total_limit = licence.total_quota or 0

        daily = self._bump(self._daily_path(doc_id, today), units, daily_limit,
                           f"Daily limit of {daily_limit} requests reached. Resets at midnight UTC.",
                           "daily_quota_exceeded", extra={"date": today})
        total = self._bump(self._total_path(doc_id), units, total_limit,
                           f"This licence's total allowance of {total_limit} requests is used up.",
                           "total_quota_exceeded")
        return {
            "daily_used": daily,
            "daily_limit": daily_limit,
            "total_used": total,
            "total_limit": total_limit,
        }

    def _bump(
        self,
        path: str,
        units: int,
        limit: int,
        message: str,
        code: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Compare-and-swap increment, enforcing `limit` (0 = unlimited)."""
        for _ in range(_CAS_ATTEMPTS):
            data, generation = self._read(path)
            count = int((data or {}).get("count", 0))
            if limit and count + units > limit:
                raise LicenceError(message, code, 429)
            payload = {"count": count + units, **(extra or {})}
            try:
                self._write(path, payload, if_generation=generation)
                return count + units
            except gexc.PreconditionFailed:
                # Someone else booked a call between our read and our write.
                # Re-read and re-check the limit rather than assuming.
                continue
        raise LicenceError(
            "Too many simultaneous requests on this licence. Try again.", "conflict", 409
        )

    def usage_snapshot(self, doc_id: str) -> Dict[str, int]:
        """Counters without booking anything -- for /v1/activate and admin listings."""
        today = utc_today().isoformat()
        daily, _ = self._read(self._daily_path(doc_id, today))
        total, _ = self._read(self._total_path(doc_id))
        return {
            "daily_used": int((daily or {}).get("count", 0)),
            "total_used": int((total or {}).get("count", 0)),
        }
