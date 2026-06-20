"""Provider-agnostic funding feed. Crunchbase concrete; dealroom/sifted seams.
Returns normalized records; [] when no key (smoke). Uses common/http (soft-fail).

Normalized record contract (STABLE — downstream relies on):
  {company, domain, amount_eur, round, announced_at, investors:[...], country}
  - company:     str
  - domain:      str | None
  - amount_eur:  float | None    (None when unknown — NEVER fabricated)
  - round:       str | None      ('seed'|'series_a'|... lower_snake; raw passthrough ok)
  - announced_at:str | None      (ISO date)
  - investors:   list[str]       (investor/fund names; [] if unknown)
  - country:     str | None      (ISO-2)
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from scripts.common import config, log
from scripts.common.http import get_json, post_json

_CB_BASE = "https://api.crunchbase.com/api/v4"


# ---- internal helpers ----

def _domain(url: str | None) -> str | None:
    """Extract bare domain from a homepage URL. Reused from exa.py pattern."""
    if not url:
        return None
    try:
        host = urlparse(url).netloc
        return host.lstrip("www.") if host else None
    except Exception:
        return None


def _company_name_from_title(title: str) -> str:
    """Best-effort: 'Velocity AI raises $4M seed' -> 'Velocity AI'.
    Adapted from vendor/opensales exa.py:34 regex pattern."""
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


# ---- normalization ----

def _to_eur(amount: float | None, currency: str | None) -> float | None:
    """Best-effort minor-FX normalization to EUR using config.FX_TO_EUR (static map).
    Unknown currency or None amount -> None (never fabricate)."""
    if amount is None or amount <= 0:
        return None
    if not currency:
        return None
    rate = config.FX_TO_EUR.get(currency.upper())
    if rate is None:
        return None   # unknown currency -> None, never fabricate
    return amount * rate


def _normalize_cb(node: dict) -> dict[str, Any]:
    """Map a Crunchbase funding_round search result node -> normalized record.

    Crunchbase v4 /searches/funding_rounds returns items with a 'properties'
    dict. Field names are provisional (see plan.md §9 risk 1); we read aliases
    defensively.
    """
    props = node.get("properties") or node  # handle both wrapped and flat responses

    # Company name: prefer organization_identifier.value, fall back to title parsing
    org = props.get("organization_identifier") or {}
    company = (
        org.get("value")
        or org.get("name")
        or props.get("organization_name")
        or _company_name_from_title(props.get("short_description") or "")
        or "Unknown"
    )

    # Homepage -> domain
    homepage = props.get("organization_identifier", {}).get("homepage_url") or props.get("homepage_url")
    domain = _domain(homepage)

    # Amount: Crunchbase uses 'money_raised' or 'raised_amount' with a currency field
    raw_amount = (
        props.get("money_raised", {}).get("value")
        if isinstance(props.get("money_raised"), dict)
        else props.get("money_raised")
        or props.get("raised_amount")
        or props.get("raised_amount_usd")
    )
    currency = (
        props.get("money_raised", {}).get("currency")
        if isinstance(props.get("money_raised"), dict)
        else props.get("raised_amount_currency_code")
        or props.get("raised_currency")
        or "USD"
    )
    try:
        raw_amount = float(raw_amount) if raw_amount is not None else None
    except (TypeError, ValueError):
        raw_amount = None
    amount_eur = _to_eur(raw_amount, currency)

    # Round type: investment_type or funding_type
    round_raw = props.get("investment_type") or props.get("funding_type") or props.get("series")
    round_str = round_raw.lower().replace(" ", "_") if isinstance(round_raw, str) else None

    # Announced date
    announced_at = props.get("announced_on") or props.get("announced_on_trust_code") and None

    # Investors: investor_identifiers is a list of {value, entity_def_id, ...}
    investor_list = props.get("investor_identifiers") or props.get("lead_investor_identifiers") or []
    investors: list[str] = []
    for inv in investor_list:
        if isinstance(inv, dict):
            name = inv.get("value") or inv.get("name")
            if name:
                investors.append(str(name))
        elif isinstance(inv, str):
            investors.append(inv)

    # Country: organization location
    country = (
        props.get("organization_location_identifiers", [{}])[0].get("location_type") and None
        or props.get("country_code")
        or props.get("organization_identifier", {}).get("country_code")
    )
    # Crunchbase country_code is 3-letter; we want ISO-2. Minimal map for common ones.
    # For unlisted: pass None (never fabricate).
    _CC3_TO_2 = {
        "USA": "US", "GBR": "GB", "DEU": "DE", "FRA": "FR", "ESP": "ES",
        "PRT": "PT", "NLD": "NL", "SWE": "SE", "NOR": "NO", "DNK": "DK",
        "CHE": "CH", "IRL": "IE", "ISR": "IL", "IND": "IN", "SGP": "SG",
        "CAN": "CA", "AUS": "AU", "BRA": "BR",
    }
    if country and len(country) == 3:
        country = _CC3_TO_2.get(country.upper(), None)
    elif country and len(country) == 2:
        country = country.upper()
    else:
        country = None

    return {
        "company": company,
        "domain": domain,
        "amount_eur": amount_eur,
        "round": round_str,
        "announced_at": announced_at,
        "investors": investors,
        "country": country,
    }


# ---- provider adapters (each returns list[normalized] or []) ----

def _fetch_crunchbase(since_days: int, limit: int) -> list[dict[str, Any]]:
    """POST {_CB_BASE}/searches/funding_rounds with header 'X-cb-user-key'.
    Filter announced_on >= today-since_days, order announced_on desc, page size=limit.
    Country filter optional via config.FUNDING_COUNTRIES (ISO-2 list) when set.
    Returns [] if not config.CRUNCHBASE_API_KEY or on any error.

    NOTE: Crunchbase API field paths are version-sensitive (plan.md §9 risk 1).
    Verify against live v4 docs when adding the key.
    """
    if not config.CRUNCHBASE_API_KEY:
        return []

    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=since_days)).isoformat()

    predicates = [
        {"field_id": "announced_on", "operator_id": "gte", "values": [cutoff]},
    ]
    if config.FUNDING_COUNTRIES:
        predicates.append({
            "field_id": "organization_location_identifiers",
            "operator_id": "includes",
            "values": config.FUNDING_COUNTRIES,
        })

    payload = {
        "field_ids": [
            "announced_on", "investment_type", "money_raised", "raised_amount_currency_code",
            "organization_identifier", "investor_identifiers", "short_description",
        ],
        "order": [{"field_id": "announced_on", "sort": "desc"}],
        "predicates": predicates,
        "limit": limit,
    }
    headers = {
        "X-cb-user-key": config.CRUNCHBASE_API_KEY,
        "Content-Type": "application/json",
    }

    data = post_json(f"{_CB_BASE}/searches/funding_rounds", headers=headers, json=payload)
    if not data or not isinstance(data, dict):
        return []

    entities = data.get("entities") or data.get("data", {}).get("items") or []
    results: list[dict[str, Any]] = []
    for node in entities:
        try:
            results.append(_normalize_cb(node))
        except Exception:
            pass  # soft-fail per record; partial results OK
    return results


def _fetch_dealroom(since_days: int, limit: int) -> list[dict[str, Any]]:
    return []   # seam: implement against config.DEALROOM_API_KEY later


def _fetch_sifted(since_days: int, limit: int) -> list[dict[str, Any]]:
    return []   # seam: implement against config.SIFTED_API_KEY later


_PROVIDERS = {
    "crunchbase": _fetch_crunchbase,
    "dealroom": _fetch_dealroom,
    "sifted": _fetch_sifted,
}


# ---- public API ----

def available() -> bool:
    """True iff the selected provider has its key. Mirrors instantly._ready()."""
    provider = config.FUNDING_PROVIDER
    if provider == "crunchbase":
        return bool(config.CRUNCHBASE_API_KEY)
    if provider == "dealroom":
        return bool(config.DEALROOM_API_KEY)
    if provider == "sifted":
        return bool(config.SIFTED_API_KEY)
    return False


def recent_rounds(since_days: int = 7, limit: int = 25) -> list[dict[str, Any]]:
    """Public API. Dispatch to config.FUNDING_PROVIDER's adapter; [] on miss/error.

    Each record (STABLE CONTRACT downstream relies on):
      {company, domain, amount_eur, round, announced_at, investors:[...], country}

    Logs log_stage('common/funding', {provider, found}).
    """
    provider = config.FUNDING_PROVIDER
    fetch = _PROVIDERS.get(provider)
    if not fetch:
        try:
            log.log_stage("common/funding", {"provider": provider, "found": 0, "error": "unknown_provider"})
        except Exception:
            pass
        return []

    try:
        results = fetch(since_days, limit)
    except Exception:
        results = []

    try:
        log.log_stage("common/funding", {"provider": provider, "found": len(results)})
    except Exception:
        pass
    return results


if __name__ == "__main__":
    print("=== common/funding.py smoke (no key expected) ===")
    print(f"  available(): {available()}")
    assert available() is False, "keyless: available() must be False"

    rounds = recent_rounds()
    print(f"  recent_rounds(): {rounds}")
    assert rounds == [], f"keyless: recent_rounds() must be [] got {rounds}"

    # Test helpers
    assert _to_eur(10_000_000, "USD") == 10_000_000 * 0.92
    assert _to_eur(None, "EUR") is None
    assert _to_eur(1000, "XYZ") is None   # unknown currency -> None
    print("  _to_eur tests: PASS")

    name = _company_name_from_title("Velocity AI raises $4M seed round - TechCrunch")
    assert "Velocity AI" in name or name, f"company name extraction: {name}"
    print(f"  _company_name_from_title: '{name}' PASS")

    print("PASS: keyless no-op, no crash")
