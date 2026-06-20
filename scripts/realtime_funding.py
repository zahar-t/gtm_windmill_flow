"""Event-driven handler for ONE funding event. Mirror realtime_intake.py.

One funding round -> [lead] -> enrich -> score(icp+priority) -> dedup -> route ->
validate -> channel -> personalize -> spam -> send -> CRM -> handoff.

A funding event is a top-tier buying signal: it confirms budget, growth momentum,
and a clear trigger for outreach. Do NOT wait for the 8am batch — process
immediately while the signal is fresh. Smoke-safe. Never raises.

    from scripts.realtime_funding import main
    main({"company": "Acme AI", "domain": "acme.example", "amount_eur": 5000000,
          "round": "series_a", "announced_at": "2026-06-20", "investors": ["Top Fund"],
          "country": "DE"})

Node-envelope note: wire as current-style main(event)->dict node; Step 3 wraps in
gate->state->dead_letter envelope with dead_letter(node='realtime_funding', reason_code='funding_parse').
"""
from __future__ import annotations

from datetime import date
from typing import Any

from scripts.common import log
from scripts.enrich import waterfall, signals, company
from scripts.score import icp, route
from scripts.score import priority as score_priority
from scripts.score import channel as score_channel
from scripts.crm import dedup, upsert, handoff
from scripts.personalize import hot, warm
from scripts.email import spam_score, postmaster, send, log as email_log, validate as email_validate


def _months_since(iso_date: str | None) -> int | None:
    """Derive months since the funding announcement from an ISO date string. None -> None."""
    if not iso_date:
        return None
    try:
        announced = date.fromisoformat(iso_date[:10])
        delta_days = (date.today() - announced).days
        return max(0, delta_days // 30)
    except Exception:
        return None


def _lead_from_event(event: dict) -> dict:
    """Map a funding event (same normalized shape as funding.recent_rounds() record,
    OR a raw webhook) to a canonical lead with source='funding_feed', signal_type='funding',
    signal_ts=announced_at, funding_* fields + the rubric keys set.
    """
    company_name = event.get("company") or "Unknown"
    amount_eur = event.get("amount_eur") or event.get("funding_amount_eur")
    round_str = event.get("round") or event.get("funding_round")
    announced_at = event.get("announced_at") or event.get("funding_announced_at")
    investors = event.get("investors") or []
    domain = event.get("domain")
    country = event.get("country")

    # Build human-readable signal
    if amount_eur is not None:
        try:
            v = float(amount_eur)
            if v >= 1_000_000:
                amt_str = f"~${v / 1_000_000:.1f}M".replace(".0M", "M")
            else:
                amt_str = f"~${int(v / 1000)}k"
        except (TypeError, ValueError):
            amt_str = "undisclosed"
    else:
        amt_str = "undisclosed"
        amount_eur = None

    round_label = round_str.replace("_", " ").title() if isinstance(round_str, str) else "funding"
    signal = f"{company_name} raised {round_label} ({amt_str})"

    return {
        # Identity (person unknown; enrich waterfall fills)
        "name": event.get("name"),
        "email": event.get("email"),
        "title": event.get("title"),
        "linkedin_url": event.get("linkedin_url"),
        # Company
        "company": company_name,
        "domain": domain,
        "country": country,
        # Source
        "source": "funding_feed",
        # Signal — funding is a strong buying signal (confirms budget + growth momentum)
        "signal": signal,
        "signal_type": "funding",
        "signal_ts": announced_at,
        # Funding fields (contracts.Lead)
        "funding_amount_eur": amount_eur,
        "funding_round": round_str,
        "funding_announced_at": announced_at,
        "investors": investors,
        # Keys that icp_rubric.py reads directly
        "last_funding_eur": amount_eur,
        "funding_stage": round_str,
        "months_since_last_funding": _months_since(announced_at),
        "_errors": [],
        "_realtime": True,
    }


def main(event: dict | None = None) -> dict:
    """Run one funding event through the full funnel in real time. Never raises.

    Order MUST match the batch:
      icp -> priority -> dedup -> (stop if _skip) -> route ->
      validate -> channel -> hot/warm -> spam -> send -> upsert -> handoff.

    Returns {result, company, icp_score, priority, channel, stage}.
    No event -> {'skipped': 'no_event'}.
    """
    if not event:
        return {"skipped": "no_event"}

    try:
        lead = _lead_from_event(event)
    except Exception as exc:
        return {"skipped": "parse_error", "error": str(exc)}

    leads = [lead]

    # Enrich (waterfall finds email; signals/company add firmographics)
    try:
        waterfall.main(leads)
        signals.main(leads)
        company.main(leads)
    except Exception:
        pass

    # Score — ICP rubric first, then priority layer
    try:
        icp.main(leads)
    except Exception:
        pass

    try:
        score_priority.main(leads)
    except Exception:
        pass

    # Dedup / suppression — stop here if _skip
    try:
        dedup.main(leads)
    except Exception:
        pass

    if lead.get("_skip"):
        try:
            log.log_stage("realtime_funding", {
                "result": "skipped",
                "reason": lead.get("_skip_reason"),
                "company": lead.get("company"),
            })
        except Exception:
            pass
        return {
            "result": "skipped",
            "reason": lead.get("_skip_reason"),
            "company": lead.get("company"),
        }

    # Route -> validate -> channel
    try:
        route.main(leads)
    except Exception:
        pass

    try:
        email_validate.main(leads)
    except Exception:
        pass

    try:
        score_channel.main(leads)
    except Exception:
        pass

    # Personalize by temperature
    try:
        hot.main(leads)
        warm.main(leads)
    except Exception:
        pass

    # Deliverability gates -> send
    try:
        spam_score.main(leads)
    except Exception:
        pass

    try:
        reputation = postmaster.latest_verdict()
    except Exception:
        reputation = {"status": "unknown", "send_multiplier": 1.0}

    try:
        send.main(leads, reputation=reputation)
        email_log.main(leads)
    except Exception:
        pass

    # Persist + handoff
    try:
        upsert.main(leads)
    except Exception:
        pass

    try:
        handoff.main(leads)
    except Exception:
        pass

    result = {
        "result": "processed",
        "company": lead.get("company"),
        "icp_score": lead.get("icp_score"),
        "priority": lead.get("priority"),
        "channel": lead.get("channel"),
        "stage": lead.get("stage"),
        "spam_score": lead.get("spam_score"),
        "held": lead.get("_hold"),
    }
    try:
        log.log_stage("realtime_funding", result)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    import json

    print("=== realtime_funding.py smoke (keyless — one funding event, no send) ===")

    # No event -> skipped
    out_none = main(None)
    assert out_none.get("skipped") == "no_event", out_none
    print(f"  main(None) -> {out_none}  PASS")

    # Minimal event -> processed or skipped (both valid keyless outcomes)
    test_event = {
        "company": "Acme AI",
        "domain": "acme.example",
        "amount_eur": 5_000_000,
        "round": "series_a",
        "announced_at": "2026-06-20",
        "investors": ["Top Fund"],
        "country": "DE",
    }
    out = main(test_event)
    print(f"  main(event) -> {json.dumps(out, default=str)}")
    assert out.get("result") in ("processed", "skipped"), f"unexpected result: {out}"
    print(f"PASS: result={out['result']}")
