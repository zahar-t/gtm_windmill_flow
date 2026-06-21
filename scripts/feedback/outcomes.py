"""feedback/outcomes.py — Outcome poller for the feedback loop.

Reads two sources and writes terminal outcomes (reply/bounce/no_open/open_no_reply)
to leads.outcome + outcome_at, and logs activity rows.

  Source A: Instantly campaign analytics — match on leads.instantly_lead_id (by
            email) -> classify bounce/no_open/open_no_reply.
  Source B: inbound_email_events table drain (fed by Instantly's reply webhook) —
            match from_email -> leads.email -> outcome='reply'.

Keyless / smoke-safe: returns the empty summary dict without touching the network
when SUPABASE_URL/SUPABASE_KEY are absent. Never raises out of main().

def main(limit: int = 500) -> dict
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from scripts.common import config
from scripts.common import supabase
from scripts.common import log as common_log
from scripts.common import slack
from scripts.common import instantly
from scripts.common.reply_classify import classify as _classify_reply

# ---------------------------------------------------------------------------
# Re-trigger classifier — deterministic keyword rules (Area 4 ADD 3)
# ---------------------------------------------------------------------------

_RETRIGGER_RULES = [   # (pattern, reason, days_offset) — first match wins, case-insensitive
    (r"\bafter (the|our) (round|raise|funding)\b", "after_round", 45),
    (r"\bnext quarter\b|\bnext qtr\b|\bq[1-4]\b", "next_quarter", 90),
    (r"\bcircle back\b|\breach out (again )?in\b|\bin (a few|some) (weeks|months)\b", "later", 30),
    (r"\bnot (right )?now\b|\blater\b|\bdown the road\b|\brevisit\b", "later", 30),
]


def classify_retrigger(text: str | None) -> tuple[str, int] | None:
    """First matching rule -> (reason, days_offset); no match -> None. Pure; covered by smoke."""
    if not text:
        return None
    for pattern, reason, days in _RETRIGGER_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return (reason, days)
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _instantly_lookup(emails: list[str]) -> dict[str, dict]:
    """Per-lead engagement from Instantly, keyed by lowercased email.

    Returns {email: {"opened": bool, "bounced": bool, "replied": bool}}. {} on
    any failure or no Instantly key — uses the shared Instantly client.
    """
    try:
        return instantly.lead_outcomes(emails)
    except Exception:
        return {}


def _classify_instantly(rec: dict, last_contacted_at: str | None) -> str | None:
    """Map Instantly per-lead engagement -> outcome string or None (not terminal yet).

      bounced                                  -> 'bounce'
      replied                                  -> 'reply'
      opened (no reply)                        -> 'open_no_reply'
      sent, not opened, AND >48h since last_contacted_at -> 'no_open'
      else (<48h with no open) -> None  (poll again later)
    """
    if rec.get("bounced"):
        return "bounce"
    if rec.get("replied"):
        return "reply"
    if rec.get("opened"):
        return "open_no_reply"  # opened but no reply seen -> open_no_reply
    # delivered + not opened: only terminal once 48h have elapsed
    if last_contacted_at:
        try:
            sent = datetime.fromisoformat(
                last_contacted_at.replace("Z", "+00:00")
            )
            age_h = (datetime.now(timezone.utc) - sent).total_seconds() / 3600.0
            if age_h >= 48:
                return "no_open"
        except Exception:
            return None
    return None  # no send time or <48h -> wait, poll again


def _should_write(existing_outcome: str | None, new_outcome: str) -> bool:
    """Outcome precedence / idempotency. Returns True iff new_outcome should overwrite existing.

    Rules:
      no existing -> write.
      'reply' always wins (overwrites any non-reply).
      An existing 'reply' is never overwritten.
      Otherwise (existing non-reply, new non-reply) -> do NOT overwrite
      (first terminal non-reply outcome is sticky).
    """
    if existing_outcome is None or existing_outcome == "":
        return True            # first terminal outcome
    if existing_outcome == "reply":
        return False           # reply is sticky — never overwritten
    if new_outcome == "reply":
        return True            # reply overrides any non-reply
    return False               # existing non-reply is sticky vs another non-reply


def _poll_instantly(limit: int) -> dict:
    """Source A. Poll Instantly analytics and classify leads handed to Instantly.

    Returns {"updated": int, "by_outcome": {bounce:int, no_open:int, open_no_reply:int}}.
    No-op (empty) if no Instantly key or no Supabase. Never raises.
    """
    updated = 0
    by_outcome: dict[str, int] = {}

    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return {"updated": updated, "by_outcome": by_outcome}

    try:
        rows = supabase.select(
            "leads",
            {"instantly_lead_id": "not.is.null", "outcome": "is.null"},
            columns="id,email,instantly_lead_id,last_contacted_at,outcome",
            order="last_contacted_at.asc",
            limit=limit,
        )
    except Exception:
        return {"updated": updated, "by_outcome": by_outcome}

    emails = [r["email"] for r in rows if r.get("email")]
    if not emails:
        return {"updated": updated, "by_outcome": by_outcome}

    events = _instantly_lookup(emails)

    for lead in rows:
        try:
            rec = events.get((lead.get("email") or "").strip().lower())
            if rec is None:
                continue  # not surfaced yet
            new = _classify_instantly(rec, lead.get("last_contacted_at"))
            if new is None:
                continue
            if not _should_write(lead.get("outcome"), new):
                continue
            supabase.update(
                "leads",
                {"id": f"eq.{lead['id']}"},
                {"outcome": new, "outcome_at": _now_iso()},
            )
            if lead.get("id"):
                try:
                    supabase.insert(
                        "activity",
                        {
                            "lead_id": lead["id"],
                            "type": "outcome",
                            "payload": {
                                "outcome": new,
                                "source": "instantly_analytics",
                            },
                        },
                    )
                except Exception:
                    pass
            updated += 1
            by_outcome[new] = by_outcome.get(new, 0) + 1
        except Exception:
            pass

    return {"updated": updated, "by_outcome": by_outcome}


def _drain_inbound(limit: int) -> dict:
    """Source B. Drain unprocessed inbound_email_events, match to leads, set outcome='reply'.

    Returns {"replies": int}. No-op if no Supabase. Never raises.
    """
    replies = 0

    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return {"replies": replies}

    try:
        events = supabase.select(
            "inbound_email_events",
            {"processed": "eq.false"},
            columns="id,from_email,subject,payload",
            order="received_at.asc",
            limit=limit,
        )
    except Exception:
        return {"replies": replies}

    if not events:
        return {"replies": replies}

    for event in events:
        try:
            frm = (event.get("from_email") or "").strip().lower()
            if not frm:
                # mark processed even if no from_email so we don't re-scan forever
                try:
                    supabase.update(
                        "inbound_email_events",
                        {"id": f"eq.{event['id']}"},
                        {"processed": True},
                    )
                except Exception:
                    pass
                continue

            matched = supabase.select(
                "leads",
                {"email": f"eq.{frm}"},
                columns="id,email,outcome,name,company,title,icp_score",
                limit=1,
            )

            if matched:
                lead = matched[0]
                if _should_write(lead.get("outcome"), "reply"):
                    # A reply is the hottest event in the funnel. Action it HERE,
                    # at detection — flip to the terminal, sequence-stopping
                    # stage AND notify a human now — instead of waiting for the
                    # daily flow (which would delay the handoff by up to ~24h).

                    # Reply body / subject for classifiers
                    reply_text = (
                        (event.get("payload") or {}).get("text")
                        or event.get("subject")
                        or ""
                    )
                    reply_subject = event.get("subject") or ""

                    # Classify the reply intent (Task 1)
                    reply_class = _classify_reply(reply_subject, reply_text)

                    # unsubscribe → flip outcome to 'unsubscribe' so dedup
                    # permanently suppresses (config.SUPPRESS_OUTCOMES includes it)
                    outcome_value = "unsubscribe" if reply_class == "unsubscribe" else "reply"

                    # Re-trigger classifier: detect deferral intent (ooo / not_now)
                    retrigger = classify_retrigger(reply_text)

                    update_payload: dict = {
                        "outcome": outcome_value,
                        "outcome_at": _now_iso(),
                        "stage": "replied",
                        "reply_class": reply_class,
                    }
                    if retrigger:
                        reason, days = retrigger
                        update_payload["re_trigger_reason"] = reason
                        update_payload["re_trigger_at"] = (
                            datetime.now(timezone.utc) + timedelta(days=days)
                        ).isoformat()

                    supabase.update(
                        "leads",
                        {"id": f"eq.{lead['id']}"},
                        update_payload,
                    )
                    if lead.get("id"):
                        try:
                            supabase.insert(
                                "activity",
                                {
                                    "lead_id": lead["id"],
                                    "type": "outcome",
                                    "payload": {
                                        "outcome": outcome_value,
                                        "reply_class": reply_class,
                                        "source": "instantly_reply",
                                    },
                                },
                            )
                        except Exception:
                            pass
                    # Event-driven handoff — shoot it straight to Slack.
                    # For 'interested' replies, use a dedicated highlight alert.
                    try:
                        if reply_class == "interested":
                            slack.post_interested_reply(lead)
                        elif reply_class != "unsubscribe":
                            slack.post_reply(lead)
                    except Exception:
                        pass
                    replies += 1
                supabase.update(
                    "inbound_email_events",
                    {"id": f"eq.{event['id']}"},
                    {"matched_lead": lead["id"], "processed": True},
                )
            else:
                # No lead matched — drain anyway so we don't re-scan forever
                supabase.update(
                    "inbound_email_events",
                    {"id": f"eq.{event['id']}"},
                    {"processed": True},
                )
        except Exception:
            pass

    return {"replies": replies}


def main(limit: int = 500) -> dict:
    """Poll outcomes from Instantly analytics + inbound_email_events. Returns a summary dict.

    Keyless/smoke-safe: returns the empty summary without network if creds absent. Never raises.

    Ordering note: _drain_inbound (replies) runs AFTER _poll_instantly. Because _should_write
    lets 'reply' overwrite any non-reply, order is not strictly required for correctness, but
    draining replies last guarantees a reply observed in the same run wins even if Instantly
    also reported 'open_no_reply' for that lead.
    """
    empty_summary = {
        "instantly": {"updated": 0, "by_outcome": {}},
        "inbound": {"replies": 0},
    }

    if not config.ENABLE_FEEDBACK_LOOP:
        summary = dict(empty_summary)
        summary["skipped"] = "disabled"
        try:
            common_log.log_stage("feedback/outcomes", summary)
        except Exception:
            pass
        return summary

    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        summary = dict(empty_summary)
        summary["skipped"] = "no supabase"
        try:
            common_log.log_stage("feedback/outcomes", summary)
        except Exception:
            pass
        return summary

    try:
        inst = _poll_instantly(limit)
    except Exception:
        inst = {"updated": 0, "by_outcome": {}}

    # Drain replies AFTER Instantly so reply overrides open_no_reply in the same run
    try:
        inb = _drain_inbound(limit)
    except Exception:
        inb = {"replies": 0}

    summary = {"instantly": inst, "inbound": inb}

    try:
        common_log.log_stage("feedback/outcomes", summary)
    except Exception:
        pass

    return summary


if __name__ == "__main__":
    from datetime import timedelta
    import json

    print("=== feedback/outcomes.py smoke (no keys, no network) ===")

    # 0. classify_retrigger pure-function tests (Area 4 ADD 3)
    cases_rt = [
        ("Let's circle back next quarter", "next_quarter", 90),
        ("not now, maybe later", "later", 30),
        ("yes, book it", None, None),
        ("reach out again in a few weeks", "later", 30),
        ("after the round we can talk", "after_round", 45),
        (None, None, None),
    ]
    for text, exp_reason, exp_days in cases_rt:
        got = classify_retrigger(text)
        if exp_reason is None:
            assert got is None, f"expected None got {got!r} for {text!r}"
            print(f"PASS: classify_retrigger({text!r}) -> None")
        else:
            assert got is not None and got[0] == exp_reason and got[1] == exp_days, \
                f"expected ({exp_reason},{exp_days}) got {got!r} for {text!r}"
            print(f"PASS: classify_retrigger({text!r}) -> {got}")

    # 1. Keyless main() — ENABLE_FEEDBACK_LOOP defaults to false, so the flag
    #    gate fires first ("disabled"); with the flag on but no creds it would
    #    short-circuit on "no supabase". Either is a valid skipped state.
    result = main()
    print("main() keyless:", json.dumps(result, default=str))
    assert result.get("skipped") in ("disabled", "no supabase"), f"expected skipped, got {result}"
    assert result["instantly"]["updated"] == 0
    assert result["inbound"]["replies"] == 0
    print("PASS: main() keyless returns empty summary")

    # 2. Pure helper: _classify_instantly (rec = {opened,bounced,replied})
    cases = [
        # (rec, last_contacted_at, expected, label)
        ({"bounced": True}, None, "bounce", "bounced"),
        ({"replied": True}, None, "reply", "replied"),
        ({"opened": True}, "2026-01-01T00:00:00+00:00", "open_no_reply", "opened"),
        # delivered, not opened, >48h ago: no_open
        (
            {"opened": False},
            (datetime.now(timezone.utc) - timedelta(hours=70)).isoformat(),
            "no_open",
            "no-open >48h",
        ),
        # delivered, not opened, <2h ago: None (wait)
        (
            {"opened": False},
            (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            None,
            "no-open <2h",
        ),
        # empty record: None
        ({}, None, None, "empty"),
    ]
    for rec, lca, expected, label in cases:
        got = _classify_instantly(rec, lca)
        status = "PASS" if got == expected else "FAIL"
        print(f"{status}: _classify_instantly({label}) -> {got!r} (expected {expected!r})")

    # 3. Pure helper: _should_write
    sw_cases = [
        (None, "bounce", True),
        ("", "bounce", True),
        ("reply", "bounce", False),
        ("bounce", "reply", True),
        ("bounce", "no_open", False),
        ("no_open", "open_no_reply", False),
    ]
    for existing, new, expected in sw_cases:
        got = _should_write(existing, new)
        status = "PASS" if got == expected else "FAIL"
        print(f"{status}: _should_write({existing!r}, {new!r}) -> {got!r} (expected {expected!r})")

    print("=== smoke complete ===")
