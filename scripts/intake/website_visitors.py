"""intake/website_visitors.py — Drain unprocessed website_visitor_events rows.

Reads RB2B webhook rows from Supabase `website_visitor_events` where
processed=false, maps each to a canonical lead dict, then marks them
processed=true. Does NOT build a webhook server — Windmill wires the
webhook; we only drain.

Smoke-safe: no Supabase creds → returns [] without any DB call.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, log

# Guarded import so smoke works without creds
try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False


def main(limit: int = 100) -> list[dict[str, Any]]:
    """Drain unprocessed website visitor events and return lead dicts.

    Parameters
    ----------
    limit:
        Maximum number of rows to drain per run (default 100).

    Returns
    -------
    list[dict]
        Canonical lead dicts with source="website_visitor". Empty list when
        Supabase creds are absent (smoke path) or no unprocessed rows exist.
    """
    # Smoke path: no creds → no-op
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        try:
            log.log_stage("intake/website_visitors", {"drained": 0})
        except Exception:
            pass
        return []

    if not _SUPABASE_OK or _supabase is None:
        try:
            log.log_stage("intake/website_visitors", {"drained": 0})
        except Exception:
            pass
        return []

    leads: list[dict[str, Any]] = []

    try:
        rows = _supabase.select(
            "website_visitor_events",
            {"processed": "eq.false"},
            order="received_at.asc",
            limit=limit,
        )
    except Exception:
        try:
            log.log_stage("intake/website_visitors", {"drained": 0})
        except Exception:
            pass
        return []

    for row in rows:
        try:
            lead: dict[str, Any] = {
                "email": row.get("email"),
                "name": row.get("name"),
                "company": row.get("company"),
                "company_url": row.get("company_url"),
                "linkedin_url": row.get("linkedin_url"),
                "title": None,
                "source": "website_visitor",
                "signal": None,
                "_errors": [],
            }
            leads.append(lead)

            # Mark row as processed
            row_id = row.get("id")
            if row_id is not None:
                try:
                    _supabase.update(
                        "website_visitor_events",
                        {"id": f"eq.{row_id}"},
                        {"processed": True},
                    )
                except Exception as exc:
                    lead["_errors"].append(f"mark_processed: {exc}")

        except Exception as exc:
            leads.append({
                "email": None,
                "name": None,
                "company": None,
                "company_url": None,
                "linkedin_url": None,
                "title": None,
                "source": "website_visitor",
                "signal": None,
                "_errors": [str(exc)],
            })

    try:
        log.log_stage("intake/website_visitors", {"drained": len(leads)})
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    print(json.dumps(main(), default=str)[:2000])
