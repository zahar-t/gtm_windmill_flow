"""Company enrichment — Proxycurl (primary) with Clearbit fallback.

Fills company_size, industry, tech_stack, country (in-dict only; no leads
columns for these except via the ICP scorer). country (ISO-2) feeds the ICP
geography signal in score/icp_rubric.py. Sets enriched_at on the lead dict.
Never raises; per-lead errors go to lead["_errors"].
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from scripts.common import config, log
from scripts.common.http import get_json


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


def _try_proxycurl(lead: dict) -> dict | None:
    """Call Proxycurl company enrichment. Returns normalized dict or None."""
    if not config.PROXYCURL_API_KEY:
        return None

    # Use company_url if it looks like a LinkedIn URL, otherwise use domain URL
    company_url = lead.get("company_url") or ""
    domain = _derive_domain(lead)

    # Proxycurl accepts a LinkedIn company URL or a website URL
    lookup_url = company_url if "linkedin.com" in company_url else (
        f"https://{domain}" if domain else company_url
    )
    if not lookup_url:
        return None

    data = get_json(
        "https://nubela.co/proxycurl/api/linkedin/company",
        headers={"Authorization": f"Bearer {config.PROXYCURL_API_KEY}"},
        params={"url": lookup_url},
    )
    if not isinstance(data, dict):
        return None

    # Map Proxycurl fields
    company_size = (
        data.get("company_size_on_linkedin")
        or data.get("company_size")
        or None
    )
    industry = data.get("industry") or None
    tech_stack = data.get("tech_stack") or None
    # Proxycurl returns hq as a dict with a 'country' (ISO-2) field.
    hq = data.get("hq") or {}
    country = (hq.get("country") if isinstance(hq, dict) else None) or data.get("country") or None

    if not any([company_size, industry, tech_stack, country]):
        return None  # empty response — let Clearbit try

    return {
        "company_size": company_size,
        "industry": industry,
        "tech_stack": tech_stack,
        "country": country,
    }


def _try_clearbit(lead: dict) -> dict | None:
    """Call Clearbit company enrichment. Returns normalized dict or None."""
    if not config.CLEARBIT_API_KEY:
        return None

    domain = _derive_domain(lead)
    if not domain:
        return None

    data = get_json(
        "https://company.clearbit.com/v2/companies/find",
        headers={"Authorization": f"Bearer {config.CLEARBIT_API_KEY}"},
        params={"domain": domain},
    )
    if not isinstance(data, dict):
        return None

    # Map Clearbit fields
    metrics = data.get("metrics") or {}
    category = data.get("category") or {}

    company_size = metrics.get("employees") or None
    industry = category.get("industry") or None
    tech_stack = data.get("tech") or None
    geo = data.get("geo") or {}
    country = (geo.get("country") if isinstance(geo, dict) else None) or data.get("country") or None

    if not any([company_size, industry, tech_stack, country]):
        return None

    return {
        "company_size": company_size,
        "industry": industry,
        "tech_stack": tech_stack,
        "country": country,
    }


def main(leads: list[dict] | None = None) -> list[dict]:
    """Enrich company data (size, industry, tech stack) for each lead."""
    if leads is None:
        leads = []

    enriched = 0
    by: dict[str, int] = {"proxycurl": 0, "clearbit": 0}

    for lead in leads:
        # Need at least a domain or company_url to look up
        if not _derive_domain(lead) and not lead.get("company_url"):
            continue

        result: dict | None = None
        provider: str | None = None

        # Primary: Proxycurl
        try:
            result = _try_proxycurl(lead)
            if result:
                provider = "proxycurl"
        except Exception as exc:
            lead.setdefault("_errors", []).append(f"company/proxycurl: {exc}")
            result = None

        # Fallback: Clearbit
        if result is None:
            try:
                result = _try_clearbit(lead)
                if result:
                    provider = "clearbit"
            except Exception as exc:
                lead.setdefault("_errors", []).append(f"company/clearbit: {exc}")
                result = None

        if result and provider:
            # Write in-dict only (company_size/industry/tech_stack/country have no leads column)
            lead["company_size"] = result.get("company_size")
            lead["industry"] = result.get("industry")
            lead["tech_stack"] = result.get("tech_stack")
            # Don't override a country already supplied by the feed (e.g. linkedin_visitor)
            if not lead.get("country") and result.get("country"):
                lead["country"] = result.get("country")
            lead["enriched_at"] = _now_iso()
            by[provider] += 1
            enriched += 1

    try:
        log.log_stage("enrich/company", {"enriched": enriched, "by": by})
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
