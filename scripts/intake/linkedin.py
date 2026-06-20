"""intake/linkedin.py — Apify LinkedIn scrape with Supabase cache + Exa fallback.

For each lead with a company_url or linkedin_url:
  1. Check Supabase linkedin_cache (168h TTL enforced in code).
  2. Cache miss/expired + APIFY_API_TOKEN present → live Apify (15s hard timeout, retries=0).
  3. Apify fail/timeout/empty OR no token → Exa fallback via exa.find_recent_activity.

Smoke-safe:
  - No Supabase creds → cache treated as miss (no DB calls).
  - No Apify token → goes straight to Exa fallback.
  - No Exa key → empty payload.
  - Never raises regardless of missing credentials.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from scripts.common import config, exa, log
from scripts.common import http

# Attempt Supabase import — guarded so smoke works without creds
try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False

_CACHE_TTL_HOURS = 168
_APIFY_BASE = "https://api.apify.com/v2/acts"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _person_from_url(url: str) -> str:
    """Best-effort: extract person name slug from a LinkedIn profile URL."""
    try:
        path = urlparse(url).path.rstrip("/")
        slug = path.split("/")[-1]
        return slug.replace("-", " ").title()
    except Exception:
        return ""


def _normalize(raw: dict) -> dict:
    """Normalize Apify actor response variants into a consistent payload.

    Ported and adapted from vendor/opensales/backend/app/services/apify.py.
    """
    return {
        "headline": raw.get("headline") or raw.get("title") or "",
        "about": (
            raw.get("about")
            or raw.get("summary")
            or raw.get("description")
            or ""
        ),
        "experience": raw.get("experience") or raw.get("positions") or [],
        "recent_posts": (
            raw.get("posts")
            or raw.get("recent_posts")
            or raw.get("activity")
            or []
        )[:5],
        "name": (
            raw.get("name")
            or raw.get("fullName")
            or f"{raw.get('firstName', '')} {raw.get('lastName', '')}".strip()
        ),
        "location": raw.get("location") or raw.get("addressWithCountry"),
    }


def _cache_get(key_url: str) -> dict | None:
    """Return cached linkedin_cache payload for key_url (the scraped profile URL)
    if present and within the 168h TTL. The DB column is named company_url for
    legacy reasons but stores whatever URL was scraped (profile URL preferred)."""
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        return None
    if not _SUPABASE_OK or _supabase is None:
        return None
    try:
        rows = _supabase.select(
            "linkedin_cache",
            {"company_url": f"eq.{key_url}"},
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        cached_at_str = row.get("cached_at") or ""
        if not cached_at_str:
            return None
        # Parse and enforce TTL in code
        try:
            cached_at = datetime.fromisoformat(cached_at_str)
        except ValueError:
            return None
        # Ensure timezone-aware comparison
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - cached_at
        if age > timedelta(hours=_CACHE_TTL_HOURS):
            return None
        return row.get("payload") or {}
    except Exception:
        return None


def _cache_put(key_url: str, payload: dict) -> None:
    """Upsert payload into linkedin_cache, keyed by key_url (scraped profile URL)."""
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        return
    if not _SUPABASE_OK or _supabase is None:
        return
    try:
        _supabase.upsert(
            "linkedin_cache",
            {
                "company_url": key_url,
                "payload": payload,
                "cached_at": _now_iso(),
            },
            on_conflict="company_url",
        )
    except Exception:
        pass


def _apify_live(url: str) -> dict | None:
    """Call Apify run-sync endpoint with 15s hard timeout, retries=0.

    Returns normalized payload dict on success, None on any failure.
    """
    if not config.APIFY_API_TOKEN:
        return None
    actor_id = config.APIFY_LINKEDIN_ACTOR_ID
    endpoint = f"{_APIFY_BASE}/{actor_id}/run-sync-get-dataset-items"
    result = http.post_json(
        endpoint,
        params={"token": config.APIFY_API_TOKEN, "timeout": 15},
        json={"profileUrls": [url], "urls": [url]},
        timeout=15.0,
        retries=0,
    )
    if not result:
        return None
    items = result if isinstance(result, list) else result.get("items") or []
    if not items:
        return None
    payload = _normalize(items[0])
    payload["source"] = "apify_live"
    return payload


def _exa_fallback(lead: dict, url: str) -> dict:
    """Use Exa find_recent_activity as fallback when Apify unavailable."""
    person_name = lead.get("name") or _person_from_url(url)
    company_name = lead.get("company") or ""
    activities = exa.find_recent_activity(person_name, company_name)
    return {
        "source": "exa_fallback",
        "name": person_name,
        "headline": "",
        "about": "",
        "experience": [],
        "recent_posts": [
            {
                "title": a.get("title"),
                "snippet": a.get("snippet"),
                "url": a.get("url"),
            }
            for a in (activities or [])
        ],
        "location": None,
    }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def main(leads: list[dict] | None = None) -> list[dict]:
    """Enrich leads with LinkedIn data via cache → Apify → Exa fallback.

    Parameters
    ----------
    leads:
        List of canonical lead dicts. None → returns [].

    Returns
    -------
    list[dict]
        Same list with `title`, `_linkedin`, `enriched_at` populated where
        possible. Per-lead errors are appended to lead["_errors"].
    """
    if leads is None:
        try:
            log.log_stage(
                "intake/linkedin",
                {"processed": 0, "cache_hits": 0, "apify": 0, "exa_fallback": 0},
            )
        except Exception:
            pass
        return []

    processed = 0
    cache_hits = 0
    apify_count = 0
    exa_fallback_count = 0
    now_iso = _now_iso()

    for lead in leads:
        if "_errors" not in lead:
            lead["_errors"] = []

        # Scrape + cache by the PERSON's profile URL — this is a profile scraper.
        # Preferring company_url (the old order) was a bug: it fed the company
        # page to a profile actor AND keyed the cache per-company, so every lead
        # at the same company collided on one cached "profile". Person URL first.
        url = lead.get("linkedin_url") or lead.get("company_url")
        if not url:
            continue

        try:
            payload: dict | None = None

            # 1. Cache check
            cached = _cache_get(url)
            if cached:
                payload = cached
                cache_hits += 1
            else:
                # 2. Live Apify (15s hard timeout, retries=0)
                apify_result = _apify_live(url)
                if apify_result:
                    payload = apify_result
                    apify_count += 1
                    # Store in cache for future runs
                    _cache_put(url, payload)
                else:
                    # 3. Exa fallback
                    payload = _exa_fallback(lead, url)
                    exa_fallback_count += 1
                    # Prefer NOT caching Exa fallback results per spec

            # Merge payload into lead
            if payload:
                # Set title from headline if not already present
                if not lead.get("title") and payload.get("headline"):
                    lead["title"] = payload["headline"]
                lead["_linkedin"] = payload
                lead["enriched_at"] = now_iso

            processed += 1

        except Exception as exc:
            lead["_errors"].append(f"linkedin: {exc}")
            processed += 1

    try:
        log.log_stage(
            "intake/linkedin",
            {
                "processed": processed,
                "cache_hits": cache_hits,
                "apify": apify_count,
                "exa_fallback": exa_fallback_count,
            },
        )
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    fixture = [
        {
            "email": None,
            "name": "Pat Doe",
            "company": "Acme Cloud",
            "company_url": "https://acme.example",
            "domain": "acme.example",
            "title": None,
            "linkedin_url": None,
            "source": "exa_web_search",
            "signal": None,
            "_errors": [],
        }
    ]
    print(json.dumps(main(fixture), default=str)[:2000])
