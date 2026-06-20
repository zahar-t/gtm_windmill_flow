"""intake/apify_cold.py — Apify LinkedIn *search* → cold ICP leads (batched).

The 8am cold-lead-generation pipe runs three discovery sources in parallel:
Exa neural search (intake/web_search.py), this Apify LinkedIn search, and a
drain of any visitor events the real-time webhook missed. This node is the
LinkedIn half — it runs an Apify LinkedIn *search* actor over the ICP query and
maps each hit to the canonical lead dict (no email yet; the downstream email
waterfall makes cold leads contactable).

Contrast with intake/linkedin.py, which is a profile *enricher* (scrapes a known
profile URL). This is *discovery*: it finds new people/companies from a query.

Smoke-safe: returns [] unless BOTH APIFY_API_TOKEN and
APIFY_LINKEDIN_SEARCH_ACTOR_ID are set — the search actor is optional, so without
it the Exa source still feeds the cold pipe. Never raises.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, http, log

_APIFY_BASE = "https://api.apify.com/v2/acts"


def _lead_from_hit(raw: dict) -> dict[str, Any]:
    """Map one Apify LinkedIn search hit to a canonical lead dict.

    Actor output shapes vary; pull from the common field aliases and leave
    anything missing as None — enrichment fills the gaps downstream.
    """
    name = (
        raw.get("fullName")
        or raw.get("name")
        or f"{raw.get('firstName', '')} {raw.get('lastName', '')}".strip()
        or None
    )
    return {
        "name": name or None,
        "company": raw.get("companyName") or raw.get("company") or None,
        "company_url": raw.get("companyWebsite") or raw.get("companyUrl") or None,
        "domain": raw.get("companyDomain") or None,
        "title": raw.get("jobTitle") or raw.get("headline") or raw.get("title") or None,
        "email": None,                       # waterfall fills this downstream
        "linkedin_url": (
            raw.get("profileUrl") or raw.get("url") or raw.get("linkedinUrl") or None
        ),
        "country": raw.get("location") or raw.get("country") or None,
        "source": "linkedin_search",
        "signal": None,
        "_errors": [],
    }


def _search(query: str, n: int) -> list[dict[str, Any]]:
    """Run the Apify LinkedIn search actor; [] on any failure / missing config."""
    if not config.APIFY_API_TOKEN or not config.APIFY_LINKEDIN_SEARCH_ACTOR_ID:
        return []
    if n <= 0:
        return []
    endpoint = f"{_APIFY_BASE}/{config.APIFY_LINKEDIN_SEARCH_ACTOR_ID}/run-sync-get-dataset-items"
    result = http.post_json(
        endpoint,
        params={"token": config.APIFY_API_TOKEN, "timeout": 60},
        # Field aliases cover common LinkedIn-search actor input schemas.
        json={"searchQuery": query, "query": query, "maxItems": n, "limit": n},
        timeout=60.0,
        retries=0,
    )
    if not result:
        return []
    items = result if isinstance(result, list) else (result.get("items") or [])
    if not isinstance(items, list):
        return []
    return [h for h in items[:n] if isinstance(h, dict)]


def main(query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Discover cold ICP leads via Apify LinkedIn search.

    Parameters
    ----------
    query:  search query; falls back to config.ICP_QUERY when empty.
    limit:  max leads to return (default 10).

    Returns
    -------
    list[dict]
        Canonical lead dicts with source="linkedin_search". [] without an
        APIFY token + search actor id (smoke path).
    """
    q = query or config.ICP_QUERY
    n = max(0, int(limit or 0))

    leads: list[dict[str, Any]] = []
    try:
        for hit in _search(q, n):
            try:
                leads.append(_lead_from_hit(hit))
            except Exception as exc:
                leads.append({
                    "name": None, "company": None, "company_url": None, "domain": None,
                    "title": None, "email": None, "linkedin_url": None, "country": None,
                    "source": "linkedin_search", "signal": None,
                    "_errors": [f"apify_cold: {exc}"],
                })
    except Exception:
        leads = []

    try:
        log.log_stage(
            "intake/apify_cold",
            {"found": len(leads), "actor": bool(config.APIFY_LINKEDIN_SEARCH_ACTOR_ID)},
        )
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    import json

    actor = "set" if config.APIFY_LINKEDIN_SEARCH_ACTOR_ID else "unset"
    print(f"=== apify_cold.py smoke (keyless; search_actor={actor}) ===")
    out = main("B2B SaaS Series A 50-500 employees", limit=5)
    print(json.dumps(out, default=str)[:600])
    assert out == [], "keyless / no search actor → no discovery"
    print("PASS: no Apify search actor configured → returned [] without raising")
