"""Funding feed -> canonical lead dicts (PRIMARY source). source='funding_feed'.
signal_type='funding'. Smoke-safe: [] without a provider key.

Mirrors scripts/intake/web_search.py shape (source node, main(...) -> list[dict],
no Supabase — dedup is downstream).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from scripts.common import config, log, node
from scripts.common import funding


def _months_since(iso_date: str | None) -> int | None:
    """Derive months_since_last_funding from an ISO date string. None -> None."""
    if not iso_date:
        return None
    try:
        announced = date.fromisoformat(iso_date[:10])
        delta_days = (date.today() - announced).days
        return max(0, delta_days // 30)
    except Exception:
        return None


def main(since_days: int = 7, limit: int = 25) -> list[dict[str, Any]]:
    """Pull recent funding rounds and map each to a canonical lead dict.

    Returns [] when funding.recent_rounds() is empty (keyless smoke path).
    Logs log_stage('intake/funding', {found}).

    Per record sets:
      company, domain, country
      source            = 'funding_feed'
      signal            = f"{company} raised {round} ({amount str or 'undisclosed'})"
      signal_type       = 'funding'
      signal_ts         = announced_at (ISO)        # recency weighting reads this
      funding_amount_eur= amount_eur                # contracts field
      funding_round     = round
      funding_announced_at = announced_at
      investors         = investors (list[str])
      last_funding_eur  = amount_eur  # ALSO set — icp_rubric reads it (icp_rubric.py:450)
      funding_stage     = round       # icp_rubric._coerce_stage reads 'funding_stage'
      months_since_last_funding = derived from announced_at
      _errors           = []
    name/email/title/linkedin_url = None (person unknown; enrich waterfall fills).
    """
    rounds = funding.recent_rounds(since_days=since_days, limit=limit)
    if not rounds:
        try:
            log.log_stage("intake/funding", {"found": 0})
        except Exception:
            pass
        return []

    leads: list[dict[str, Any]] = []

    def _emit(lead: dict[str, Any]) -> None:
        if node.has_identity(lead):
            leads.append(lead)
        else:
            node.dead_letter("intake/funding", node.NO_IDENTITY, lead,
                             detail="no email/linkedin/domain")
            node.record_run("intake/funding", lead, node.STATUS_QUARANTINED)

    for r in rounds:
        try:
            company = r.get("company") or "Unknown"
            amount_eur = r.get("amount_eur")
            round_str = r.get("round")
            announced_at = r.get("announced_at")

            # Build human-readable signal
            if amount_eur is not None:
                # Format: ~€10M, ~€2.3M, ~€750k
                if amount_eur >= 1_000_000:
                    amt_str = f"~€{amount_eur / 1_000_000:.1f}M".replace(".0M", "M")
                else:
                    amt_str = f"~€{int(amount_eur / 1000)}k"
                amount_label = amt_str
            else:
                amount_label = "undisclosed"

            round_label = round_str.replace("_", " ").title() if round_str else "funding"
            signal = f"{company} raised {round_label} ({amount_label})"

            lead: dict[str, Any] = {
                # Identity
                "name": None,
                "email": None,
                "title": None,
                "linkedin_url": None,
                # Company
                "company": company,
                "domain": r.get("domain"),
                "country": r.get("country"),
                # Source
                "source": "funding_feed",
                # Signal
                "signal": signal,
                "signal_type": "funding",
                "signal_ts": announced_at,
                # Funding fields (contracts.Lead)
                "funding_amount_eur": amount_eur,
                "funding_round": round_str,
                "funding_announced_at": announced_at,
                "investors": r.get("investors") or [],
                # Keys that icp_rubric.py reads directly (icp_rubric.py:435,447,450)
                "last_funding_eur": amount_eur,
                "funding_stage": round_str,
                "months_since_last_funding": _months_since(announced_at),
                "_errors": [],
            }
            _emit(lead)
        except Exception as exc:
            _emit({"company": None, "source": "funding_feed", "_errors": [str(exc)]})

    try:
        log.log_stage("intake/funding", {"found": len(leads)})
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    import json

    print("=== intake/funding.py smoke (no key expected) ===")
    result = main()
    print(f"  main() -> {result}")
    assert result == [], f"keyless: main() must be [] got {result}"
    print("PASS: keyless returns [], no crash")
