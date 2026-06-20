"""Email waterfall enrichment — Hunter → Apollo → PDL, stop on first hit.

For each lead that lacks an email but has a name and domain/company_url,
tries providers in priority order. Missing provider key → skip that provider.
Never raises; per-lead errors go to lead["_errors"].
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from scripts.common import config, log
from scripts.common.http import get_json, post_json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_domain(lead: dict) -> str | None:
    """Return bare domain from lead['domain'] or lead['company_url']."""
    domain = lead.get("domain")
    if domain:
        return domain
    url = lead.get("company_url") or ""
    if not url:
        return None
    try:
        host = urlparse(url).netloc
        return host.lstrip("www.") if host else None
    except Exception:
        return None


def _split_name(name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles single-word names."""
    parts = name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _try_hunter(first: str, last: str, domain: str) -> str | None:
    """Hunter email-finder. Returns email string or None."""
    if not config.HUNTER_API_KEY:
        return None
    data = get_json(
        "https://api.hunter.io/v2/email-finder",
        params={
            "domain": domain,
            "first_name": first,
            "last_name": last,
            "api_key": config.HUNTER_API_KEY,
        },
    )
    if not isinstance(data, dict):
        return None
    return data.get("data", {}).get("email") or None


def _try_apollo(first: str, last: str, domain: str) -> str | None:
    """Apollo people match. Returns email string or None."""
    if not config.APOLLO_API_KEY:
        return None
    data = post_json(
        "https://api.apollo.io/v1/people/match",
        headers={"X-Api-Key": config.APOLLO_API_KEY},
        json={"first_name": first, "last_name": last, "domain": domain},
    )
    if not isinstance(data, dict):
        return None
    person = data.get("person") or {}
    return person.get("email") or None


def _try_pdl(first: str, last: str, domain: str) -> str | None:
    """People Data Labs person enrich. Returns email string or None."""
    if not config.PDL_API_KEY:
        return None
    data = get_json(
        "https://api.peopledatalabs.com/v5/person/enrich",
        headers={"X-Api-Key": config.PDL_API_KEY},
        params={"first_name": first, "last_name": last, "company": domain},
    )
    if not isinstance(data, dict):
        return None
    # Prefer work_email; fall back to first address in emails list
    email = data.get("work_email")
    if not email:
        emails = data.get("emails") or []
        if emails and isinstance(emails[0], dict):
            email = emails[0].get("address")
    return email or None


def main(leads: list[dict] | None = None) -> list[dict]:
    """Enrich email for leads missing one. Mutates and returns the list."""
    if leads is None:
        leads = []

    counts: dict[str, int] = {"hunter": 0, "apollo": 0, "pdl": 0}
    enriched = 0

    providers = [
        ("hunter", _try_hunter),
        ("apollo", _try_apollo),
        ("pdl", _try_pdl),
    ]

    for lead in leads:
        # Only attempt leads missing email that have name + a resolvable domain
        if lead.get("email"):
            continue
        name = lead.get("name") or ""
        if not name:
            continue
        domain = _derive_domain(lead)
        if not domain:
            continue

        first, last = _split_name(name)

        for provider_name, provider_fn in providers:
            try:
                email = provider_fn(first, last, domain)
            except Exception as exc:
                lead.setdefault("_errors", []).append(
                    f"waterfall/{provider_name}: {exc}"
                )
                email = None

            if email:
                lead["email"] = email
                lead["_email_source"] = provider_name
                lead["enriched_at"] = _now_iso()
                counts[provider_name] += 1
                enriched += 1
                break  # stop on first hit

    try:
        log.log_stage(
            "enrich/waterfall",
            {"enriched": enriched, "by": counts},
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
            "source": "exa_web_search",
            "signal": None,
            "_errors": [],
        }
    ]
    print(json.dumps(main(fixture), default=str)[:2000])
