"""intake/linkedin_visitors.py — Drain LinkedIn company-page visitor events.

One of the three Stage-1 intake feeds (alongside web_search and
website_visitors). This is the LinkedIn *page visitor* feed: people who viewed
the company's LinkedIn page, surfaced as warm top-of-funnel leads.

Windmill owns ingestion — a LinkedIn page-visitor source (Apify LinkedIn
page-visitor actor, or a LinkedIn "who viewed your page" export job) writes raw
events into the Supabase `linkedin_visitor_events` table. This stage only
DRAINS unprocessed rows, maps each to a canonical lead dict with
source="linkedin_visitor", then marks them processed=true. It never builds a
scraper or webhook server itself — same drain-only contract as
intake/website_visitors.py.

Smoke-safe: no Supabase creds → returns [] without any DB call. Never raises.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, log, node

# Guarded import so smoke works without creds
try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False


def main(limit: int = 100) -> list[dict[str, Any]]:
    """Drain unprocessed LinkedIn page-visitor events into lead dicts.

    Parameters
    ----------
    limit:
        Maximum number of rows to drain per run (default 100).

    Returns
    -------
    list[dict]
        Canonical lead dicts with source="linkedin_visitor". Empty list when
        Supabase creds are absent (smoke path) or no unprocessed rows exist.
    """
    # Smoke path: no creds → no-op
    if not config.SUPABASE_URL or not config.SUPABASE_KEY or not _SUPABASE_OK or _supabase is None:
        try:
            log.log_stage("intake/linkedin_visitors", {"drained": 0})
        except Exception:
            pass
        return []

    try:
        rows = _supabase.select(
            "linkedin_visitor_events",
            {"processed": "eq.false"},
            order="received_at.asc",
            limit=limit,
        )
    except Exception:
        try:
            log.log_stage("intake/linkedin_visitors", {"drained": 0})
        except Exception:
            pass
        return []

    leads: list[dict[str, Any]] = []

    def _emit(lead: dict[str, Any]) -> None:
        if node.has_identity(lead):
            leads.append(lead)
        else:
            node.dead_letter("intake/linkedin_visitors", node.NO_IDENTITY, lead,
                             detail="no email/linkedin/domain")
            node.record_run("intake/linkedin_visitors", lead, node.STATUS_QUARANTINED)

    for row in rows:
        try:
            lead: dict[str, Any] = {
                "email": row.get("email"),
                "name": row.get("name"),
                "company": row.get("company"),
                "company_url": row.get("company_url"),
                # LinkedIn profile of the visitor — primary identity for this feed
                "linkedin_url": row.get("linkedin_url") or row.get("visitor_linkedin_url"),
                "title": row.get("title"),
                "country": row.get("country"),
                "source": "linkedin_visitor",
                "signal": None,
                "_errors": [],
            }

            # Mark row as processed BEFORE emit — a quarantined visitor must
            # still be marked processed (payload captured in dead_letter;
            # re-draining would duplicate it).
            row_id = row.get("id")
            if row_id is not None:
                try:
                    _supabase.update(
                        "linkedin_visitor_events",
                        {"id": f"eq.{row_id}"},
                        {"processed": True},
                    )
                except Exception as exc:
                    lead["_errors"].append(f"mark_processed: {exc}")

            _emit(lead)

        except Exception as exc:
            _emit({
                "email": None,
                "name": None,
                "company": None,
                "company_url": None,
                "linkedin_url": None,
                "title": None,
                "country": None,
                "source": "linkedin_visitor",
                "signal": None,
                "_errors": [str(exc)],
            })

    try:
        log.log_stage("intake/linkedin_visitors", {"drained": len(leads)})
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    # Keyless smoke: no Supabase creds expected → returns [] without raising.
    print("linkedin_visitors.py smoke (keyless — no SUPABASE creds expected):")
    out = main()
    print(json.dumps(out, default=str)[:2000])
    assert isinstance(out, list), "main() must return a list"
    print(f"PASS: main() returned a list ({len(out)} leads) without raising")
