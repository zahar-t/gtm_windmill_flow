"""intake/web_search.py — Exa ICP discovery → new company/lead dicts.

Discovers target companies matching the ICP query via Exa neural search and
maps each result to the canonical lead-dict contract. No Supabase interaction
at this stage — dedup happens downstream in crm/dedup.py.

Smoke-safe: if EXA_API_KEY is absent, returns [] without any network call.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, exa, log


def main(icp_query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Discover companies via Exa and return canonical lead dicts.

    Parameters
    ----------
    icp_query:
        ICP search query. Falls back to config.ICP_QUERY when empty.
    limit:
        Maximum number of companies to return (default 10).

    Returns
    -------
    list[dict]
        Canonical lead dicts with source="exa_web_search". Empty list when
        EXA_API_KEY is unset (smoke path).
    """
    q = icp_query or config.ICP_QUERY

    results = exa.discover_companies(q, limit)

    leads: list[dict[str, Any]] = []
    for r in results:
        try:
            lead: dict[str, Any] = {
                "name": None,          # person name unknown at intake
                "company": r.get("name"),
                "company_url": r.get("url"),
                "domain": r.get("domain"),
                "title": None,
                "email": None,
                "linkedin_url": None,
                "source": "exa_web_search",
                "signal": None,
                "_errors": [],
            }
            leads.append(lead)
        except Exception as exc:
            leads.append({
                "name": None,
                "company": None,
                "company_url": None,
                "domain": None,
                "title": None,
                "email": None,
                "linkedin_url": None,
                "source": "exa_web_search",
                "signal": None,
                "_errors": [str(exc)],
            })

    try:
        log.log_stage("intake/web_search", {"found": len(leads)})
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    print(json.dumps(main(), default=str)[:2000])
