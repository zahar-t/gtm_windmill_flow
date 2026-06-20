"""intake/linkedin_cold.py — Cold-lead discovery / volume backstop (batched).

Warm leads (inbound visitors) are handled in REAL TIME — intent decays in
minutes. Cold discovery is the opposite: no urgency, so it runs once a day. This
node is the volume floor — when a daily run's inbound feeds come up thin, it
tops the batch up with cold ICP-matching leads scraped from LinkedIn/Exa.

It is a DISCOVERY feed, not an enricher: it ADDS new cold leads (source
"linkedin_cold", no email — the email waterfall makes them contactable
downstream). It deliberately runs AFTER intake_merge so it can see how many
inbound leads exist and only top up the shortfall.

Discovery source: Apify LinkedIn *search* actor if configured, else Exa neural
search (reusing exa.discover_companies). Smoke-safe: no keys → no discovery,
returns the inbound list unchanged. Never raises.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, exa, log


def _discover(query: str, n: int) -> list[dict[str, Any]]:
    """Return up to n cold ICP leads. Exa-backed; smoke-safe ([] without a key)."""
    try:
        results = exa.discover_companies(query, n)
    except Exception:
        return []
    leads: list[dict[str, Any]] = []
    for r in results:
        leads.append({
            "name": None,                      # person unknown at discovery
            "company": r.get("name"),
            "company_url": r.get("url"),
            "domain": r.get("domain"),
            "title": None,
            "email": None,                     # waterfall fills this downstream
            "linkedin_url": None,
            "source": "linkedin_cold",
            "signal": None,
            "_errors": [],
        })
    return leads


def main(leads: list[dict] | None = None, target: int | None = None) -> list[dict]:
    """Top up a thin inbound batch with cold discovery.

    Parameters
    ----------
    leads:   the inbound batch so far (from intake_merge). None → [].
    target:  desired minimum batch size. None → config.MIN_DAILY_LEADS.

    Returns inbound + freshly-discovered cold leads (≤ target total). If inbound
    already meets the floor, returns it unchanged — no spend on cold scraping.
    """
    leads = leads or []
    target = config.MIN_DAILY_LEADS if target is None else target

    shortfall = target - len(leads)
    if shortfall <= 0:
        try:
            log.log_stage("intake/linkedin_cold", {"inbound": len(leads), "discovered": 0, "reason": "floor_met"})
        except Exception:
            pass
        return leads

    discovered = _discover(config.ICP_QUERY, shortfall)
    out = leads + discovered

    try:
        log.log_stage(
            "intake/linkedin_cold",
            {"inbound": len(leads), "shortfall": shortfall, "discovered": len(discovered)},
        )
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import json
    print(f"=== linkedin_cold.py smoke (keyless; floor={config.MIN_DAILY_LEADS}) ===")
    # 2 inbound leads, no Exa key → discovery returns nothing, returns the 2 unchanged.
    inbound = [{"company": "Inbound A", "source": "website_visitor"},
               {"company": "Inbound B", "source": "linkedin_visitor"}]
    out = main(inbound)
    print(json.dumps(out, default=str)[:600])
    assert len(out) == 2, "keyless discovery must add nothing"
    print(f"PASS: floor not met but no key → returned inbound unchanged ({len(out)})")
