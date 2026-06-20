"""crm/dedup.py — Suppress leads we must not contact.

Runs BEFORE enrich/score/personalize and marks lead["_skip"]=True for two
reasons:
  1. RECENCY — contacted < 30 days ago (don't double-tap an active sequence).
  2. SUPPRESSION (permanent) — the lead's terminal outcome is in
     config.SUPPRESS_OUTCOMES (reply / bounce / unsubscribe). Recency alone is
     a bug: 31 days after someone REPLIES or BOUNCES, a pure last_contacted
     check would happily email them again. Outcome-based suppression closes
     that — a reply/bounce/unsub is forever, not 30 days.

Keyless/no-creds → pass all leads through with _skip=False (no DB call).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scripts.common import config, log

_30_DAYS = timedelta(days=30)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO 8601 string into a tz-aware UTC datetime, or None."""
    if not ts:
        return None
    try:
        # Handle both Z-suffix and +00:00
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def main(leads: list[dict] | None = None) -> list[dict]:
    """Check each lead (by email) against Supabase leads.last_contacted_at.

    Marks lead["_skip"] = True when the lead was contacted less than 30 days
    ago. Sets _skip = False for all others (new, stale, or emailless).

    Returns the full list (unchanged except for _skip and optionally id).
    No creds → _skip=False for all, no network call.
    """
    if leads is None:
        leads = []

    skipped = 0
    fresh = 0

    no_creds = not (config.SUPABASE_URL and config.SUPABASE_KEY)

    for lead in leads:
        email = lead.get("email")

        # No CRM or no email to match on → pass through here. Safe only because the
        # SEND node refuses to send when Instantly is live but the CRM is
        # unreachable, so suppression can never be bypassed on a real send.
        if no_creds or not email:
            lead["_skip"] = False
            fresh += 1
            continue

        try:
            from scripts.common import supabase

            rows = supabase.select(
                "leads",
                {"email": f"eq.{email}"},
                columns="id,email,last_contacted_at,stage,outcome",
                limit=1,
            )
        except Exception as exc:
            # Creds present but the lookup FAILED — we cannot verify suppression.
            # FAIL CLOSED: never contact an unverifiable lead (Risk #2).
            lead.setdefault("_errors", []).append(f"dedup error: {exc}")
            lead["_skip"] = True
            lead["_skip_reason"] = "dedup_unverified:db_error"
            skipped += 1
            continue

        if rows:
            row = rows[0]
            # Carry the DB id forward regardless of skip decision.
            if row.get("id"):
                lead["id"] = row["id"]

            # 1. Permanent suppression on terminal outcome (reply/bounce/unsub).
            outcome = (row.get("outcome") or "").lower()
            if outcome in config.SUPPRESS_OUTCOMES:
                lead["_skip"] = True
                lead["suppressed"] = True          # contract field; send re-checks it
                lead["_skip_reason"] = f"suppressed:{outcome}"
                skipped += 1
                continue

            # 2. Recency: don't double-tap an active sequence.
            last_contacted_at = _parse_iso(row.get("last_contacted_at"))
            if last_contacted_at and (_now_utc() - last_contacted_at) < _30_DAYS:
                lead["_skip"] = True
                lead["_skip_reason"] = "recency:<30d"
                skipped += 1
                continue

        lead["_skip"] = False
        fresh += 1

    try:
        log.log_stage("crm/dedup", {"skipped": skipped, "fresh": fresh})
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json
    from datetime import timedelta

    now_iso = datetime.now(timezone.utc).isoformat()
    recent_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()

    # Fixture: three leads — no email (pass through), recently contacted (skip), old contact (keep)
    fixture_leads = [
        {
            "email": None,
            "name": "Alex Founder",
            "company": "Acme Cloud",
            "company_url": "https://acme.example",
            "source": "exa_web_search",
        },
        {
            # Simulate a lead that WOULD be skipped if DB had recent contact
            # In keyless mode _skip=False since no DB is consulted
            "email": "pat@acme.example",
            "name": "Pat Doe",
            "company": "Acme Cloud",
            "company_url": "https://acme.example",
            "source": "exa_web_search",
            # Pretend DB would return this (we inject expected result manually below)
            "_test_last_contacted_at": recent_iso,
        },
        {
            "email": "sam@widget.example",
            "name": "Sam Smith",
            "company": "Widget Corp",
            "company_url": "https://widget.example",
            "source": "exa_web_search",
            "_test_last_contacted_at": old_iso,
        },
    ]

    print("=== crm/dedup.py smoke (keyless — no Supabase creds) ===")
    print("Expected: all leads get _skip=False (no DB to query)")
    result = main(fixture_leads)
    print(json.dumps(result, default=str, indent=2)[:2000])

    print("\n=== Manual dedup logic demonstration ===")
    now = _now_utc()

    def _demo_would_skip(last_contacted_str: str) -> bool:
        lc = _parse_iso(last_contacted_str)
        if lc and (now - lc) < _30_DAYS:
            return True
        return False

    print(f"  Lead contacted 5 days ago  → would _skip={_demo_would_skip(recent_iso)}")
    print(f"  Lead contacted 45 days ago → would _skip={_demo_would_skip(old_iso)}")
    print(f"  No last_contacted_at       → would _skip={_demo_would_skip('')}")
