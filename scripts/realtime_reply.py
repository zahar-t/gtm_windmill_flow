"""realtime_reply.py — Event-driven handler for ONE inbound reply.

A reply is the hottest event in the funnel. Instantly POSTs each reply (its
reply_received webhook) to a Windmill webhook, which triggers this handler
immediately — so the human is pinged within SECONDS, not on the next hourly poll
and certainly not on the next daily run. It flips the lead to stage='replied'
(stopping the sequencer + permanently suppressing re-contact) and Slacks the owner.

The hourly outcomes poller remains as the backstop that sweeps anything this
webhook missed (e.g. webhook downtime). Smoke-safe: no Supabase → no-op.
Never raises.

    from scripts.realtime_reply import main
    main({"lead_email": "lead@acme.com", "subject": "Re: ...", "text": "..."})
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.common import config, log, slack
from scripts.common.reply_classify import classify as _classify_reply
from scripts.feedback.outcomes import classify_retrigger

try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sender(event: dict) -> str:
    raw = (event.get("lead_email") or event.get("from_email") or event.get("from") or event.get("sender") or "").strip().lower()
    # tolerate "Name <addr@x.com>"
    if "<" in raw and ">" in raw:
        raw = raw[raw.find("<") + 1: raw.find(">")].strip()
    return raw


def main(event: dict | None = None) -> dict:
    """Handle one inbound reply in real time. Returns {"handled": 0|1}; never raises."""
    if not event:
        return {"handled": 0, "reason": "no_event"}
    if not config.SUPABASE_URL or not config.SUPABASE_KEY or not _SUPABASE_OK or _supabase is None:
        return {"handled": 0, "reason": "no_db"}

    frm = _sender(event)
    if not frm:
        return {"handled": 0, "reason": "no_sender"}

    try:
        matched = _supabase.select(
            "leads",
            {"email": f"eq.{frm}"},
            columns="id,email,outcome,stage,name,company,title,icp_score",
            limit=1,
        )
    except Exception:
        return {"handled": 0, "reason": "lookup_failed"}

    if not matched:
        return {"handled": 0, "reason": "no_match", "from": frm}

    lead = matched[0]
    if lead.get("outcome") == "reply" and lead.get("stage") == "replied":
        return {"handled": 0, "reason": "already_handled", "from": frm}

    now = _now_iso()
    try:
        # Classify the reply intent (Task 1)
        reply_subject = event.get("subject") or ""
        reply_text = event.get("text") or reply_subject
        reply_class = _classify_reply(reply_subject, reply_text)

        # unsubscribe → flip outcome so dedup permanently suppresses
        outcome_value = "unsubscribe" if reply_class == "unsubscribe" else "reply"

        # Re-trigger classifier: detect deferral intent (ooo / not_now)
        retrigger = classify_retrigger(reply_text)

        update_payload: dict = {
            "outcome": outcome_value,
            "outcome_at": now,
            "stage": "replied",
            "updated_at": now,
            "reply_class": reply_class,
        }
        if retrigger:
            reason, days = retrigger
            update_payload["re_trigger_reason"] = reason
            update_payload["re_trigger_at"] = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).isoformat()

        _supabase.update(
            "leads",
            {"id": f"eq.{lead['id']}"},
            update_payload,
        )
        try:
            _supabase.insert(
                "activity",
                {"lead_id": lead["id"], "type": "reply",
                 "payload": {
                     "source": "instantly_reply_webhook",
                     "reply_class": reply_class,
                     "at": now,
                 }},
            )
        except Exception:
            pass
        # 'interested' gets a dedicated highlight alert; unsub → no Slack noise
        if reply_class == "interested":
            slack.post_interested_reply(lead)
        elif reply_class != "unsubscribe":
            slack.post_reply(lead)
    except Exception as exc:
        return {"handled": 0, "reason": f"error:{exc}", "from": frm}

    try:
        log.log_stage("realtime_reply", {"handled": 1, "from": frm})
    except Exception:
        pass
    return {"handled": 1, "from": frm, "lead_id": lead.get("id")}


if __name__ == "__main__":
    import json
    print("=== realtime_reply.py smoke (keyless — expect no_db) ===")
    out = main({"from": "Dana Reis <dana@northwind.example>", "subject": "Re: your note"})
    print(json.dumps(out))
    assert out["handled"] == 0
    print("PASS: no-op without Supabase, sender parsed, no raise")
