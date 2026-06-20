"""realtime_intake.py — Event-driven handler for ONE inbound visitor.

Warm = inbound intent, and intent decays in minutes (speed-to-lead). So a
visitor event is NOT queued for the 8am batch — the visitor source's webhook
triggers this handler the moment the event lands, and it runs that single lead
straight through the funnel: enrich → score → route → personalize → spam →
reputation → send → CRM → (instant Slack if hot).

This reuses the exact same stage modules as the daily batch — every stage is
`main(leads: list)`, so a 1-element list is just the N=1 case. The two global
gates (warmup ramp, reputation multiplier) keep their state in Supabase, so a
real-time send respects the same daily volume budget as the batch.

Smoke-safe: with no keys every stage no-ops and nothing sends. Never raises.

    from scripts.realtime_intake import main
    main({"email": "...", "name": "...", "company": "...",
          "linkedin_url": "...", "company_url": "..."}, source="website_visitor")
"""
from __future__ import annotations

from typing import Any

from scripts.common import log
from scripts.enrich import waterfall, signals, company
from scripts.score import icp, route
from scripts.crm import dedup, upsert, handoff
from scripts.personalize import hot, warm
from scripts.email import spam_score, postmaster, send, log as email_log

_VALID_SOURCES = {"website_visitor", "linkedin_visitor"}


def _lead_from_event(event: dict, source: str) -> dict:
    return {
        "email": event.get("email"),
        "name": event.get("name"),
        "company": event.get("company"),
        "company_url": event.get("company_url"),
        "domain": event.get("domain"),
        "linkedin_url": event.get("linkedin_url") or event.get("visitor_linkedin_url"),
        "title": event.get("title"),
        "country": event.get("country"),
        "source": source if source in _VALID_SOURCES else "website_visitor",
        "signal": None,
        "_errors": [],
        "_realtime": True,
    }


def main(event: dict | None = None, source: str = "website_visitor") -> dict:
    """Run one inbound visitor through the full funnel in real time.

    Returns a compact result dict; never raises.
    """
    if not event:
        return {"skipped": "no_event"}

    lead = _lead_from_event(event, source)
    leads = [lead]

    # Enrich (waterfall finds the email; signals/company add the "why now" + firmographics)
    waterfall.main(leads)
    signals.main(leads)
    company.main(leads)

    # Score → dedup/suppress → route
    icp.main(leads)
    dedup.main(leads)
    if lead.get("_skip"):
        # Recently-contacted or suppressed (reply/bounce/unsub) — stop here.
        try:
            log.log_stage("realtime_intake", {"result": "skipped", "reason": lead.get("_skip_reason")})
        except Exception:
            pass
        return {"result": "skipped", "reason": lead.get("_skip_reason"), "email": lead.get("email")}
    route.main(leads)

    # Personalize by temperature
    hot.main(leads)
    warm.main(leads)

    # Deliverability gates (same as batch) → send
    spam_score.main(leads)
    reputation = postmaster.latest_verdict()
    send.main(leads, reputation=reputation)
    email_log.main(leads)

    # Persist + instant human handoff for hot leads
    upsert.main(leads)
    handoff.main(leads)

    result = {
        "result": "processed",
        "email": lead.get("email"),
        "company": lead.get("company"),
        "icp_score": lead.get("icp_score"),
        "stage": lead.get("stage"),
        "spam_score": lead.get("spam_score"),
        "held": lead.get("_hold"),
        "reputation": reputation.get("status"),
    }
    try:
        log.log_stage("realtime_intake", result)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    import json
    print("=== realtime_intake.py smoke (keyless — one visitor, no send) ===")
    out = main({"email": "dana@northwind.example", "name": "Dana Reis",
                "company": "Northwind Labs", "country": "PT",
                "linkedin_url": "https://linkedin.com/in/dana"},
               source="linkedin_visitor")
    print(json.dumps(out, default=str, indent=2))
    assert out["result"] in ("processed", "skipped")
    print("PASS: single inbound lead ran the full per-lead path without raising")
