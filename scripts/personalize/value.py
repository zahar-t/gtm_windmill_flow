"""personalize/value.py — Relevance hook builder for copy personalization.

Builds a short, signal-driven value snippet for outbound emails based on
the lead's firmographics and recent buying signal. Never fabricates numbers
when inputs are missing.

Used by hot.py and warm.py to inject concrete context into prompts when
available.
"""
from __future__ import annotations

from scripts.common import config


def _humanize_amount(amount_eur: float | None) -> str | None:
    """Format a funding/revenue figure for copy: 10_000_000 -> '~10M'; 750_000 -> '~750k'.
    None -> None. No fabrication."""
    if amount_eur is None or amount_eur <= 0:
        return None
    if amount_eur >= 1_000_000:
        m = amount_eur / 1_000_000
        if m == int(m):
            return f"~{int(m)}M"
        return f"~{m:.1f}M"
    if amount_eur >= 1_000:
        k = int(round(amount_eur / 1_000))
        return f"~{k}k"
    return f"~{int(round(amount_eur))}"


def value_line(lead: dict) -> dict:
    """Returns {amount_eur, amount_str, hook} for prompt injection.

    Reads lead['funding_amount_eur'] or lead['last_funding_eur'] for size context.
    Builds a generic relevance hook from signal + firmographics.
    All None-safe — unknown data returns empty strings, never fabricated figures.
    """
    amount_eur = lead.get("funding_amount_eur") or lead.get("last_funding_eur")
    try:
        amount_eur = float(amount_eur) if amount_eur is not None else None
    except (TypeError, ValueError):
        amount_eur = None

    amount_str = _humanize_amount(amount_eur)

    # Build a short relevance hook from available context (no fabrication)
    signal_type = (lead.get("signal_type") or "").lower()
    industry = lead.get("industry") or ""
    company_size = lead.get("company_size")

    hook_parts: list[str] = []
    if signal_type == "funding" and amount_str:
        hook_parts.append(f"fresh off a {amount_str} raise")
    elif signal_type == "hiring":
        hook_parts.append("actively scaling the team")
    elif signal_type == "job_change":
        hook_parts.append("new in role")
    elif signal_type == "product_launch":
        hook_parts.append("just shipped a new product")

    if industry:
        hook_parts.append(f"{industry} company")
    if company_size:
        hook_parts.append(f"~{company_size} people")

    hook = ", ".join(hook_parts) if hook_parts else ""

    return {
        "amount_eur": amount_eur,
        "amount_str": amount_str,
        "hook": hook,
    }


if __name__ == "__main__":
    print("=== personalize/value.py smoke ===")

    # value_line({}) -> all None/empty
    result = value_line({})
    assert result == {"amount_eur": None, "amount_str": None, "hook": ""}, result
    print(f"  value_line({{}}) -> {result}  PASS")

    # _humanize_amount formatting
    assert _humanize_amount(10_000_000) == "~10M", _humanize_amount(10_000_000)
    assert _humanize_amount(750_000) == "~750k", _humanize_amount(750_000)
    assert _humanize_amount(2_300_000) == "~2.3M", _humanize_amount(2_300_000)
    assert _humanize_amount(None) is None
    print("  _humanize_amount: PASS")

    # value_line with a known amount and signal
    vl = value_line({"funding_amount_eur": 5_000_000, "signal_type": "funding",
                     "industry": "b2b saas", "company_size": 120})
    assert vl["amount_str"] == "~5M", vl
    assert "raise" in vl["hook"], vl
    print(f"  value_line(5M, funding): {vl}  PASS")

    # value_line with hiring signal
    vl2 = value_line({"signal_type": "hiring", "industry": "fintech saas"})
    assert "scaling" in vl2["hook"], vl2
    print(f"  value_line(hiring): {vl2}  PASS")

    print("PASS: all value.py assertions")
