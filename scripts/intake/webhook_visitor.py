"""intake/webhook_visitor.py — Map ONE inbound visitor webhook event → [lead].

The unified pipeline flow (f/daily/gtm_pipeline) has two triggers:
  • 8am cron  → cold lead generation (Exa · Apify LinkedIn · missed-webhook drain)
  • webhook   → this node: one inbound visitor, mapped to the canonical lead
                dict and handed straight to the shared enrichment stage.

Both triggers converge at enrich, so this module only does the MAPPING — the
funnel (enrich → score → … → send → CRM) is the flow's job, identical for both
paths. That's the speed-to-lead win: a visitor event rides the same machinery as
the batch but starts the moment it lands, not at 8am.

(Supersedes the standalone realtime_intake.py, whose per-lead funnel is now the
unified flow itself.)

Smoke-safe: no event → []. Never raises.
"""
from __future__ import annotations

from typing import Any

from scripts.common import log, node

_VALID_SOURCES = {"website_visitor", "linkedin_visitor"}


def main(event: dict | None = None, source: str = "website_visitor") -> list[dict[str, Any]]:
    """Map one inbound visitor event to a one-element canonical lead list.

    Parameters
    ----------
    event:   raw webhook payload (RB2B website visitor or LinkedIn page visitor).
    source:  "website_visitor" | "linkedin_visitor" (anything else → website).

    Returns
    -------
    list[dict]
        ``[lead]`` for a valid event, or ``[]`` when no event was supplied.
    """
    if not event:
        try:
            log.log_stage("intake/webhook_visitor", {"mapped": 0})
        except Exception:
            pass
        return []

    lead: dict[str, Any] = {
        "email": event.get("email"),
        "name": event.get("name"),
        "company": event.get("company"),
        "company_url": event.get("company_url"),
        "domain": event.get("domain"),
        # Visitor's own profile is the primary identity for the LinkedIn feed.
        "linkedin_url": event.get("linkedin_url") or event.get("visitor_linkedin_url"),
        "title": event.get("title"),
        "country": event.get("country"),
        "source": source if source in _VALID_SOURCES else "website_visitor",
        "signal": None,
        "_errors": [],
        "_realtime": True,
    }

    if not node.has_identity(lead):
        node.dead_letter("intake/webhook_visitor", node.NO_IDENTITY, lead,
                         detail="no email/linkedin/domain")
        node.record_run("intake/webhook_visitor", lead, node.STATUS_QUARANTINED)
        try:
            log.log_stage("intake/webhook_visitor", {"mapped": 0, "quarantined": 1})
        except Exception:
            pass
        return []

    try:
        log.log_stage("intake/webhook_visitor", {"mapped": 1, "source": lead["source"]})
    except Exception:
        pass
    return [lead]


if __name__ == "__main__":
    import json

    print("=== webhook_visitor.py smoke (mapping only — no funnel, no send) ===")
    out = main(
        {"email": "dana@northwind.example", "name": "Dana Reis",
         "company": "Northwind Labs", "country": "PT",
         "linkedin_url": "https://linkedin.com/in/dana"},
        source="linkedin_visitor",
    )
    print(json.dumps(out, default=str, indent=2))
    assert isinstance(out, list) and len(out) == 1, "one event → one lead"
    assert out[0]["source"] == "linkedin_visitor"
    assert main(None) == [] and main({}) == [], "no event → []"
    print("PASS: one event → one canonical lead; empty event → []")
