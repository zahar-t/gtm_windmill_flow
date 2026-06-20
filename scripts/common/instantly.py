"""Instantly v2 client — cold-outreach send + warmup + open/reply/bounce tracking.

Instantly owns the cold send path. We don't transactional-send a message; we push
a personalized lead into a *campaign* and Instantly sends it on its own warmed,
inbox-rotated schedule, then reports events back (webhooks + campaign analytics).
That's the reputation play SendGrid can't do for cold email.

  • send         → POST /api/v2/leads  (add lead to campaign; per-lead subject/body
                   ride in custom_variables the campaign template references)
  • reputation   → GET  /api/v2/campaigns/analytics  (sent / bounced / opens / replies)
  • outcomes     → same analytics + lead status, matched back by email

Auth: header `Authorization: Bearer <INSTANTLY_API_KEY>`. Base from
config.INSTANTLY_BASE_URL (default https://api.instantly.ai/api/v2).

Every function returns None/{}/"" when the key (or campaign id, where needed) is
missing or on ANY error — uses common/http, which never raises. Response field
names are read defensively (verify against live payloads when keys are added).
"""
from __future__ import annotations

from typing import Any

from scripts.common import config
from scripts.common.http import get_json, post_json


def _ready() -> bool:
    return bool(config.INSTANTLY_API_KEY)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.INSTANTLY_API_KEY}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    return config.INSTANTLY_BASE_URL.rstrip("/")


def add_lead_to_campaign(lead: dict) -> str | None:
    """Push one personalized lead into the Instantly campaign. Returns its
    Instantly lead id (our send-tracking handle), or None on miss/skip.

    The AI-written subject/body travel as `custom_variables`; the campaign's
    sequence template renders {{ai_subject}} / {{ai_body}} per lead.
    """
    if not _ready() or not config.INSTANTLY_CAMPAIGN_ID:
        return None
    email = lead.get("email")
    if not email:
        return None

    first, _, last = (lead.get("name") or "").partition(" ")
    body = {
        "campaign": config.INSTANTLY_CAMPAIGN_ID,
        "email": email,
        "first_name": first or None,
        "last_name": last or None,
        "company_name": lead.get("company") or None,
        "custom_variables": {
            "ai_subject": lead.get("email_subject") or "",
            "ai_body": lead.get("email_body") or "",
            "signal": lead.get("signal") or "",
        },
    }
    data = post_json(f"{_base()}/leads", headers=_headers(), json=body, timeout=20.0, retries=1)
    if not isinstance(data, dict):
        return None
    return data.get("id") or data.get("lead_id") or None


def campaign_analytics() -> dict | None:
    """Trailing campaign analytics for the reputation gate. None if unavailable.

    Returns {sent, delivered, bounced, opens, replies, bounce_rate} — normalized
    from Instantly's analytics payload (field aliases handled defensively).
    """
    if not _ready() or not config.INSTANTLY_CAMPAIGN_ID:
        return None
    data = get_json(
        f"{_base()}/campaigns/analytics",
        headers=_headers(),
        params={"id": config.INSTANTLY_CAMPAIGN_ID},
        timeout=15.0,
        retries=1,
    )
    # Analytics may come back as a single object or a one-element list.
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None

    def _n(*keys: str) -> int:
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return 0

    sent = _n("emails_sent_count", "sent", "emails_sent")
    bounced = _n("bounced_count", "bounced", "total_bounces")
    if sent <= 0:
        return {"_empty": True}
    return {
        "sent": sent,
        "delivered": sent - bounced,
        "bounced": bounced,
        "opens": _n("open_count", "opens", "total_opens"),
        "replies": _n("reply_count", "replies", "total_replies"),
        "bounce_rate": round(bounced / sent, 5),
    }


def lead_outcomes(emails: list[str]) -> dict[str, dict[str, bool]]:
    """For the feedback poller: per-lead engagement, keyed by email.

    Returns {email_lower: {"opened": bool, "bounced": bool, "replied": bool}}.
    {} on no key / no emails / any failure. Reads Instantly's per-email events.
    """
    wanted = {e.strip().lower() for e in emails if e}
    if not _ready() or not config.INSTANTLY_CAMPAIGN_ID or not wanted:
        return {}
    data = get_json(
        f"{_base()}/emails",
        headers=_headers(),
        params={"campaign_id": config.INSTANTLY_CAMPAIGN_ID, "limit": 100},
        timeout=15.0,
        retries=1,
    )
    rows = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out: dict[str, dict[str, bool]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        email = (row.get("lead_email") or row.get("to_email") or row.get("email") or "").strip().lower()
        if email not in wanted:
            continue
        rec = out.setdefault(email, {"opened": False, "bounced": False, "replied": False})
        status = (row.get("status") or row.get("event_type") or "").lower()
        if "bounce" in status:
            rec["bounced"] = True
        if "reply" in status or row.get("reply_count"):
            rec["replied"] = True
        if "open" in status or row.get("open_count"):
            rec["opened"] = True
    return out


if __name__ == "__main__":
    import json

    print(f"=== instantly.py smoke (keyless; ready={_ready()}) ===")
    out = {
        "send": add_lead_to_campaign({"email": "x@y.example", "name": "X Y", "email_body": "hi"}),
        "analytics": campaign_analytics(),
        "outcomes": lead_outcomes(["x@y.example"]),
    }
    print(json.dumps(out, default=str)[:300])
    assert out["send"] is None and out["analytics"] is None and out["outcomes"] == {}
    print("PASS: no Instantly key → send/analytics/outcomes all no-op without raising")
