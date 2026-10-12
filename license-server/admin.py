#!/usr/bin/env python3
"""
Licence administration, run locally by the operator.

Deliberately not an HTTP endpoint. Minting and revoking are the highest-value
operations in the system, so they are reachable only by someone holding Google
credentials for the project -- there is no admin surface on the public internet
to find, guess or leak.

Authenticates with your own gcloud Application Default Credentials:

    gcloud auth application-default login
    pip install google-cloud-storage
    python admin.py mint --name "Acme Ltd" --email ops@acme.com

The plaintext key is shown once, at mint time, and is not recoverable
afterwards. Send it to the customer and keep no copy; if they lose it, revoke
the old one and mint a new one.
"""

from __future__ import annotations

import argparse
import os
import sys

from keystore import Licence, LicenceStore, key_prefix

STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "your-bucket-name")
STORAGE_PREFIX = os.getenv("STORAGE_PREFIX", "resumes/job-agent")
# Optional: point at the same service-account key the deployed service mounts.
# Without it, your own gcloud Application Default Credentials are used, which is
# the normal case for an operator running this from their laptop.
SA_KEY_PATH = os.getenv("VERTEX_SA_KEY_PATH", "") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")


def _store() -> LicenceStore:
    credentials = None
    project = None
    if SA_KEY_PATH and os.path.exists(SA_KEY_PATH):
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            SA_KEY_PATH, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        project = credentials.project_id
    return LicenceStore(
        bucket=STORAGE_BUCKET, credentials=credentials, project=project, prefix=STORAGE_PREFIX
    )


def cmd_mint(args: argparse.Namespace) -> int:
    key, licence = _store().mint(
        name=args.name,
        email=args.email,
        valid_days=args.days,
        daily_quota=args.daily_quota,
        total_quota=args.total_quota,
        max_devices=args.devices,
        notes=args.notes,
    )
    print()
    print("  Licence minted")
    print("  " + "-" * 46)
    print(f"  Customer     : {licence.name}")
    print(f"  Email        : {licence.email or '-'}")
    print(f"  Expires      : {licence.expires_at or 'never'}")
    print(f"  Daily quota  : {licence.daily_quota or 'unlimited'}")
    print(f"  Total quota  : {licence.total_quota or 'unlimited'}")
    print(f"  Max devices  : {licence.max_devices or 'unlimited'}")
    print()
    print(f"  KEY: {key}")
    print()
    print("  Shown once. Send it to the customer now -- it is stored only as a")
    print("  hash and cannot be displayed again.")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = _store().list_all()
    if not rows:
        print("No licences yet. Mint one with:  python admin.py mint --name 'Client'")
        return 0

    store = _store()
    print()
    print(f"  {'KEY':<12} {'CUSTOMER':<22} {'EXPIRES':<12} {'DAILY':>11}  STATUS")
    print("  " + "-" * 74)
    for doc_id, lic in sorted(rows, key=lambda r: r[1].created_at, reverse=True):
        usage = store.usage_snapshot(doc_id)
        remaining = lic.days_remaining()
        if lic.revoked:
            status = "REVOKED"
        elif remaining is not None and remaining < 0:
            status = "EXPIRED"
        elif remaining is not None and remaining <= 14:
            status = f"{remaining}d left"
        else:
            status = "active"
        daily = f"{usage['daily_used']}/{lic.daily_quota or '∞'}"
        print(
            f"  {lic.key_prefix:<12} {lic.name[:22]:<22} "
            f"{lic.expires_at or 'never':<12} {daily:>11}  {status}"
        )
    print()
    if args.hashes:
        print("  Document ids (for revoke/show when you do not have the key):")
        for doc_id, lic in rows:
            print(f"    {lic.key_prefix}  {doc_id}")
        print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store()
    found = store.get(args.key)
    if found is None:
        print(f"No licence matching '{key_prefix(args.key)}'.", file=sys.stderr)
        return 1
    doc_id, lic = found
    usage = store.usage_snapshot(doc_id)
    print()
    for label, value in [
        ("Key", lic.key_prefix),
        ("Customer", lic.name),
        ("Email", lic.email or "-"),
        ("Created", lic.created_at),
        ("Expires", lic.expires_at or "never"),
        ("Days left", lic.days_remaining() if lic.days_remaining() is not None else "n/a"),
        ("Revoked", f"yes ({lic.revoked_reason})" if lic.revoked else "no"),
        ("Daily quota", lic.daily_quota or "unlimited"),
        ("Used today", usage["daily_used"]),
        ("Total quota", lic.total_quota or "unlimited"),
        ("Used total", usage["total_used"]),
        ("Max devices", lic.max_devices or "unlimited"),
        ("Devices seen", len(lic.devices)),
        ("Notes", lic.notes or "-"),
        ("Object id", doc_id),
    ]:
        print(f"  {label:<14}: {value}")
    print()
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    if _store().revoke(args.key, reason=args.reason):
        print(f"Revoked {key_prefix(args.key)}. It stops working on the next request.")
        return 0
    print(f"No licence matching '{key_prefix(args.key)}'.", file=sys.stderr)
    return 1


