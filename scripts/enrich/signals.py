"""Exa funding/hiring/launch signal enrichment — last 7 days.

For each lead with a company name, fetches recent signals via exa.find_signals.
Sets lead["signal"] and lead["signal_type"] when a signal is found.
No signal → both keys left as None (drives warm routing downstream).
Never raises; per-lead errors go to lead["_errors"].
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.common import exa, log


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_best(signals: list[dict]) -> dict | None:
    """Return the most recent signal by published_date, or first if dates missing."""
    if not signals:
        return None
    # Try to sort by published_date (ISO strings sort lexicographically)
    dated = [s for s in signals if s.get("published_date")]
    if dated:
        return max(dated, key=lambda s: s["published_date"])
    return signals[0]


def main(leads: list[dict] | None = None) -> list[dict]:
    """Enrich each lead with the most recent growth/funding/launch signal."""
    if leads is None:
        leads = []

    with_signal = 0

    for lead in leads:
        company = lead.get("company") or ""
        if not company:
            continue

        try:
            sigs = exa.find_signals(company, n=5)
        except Exception as exc:
            lead.setdefault("_errors", []).append(f"signals/exa: {exc}")
            sigs = []

        chosen = _pick_best(sigs)
        if chosen:
            # Use title if available, fall back to snippet
            lead["signal"] = chosen.get("title") or chosen.get("snippet") or None
            lead["signal_type"] = chosen.get("type") or None
            lead["enriched_at"] = _now_iso()
            with_signal += 1
        # No signal → leave signal/signal_type as-is (None)

    try:
        log.log_stage(
            "enrich/signals",
            {"with_signal": with_signal, "total": len(leads)},
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
            "signal_type": None,
            "_errors": [],
        }
    ]
    print(json.dumps(main(fixture), default=str)[:2000])
