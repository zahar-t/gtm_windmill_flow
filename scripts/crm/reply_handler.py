"""crm/reply_handler.py — OPTIONAL backstop for replied leads.

The PRIMARY reply handoff is event-driven: the hourly outcomes poller
(scripts/feedback/outcomes.py) flips stage='replied' and Slacks the human the
moment a reply is detected — within the hour, not the next day. This module is
a cheap idempotent *backstop*: it re-sweeps for any lead labelled
outcome='reply' but still stage!='replied' (e.g. Slack was down when the poller
ran) and finishes the handoff. It is NOT wired into the daily flow by default;
run it on its own schedule if you want belt-and-suspenders.

Operates on EXISTING Supabase leads. Smoke-safe: no creds → {"handled": 0}.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.common import config, log, slack

try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(limit: int = 200) -> dict:
    """Hand off replied leads. Returns {"handled": int}; never raises."""
    if not config.SUPABASE_URL or not config.SUPABASE_KEY or not _SUPABASE_OK or _supabase is None:
        try:
            log.log_stage("crm/reply_handler", {"handled": 0})
        except Exception:
            pass
        return {"handled": 0}

    try:
        # Replies the poller has labelled but no one has actioned yet.
        rows = _supabase.select(
            "leads",
            {"outcome": "eq.reply", "stage": "neq.replied"},
            columns="id,email,name,company,title,icp_score,signal,stage",
            limit=limit,
        )
    except Exception:
        try:
            log.log_stage("crm/reply_handler", {"handled": 0})
        except Exception:
            pass
        return {"handled": 0}

    handled = 0
    now = _now_iso()
    for lead in rows:
        lead_id = lead.get("id")
        if not lead_id:
            continue
        try:
            # 1. Flip to a terminal, sequence-stopping stage.
            _supabase.update(
                "leads",
                {"id": f"eq.{lead_id}"},
                {"stage": "replied", "updated_at": now},
            )
            # 2. Audit trail.
            try:
                _supabase.insert(
                    "activity",
                    {"lead_id": lead_id, "type": "reply",
                     "payload": {"email": lead.get("email"), "handed_off_at": now}},
                )
            except Exception:
                pass
            # 3. Human handoff.
            slack.post_reply(lead)
            handled += 1
        except Exception as exc:
            try:
                log.log_stage("crm/reply_handler", {"error": str(exc), "lead": lead_id})
            except Exception:
                pass

    try:
        log.log_stage("crm/reply_handler", {"handled": handled})
    except Exception:
        pass
    return {"handled": handled}


if __name__ == "__main__":
    import json
    print("=== reply_handler.py smoke (keyless — expect handled=0) ===")
    out = main()
    print(json.dumps(out))
    assert out == {"handled": 0}
    print("PASS: no-op without Supabase creds, no raise")