def cmd_reinstate(args: argparse.Namespace) -> int:
    if _store().reinstate(args.key):
        print(f"Reinstated {key_prefix(args.key)}.")
        return 0
    print(f"No licence matching '{key_prefix(args.key)}'.", file=sys.stderr)
    return 1


def cmd_reset_devices(args: argparse.Namespace) -> int:
    """Clears device binding so a customer can move to a new machine."""
    if _store().update_fields(args.key, devices=[]):
        print(f"Cleared device binding on {key_prefix(args.key)}.")
        return 0
    print(f"No licence matching '{key_prefix(args.key)}'.", file=sys.stderr)
    return 1


def cmd_set(args: argparse.Namespace) -> int:
    fields = {}
    if args.days is not None:
        import datetime as dt

        fields["expires_at"] = (dt.date.today() + dt.timedelta(days=args.days)).isoformat()
    if args.daily_quota is not None:
        fields["daily_quota"] = args.daily_quota
    if args.total_quota is not None:
        fields["total_quota"] = args.total_quota
    if args.devices is not None:
        fields["max_devices"] = args.devices
    if not fields:
        print("Nothing to change. Pass at least one of --days/--daily-quota/--total-quota/--devices.")
        return 1
    if _store().update_fields(args.key, **fields):
        print(f"Updated {key_prefix(args.key)}: {fields}")
        return 0
    print(f"No licence matching '{key_prefix(args.key)}'.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="admin.py", description=__doc__.split("\n")[1])
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("mint", help="create a new licence key")
    m.add_argument("--name", required=True, help="customer name, shown in listings")
    m.add_argument("--email", default="", help="customer email, for your records")
    m.add_argument("--days", type=int, default=365, help="validity in days (0 = never expires)")
    m.add_argument("--daily-quota", type=int, default=400, help="model calls per day (0 = unlimited)")
    m.add_argument("--total-quota", type=int, default=0, help="lifetime cap (0 = unlimited)")
    m.add_argument("--devices", type=int, default=2, help="machines allowed (0 = unlimited)")
    m.add_argument("--notes", default="", help="free-text note")
    m.set_defaults(func=cmd_mint)

    l = sub.add_parser("list", help="list every licence and today's usage")
    l.add_argument("--hashes", action="store_true", help="also print document ids")
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="full detail for one licence")
    s.add_argument("key", help="the licence key, or its document id")
    s.set_defaults(func=cmd_show)

    r = sub.add_parser("revoke", help="disable a licence immediately")
    r.add_argument("key")
    r.add_argument("--reason", default="", help="message the customer will see")
    r.set_defaults(func=cmd_revoke)

    ri = sub.add_parser("reinstate", help="undo a revoke")
    ri.add_argument("key")
    ri.set_defaults(func=cmd_reinstate)

    rd = sub.add_parser("reset-devices", help="clear device binding so the customer can move machines")
    rd.add_argument("key")
    rd.set_defaults(func=cmd_reset_devices)

    st = sub.add_parser("set", help="change quotas or extend validity")
    st.add_argument("key")
    st.add_argument("--days", type=int, default=None, help="reset expiry to N days from today")
    st.add_argument("--daily-quota", type=int, default=None)
    st.add_argument("--total-quota", type=int, default=None)
    st.add_argument("--devices", type=int, default=None)
    st.set_defaults(func=cmd_set)

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(args.func(args))
