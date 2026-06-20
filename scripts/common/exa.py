"""Exa neural search — synchronous adaptation of vendor/opensales exa.py.

All functions return [] when EXA_API_KEY is missing or on any error.
Uses common/http.post_json so no raw httpx here.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from scripts.common import config
from scripts.common.http import post_json

EXA_BASE = "https://api.exa.ai"


# ---------------------------------------------------------------------------
# Internal helpers (ported verbatim from vendor exa.py)
# ---------------------------------------------------------------------------

def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).netloc
        return host.lstrip("www.") if host else None
    except Exception:
        return None


def _company_name_from_title(title: str) -> str:
    """Best-effort: 'Velocity AI raises $4M seed - TechCrunch' -> 'Velocity AI'."""
    if not title:
        return "Unknown"
    parts = re.split(r"\s[-—|:·]\s", title)
    name = parts[0].strip()
    name = re.split(
        r"\s+(raises|announces|launches|secures|closes|appoints|founder|CEO)",
        name,
        flags=re.I,
    )[0].strip()
    return name or title[:40]


def _exa_headers() -> dict[str, str]:
    return {
        "x-api-key": config.EXA_API_KEY,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_companies(icp_query: str, n: int = 10) -> list[dict[str, Any]]:
    """Find target companies matching the ICP.

    POST /search — returns [{name, title, url, domain, snippet, published_date}].
    Returns [] when EXA_API_KEY is missing or on any network/parse error.
    """
    if not config.EXA_API_KEY:
        return []

    payload = {
        "query": icp_query,
        "type": "auto",
        "numResults": n,
        "useAutoprompt": True,
        "contents": {
            "text": {"maxCharacters": 600},
            "summary": True,
        },
    }

    data = post_json(
        f"{EXA_BASE}/search",
        headers=_exa_headers(),
        json=payload,
    )
    if data is None:
        return []

    out: list[dict[str, Any]] = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for item in results:
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippet = (
            item.get("summary")
            or (item.get("text") or "")[:300]
            or (item.get("highlights") or [""])[0]
        )
        out.append(
            {
                "name": _company_name_from_title(title),
                "title": title,
                "url": url,
                "domain": _domain(url),
                "snippet": snippet.strip() if isinstance(snippet, str) else "",
                "published_date": item.get("publishedDate"),
            }
        )
    return out


def find_recent_activity(
    name: str,
    company: str = "",
    n: int = 5,
) -> list[dict[str, Any]]:
    """Personalization signal: recent posts/talks/articles by a person.

    Returns [{title, url, snippet, published_date}].
    """
    if not config.EXA_API_KEY:
        return []

    query = f"{name} {company} recent post OR talk OR interview OR blog".strip()

    payload = {
        "query": query,
        "type": "auto",
        "numResults": n,
        "useAutoprompt": True,
        "contents": {"text": {"maxCharacters": 500}, "summary": True},
    }

    data = post_json(
        f"{EXA_BASE}/search",
        headers=_exa_headers(),
        json=payload,
    )
    if data is None:
        return []

    out: list[dict[str, Any]] = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for item in results:
        snippet = item.get("summary") or item.get("text") or ""
        out.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": snippet[:400].strip() if isinstance(snippet, str) else "",
                "published_date": item.get("publishedDate"),
            }
        )
    return out


def find_signals(company: str, n: int = 5) -> list[dict[str, Any]]:
    """Recent growth/funding/launch signals for a company (last 7 days).

    Returns [{type, title, url, snippet, published_date}].
    type is one of: funding | hiring | launch | other
    """
    if not config.EXA_API_KEY:
        return []

    start_date = (date.today() - timedelta(days=7)).isoformat()
    query = (
        f"{company} funding OR raises OR hiring OR launches OR new product"
    )

    payload = {
        "query": query,
        "type": "auto",
        "numResults": n,
        "useAutoprompt": True,
        "startPublishedDate": start_date,
        "contents": {"text": {"maxCharacters": 400}, "summary": True},
    }

    data = post_json(
        f"{EXA_BASE}/search",
        headers=_exa_headers(),
        json=payload,
    )
    if data is None:
        return []

    out: list[dict[str, Any]] = []
    results = data.get("results", []) if isinstance(data, dict) else []
    for item in results:
        title = item.get("title") or ""
        snippet_raw = item.get("summary") or item.get("text") or ""
        snippet = snippet_raw[:400].strip() if isinstance(snippet_raw, str) else ""
        combined = f"{title} {snippet}".lower()

        if re.search(r"raises?|seed|series [a-z]|funding|funded|investment|round", combined):
            signal_type = "funding"
        elif re.search(r"hiring|hires?|headcount|jobs?|recruit|open role", combined):
            signal_type = "hiring"
        elif re.search(r"launch(?:es|ed)?|unveils?|new product|announces?", combined):
            signal_type = "launch"
        else:
            signal_type = "other"

        out.append(
            {
                "type": signal_type,
                "title": title,
                "url": item.get("url") or "",
                "snippet": snippet,
                "published_date": item.get("publishedDate"),
            }
        )
    return out


if __name__ == "__main__":
    import json

    print("exa.py smoke (no key expected — all return []):")
    print(f"  EXA_API_KEY present: {bool(config.EXA_API_KEY)}")
    print(f"  discover_companies:  {json.dumps(discover_companies('B2B SaaS'))}")
    print(f"  find_recent_activity: {json.dumps(find_recent_activity('Pat Doe', 'Acme Cloud'))}")
    print(f"  find_signals:        {json.dumps(find_signals('Acme Cloud'))}")
