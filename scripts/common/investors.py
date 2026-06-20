"""common/investors.py — investor-graph lookups + relationship check.

Populated from funding records (persist_graph). Smoke-safe: no Supabase -> empty
results; relationship check is config-only (works keyless).
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, log


def _relationship_set() -> set[str]:
    """Lowercased, stripped names from config.RELATIONSHIP_INVESTORS. Config-only."""
    return {name.lower().strip() for name in config.RELATIONSHIP_INVESTORS if name.strip()}


def is_relationship(investor: str | None) -> bool:
    """True iff investor (case-insensitive) is in the relationship set. None -> False."""
    if not investor:
        return False
    return investor.lower().strip() in _relationship_set()


def lead_investor(lead: dict) -> str | None:
    """The lead's primary investor for routing.

    Order:
      1. first name in lead['investors'] that is_relationship() (prefer intro path)
      2. else first in lead['investors']
      3. else None

    Pure dict read; no DB needed for routing.
    """
    investors = lead.get("investors") or []
    if not investors:
        return None

    # Prefer a relationship investor
    for inv in investors:
        if is_relationship(inv):
            return inv

    # Fall back to first investor
    return investors[0] if investors else None


def persist_graph(records: list[dict]) -> int:
    """Best-effort: upsert investors + company_investors from records.

    Records carry: company/domain/investors/round/funding_amount_eur/funding_announced_at.
    No Supabase creds -> 0. try/except around every write. Keys on (domain, investor_name, round).
    Called after upsert in run_pipeline (it needs no lead id). Never raises.

    Returns rows written (investor + company_investor rows combined).
    """
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        return 0

    try:
        from scripts.common import supabase
    except Exception:
        return 0

    written = 0
    rel_set = _relationship_set()

    for record in records:
        investors = record.get("investors") or []
        company = record.get("company") or ""
        domain = record.get("domain") or ""
        round_str = record.get("funding_round") or record.get("round") or ""
        amount_eur = record.get("funding_amount_eur") or record.get("amount_eur")
        announced_at = record.get("funding_announced_at") or record.get("announced_at")

        for inv_name in investors:
            if not inv_name:
                continue
            try:
                # Upsert investor row
                supabase.upsert(
                    "investors",
                    {
                        "name": inv_name,
                        "is_relationship": inv_name.lower().strip() in rel_set,
                    },
                    on_conflict="name",
                )
                written += 1
            except Exception:
                pass

            if domain and round_str:
                try:
                    # Upsert company_investors edge
                    supabase.upsert(
                        "company_investors",
                        {
                            "company": company,
                            "domain": domain,
                            "investor_name": inv_name,
                            "round": round_str,
                            "amount_eur": amount_eur,
                            "announced_at": announced_at,
                        },
                        on_conflict="domain,investor_name,round",
                    )
                    written += 1
                except Exception:
                    pass

    try:
        log.log_stage("common/investors", {"persist_graph_written": written, "records": len(records)})
    except Exception:
        pass
    return written


if __name__ == "__main__":
    import os

    print("=== common/investors.py smoke ===")

    # is_relationship works keyless from config (RELATIONSHIP_INVESTORS empty by default)
    result = is_relationship("Acme Ventures")
    print(f"  is_relationship('Acme Ventures') (no config) -> {result}")
    assert result is False

    # With a mocked config value
    original = config.RELATIONSHIP_INVESTORS[:]
    config.RELATIONSHIP_INVESTORS.append("Acme Ventures")
    assert is_relationship("Acme Ventures") is True
    assert is_relationship("acme ventures") is True  # case-insensitive
    assert is_relationship(None) is False
    config.RELATIONSHIP_INVESTORS.clear()
    config.RELATIONSHIP_INVESTORS.extend(original)
    print("  is_relationship with config value: PASS")

    # persist_graph([]) -> 0
    result = persist_graph([])
    assert result == 0, f"keyless persist_graph([]) must be 0 got {result}"
    print(f"  persist_graph([]) -> {result}  PASS")

    # lead_investor prefers a relationship investor
    config.RELATIONSHIP_INVESTORS.append("Top Fund")
    lead = {"investors": ["Other Fund", "Top Fund"]}
    inv = lead_investor(lead)
    assert inv == "Top Fund", f"should pick relationship investor, got {inv}"
    config.RELATIONSHIP_INVESTORS.clear()
    config.RELATIONSHIP_INVESTORS.extend(original)
    print(f"  lead_investor prefers relationship: PASS (picked '{inv}')")

    # lead_investor falls back to first
    lead2 = {"investors": ["Fund A", "Fund B"]}
    assert lead_investor(lead2) == "Fund A"
    print("  lead_investor fallback to first: PASS")

    # lead_investor None when no investors
    assert lead_investor({}) is None
    print("  lead_investor({}): PASS")

    print("PASS: all investors.py assertions")
