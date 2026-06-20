"""intake/unipile_engagement.py — warm LinkedIn engagement → canonical leads.

The unified flow has three entry modes, all converging at enrichment:
  • 8am cron            → cold lead generation (Exa · Apify · missed-webhook drain)
  • webhook (event)     → one inbound website/LinkedIn visitor
  • ~20-min poll (mode) → THIS node: pull people who engaged with us on LinkedIn

Unipile exposes no webhook for profile views or post engagement, so we poll:
viewers of our LinkedIn profile + reactors/commenters on our recent posts. Each
becomes a warm lead carrying a "why now" signal, and rides the same funnel
(enrich → score → … → send → CRM). Inbound intent decays, so ~20 min beats the
8am batch; the cold-gen drain is the daily backfill for anything missed.

These leads arrive with a LinkedIn identity but usually no email — the email
waterfall (Hunter/Apollo/PDL, which need a domain) only catches some. Even
email-less, they're worth scoring: CRM record + instant Slack handoff for hot
ones engaging with our content. (Adding Unipile's get-email-from-profile to the
waterfall would close the email gap.)

Smoke-safe: no Unipile DSN/key/account → []. Never raises.
"""
from __future__ import annotations

from typing import Any

from scripts.common import config, log, unipile

# Profile viewers reuse the existing "visited our LinkedIn page" semantics;
# reactors/commenters get a distinct source so engagement is queryable on its own.
_SOURCE_BY_KIND = {
    "viewer": "linkedin_visitor",
    "reaction": "linkedin_engagement",
    "comment": "linkedin_engagement",
}


def _to_lead(person: dict) -> dict[str, Any]:
    """Map a Unipile engager to the canonical lead dict every stage expects."""
    return {
        "name": person.get("name"),
        "company": person.get("company"),
        "company_url": None,
        "domain": None,
        "title": person.get("title"),
        "email": None,                       # waterfall (or Unipile profile) fills this
        "linkedin_url": person.get("linkedin_url"),
        "country": person.get("country"),
        "source": _SOURCE_BY_KIND.get(person.get("_kind"), "linkedin_engagement"),
        "signal": person.get("_signal"),     # "reacted to '<post>…'" — a real why-now
        "_errors": [],
        "_realtime": True,
    }


def _key(person: dict) -> str | None:
    """Identity for in-batch dedup — same person can view AND engage."""
    return person.get("provider_id") or person.get("linkedin_url")


def main(limit: int = 50) -> list[dict[str, Any]]:
    """Pull warm LinkedIn engagement and return canonical lead dicts.

    Parameters
    ----------
    limit:  max leads to return per poll (default 50).

    Returns
    -------
    list[dict]
        Warm leads (source linkedin_visitor / linkedin_engagement). [] when
        Unipile is unconfigured (smoke path) or nothing new engaged.
    """
    n = max(0, int(limit or 0))
    if n == 0 or not (config.UNIPILE_DSN and config.UNIPILE_API_KEY and config.UNIPILE_ACCOUNT_ID):
        try:
            log.log_stage("intake/unipile_engagement", {"found": 0, "configured": False})
        except Exception:
            pass
        return []

    leads: list[dict[str, Any]] = []
    seen: set[str] = set()
    counts = {"viewer": 0, "reaction": 0, "comment": 0}

    try:
        # Engagers first (stronger intent than a passive profile view), then
        # viewers — dedup keeps the first/strongest signal per person.
        people = unipile.get_post_engagers() + unipile.get_profile_viewers()
        for p in people:
            if len(leads) >= n:
                break
            k = _key(p)
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            counts[p.get("_kind", "reaction")] = counts.get(p.get("_kind", "reaction"), 0) + 1
            leads.append(_to_lead(p))
    except Exception:
        leads = []

    try:
        log.log_stage(
            "intake/unipile_engagement",
            {"found": len(leads), "configured": True, "by_kind": counts},
        )
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    import json

    print(f"=== unipile_engagement.py smoke (keyless; configured={bool(config.UNIPILE_API_KEY)}) ===")
    out = main(limit=25)
    print(json.dumps(out, default=str)[:600])
    assert out == [], "no Unipile creds → no engagement leads"
    print("PASS: unconfigured → returned [] without raising")
