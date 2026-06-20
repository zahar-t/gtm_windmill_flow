"""score/channel.py — set lead['channel'] in {investor_intro, linkedin, email} by priority.

Run AFTER route.main + email_validate.main, BEFORE personalize.
Pure function, always runs. Never raises.

Node-envelope note: folds into the score node group; Step 3 routes channel-specific
dead-letters to separate queues.
"""
from __future__ import annotations

from scripts.common import config, log
from scripts.common import investors


def _pick_channel(lead: dict) -> str:
    """First match wins:
      'investor_intro'  if investors.lead_investor(lead) is a relationship investor
                        (warm intro beats any cold touch)
      'linkedin'        elif lead.get('linkedin_url') AND config.UNIPILE_API_KEY set
                        (Unipile configured -> we can actually touch on LinkedIn)
      'email'           else  (existing cold-email path)
    """
    # Resolve the primary investor for this lead
    primary = investors.lead_investor(lead)
    if primary and investors.is_relationship(primary):
        return "investor_intro"

    if lead.get("linkedin_url") and config.UNIPILE_API_KEY:
        return "linkedin"

    return "email"


def main(leads: list[dict] | None = None) -> list[dict]:
    """Set lead['channel'] and lead['lead_investor'] (resolved investor name) on each
    non-_skip, hot/warm lead. Cold leads default channel='email' (no effect; not sent).

    Logs log_stage('score/channel', {investor_intro, linkedin, email}).
    """
    if leads is None:
        leads = []

    counts: dict[str, int] = {"investor_intro": 0, "linkedin": 0, "email": 0}

    for lead in leads:
        if lead.get("_skip"):
            continue

        # Resolve lead_investor for all leads (used by reporting/handoff even for cold)
        primary = investors.lead_investor(lead)
        lead["lead_investor"] = primary

        stage = lead.get("stage") or ""
        if stage in ("hot", "warm"):
            ch = _pick_channel(lead)
        else:
            # Cold leads: set email as default so the field is always populated
            ch = "email"

        lead["channel"] = ch
        counts[ch] = counts.get(ch, 0) + 1

    try:
        log.log_stage("score/channel", counts)
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    import os

    print("=== score/channel.py smoke ===")

    # Baseline: no relationship investors, no Unipile key -> all email
    leads_plain = [
        {"stage": "hot", "linkedin_url": None, "investors": [], "icp_score": 80},
        {"stage": "warm", "linkedin_url": "https://linkedin.com/in/foo", "investors": []},
        {"stage": "cold", "investors": []},
    ]
    main(leads_plain)
    assert leads_plain[0]["channel"] == "email", leads_plain[0]
    assert leads_plain[1]["channel"] == "email", leads_plain[1]  # UNIPILE_API_KEY not set
    assert leads_plain[2]["channel"] == "email", leads_plain[2]
    print("  no-config plain leads -> all email: PASS")

    # investor_intro when relationship investor present
    original_rel = config.RELATIONSHIP_INVESTORS[:]
    config.RELATIONSHIP_INVESTORS.append("Top Fund")

    lead_intro = {"stage": "hot", "investors": ["Top Fund"], "linkedin_url": None}
    main([lead_intro])
    assert lead_intro["channel"] == "investor_intro", lead_intro
    assert lead_intro["lead_investor"] == "Top Fund"
    print(f"  investor_intro channel: PASS (lead_investor={lead_intro['lead_investor']})")

    # linkedin when UNIPILE_API_KEY set and no relationship investor
    original_unipile = config.UNIPILE_API_KEY
    config.UNIPILE_API_KEY = "fake-key"
    lead_li = {"stage": "hot", "investors": ["Non-relationship Fund"], "linkedin_url": "https://linkedin.com/in/bar"}
    main([lead_li])
    assert lead_li["channel"] == "linkedin", lead_li
    print(f"  linkedin channel: PASS")

    # Restore
    config.UNIPILE_API_KEY = original_unipile
    config.RELATIONSHIP_INVESTORS.clear()
    config.RELATIONSHIP_INVESTORS.extend(original_rel)

    print("PASS: all channel.py assertions")
