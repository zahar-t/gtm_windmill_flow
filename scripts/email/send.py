"""email/send.py — Instantly campaign send + warmup gate.

Hands queued emails (stage in {hot, warm}) to Instantly, respecting the warmup
ramp headroom from warmup_check.main() × the postmaster reputation multiplier.

We don't transactional-send a message: each lead is pushed into the Instantly
campaign (config.INSTANTLY_CAMPAIGN_ID) with its AI-written subject/body as
custom variables, and Instantly sends it across warmed, rotated inboxes on its
own schedule — then reports opens/replies/bounces back (webhooks + analytics).
That's the cold-email reputation play SendGrid can't do.

Idempotency + compliance (Step 2):
  - Refuses to send if Instantly is live but the CRM is unreachable (can't verify
    suppression → fail closed).
  - Per lead: skips anything suppressed/_skip; re-checks terminal outcome in the
    CRM; for NEW leads skips anything already sent or in-flight, so a re-run never
    double-sends — even if a prior run's result-persist failed.
  - Claims 'sending' BEFORE the Instantly call, confirms 'sent' after, reverts the
    claim if the send genuinely failed. Follow-ups are exempt from the recency /
    in-flight check (their cadence is gated by lifecycle.py).

On a successful push:
  - Sets lead["stage"] = "contacted", lead["last_contacted_at"] = now_iso
  - Stores lead["instantly_lead_id"] (the send-tracking handle)
  - Persists to Supabase leads table (incl. pipeline_state='sent') + an activity
    row (type=email_sent)
  - Increments email_warmup.sends_count

Smoke-safe: if INSTANTLY_API_KEY / INSTANTLY_CAMPAIGN_ID are empty, skips all
sends and returns leads with _errors flagged. If Supabase creds absent, skip
DB writes.

def main(leads: list[dict] | None = None, reputation: dict | None = None) -> list[dict]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.common import config
from scripts.common import supabase
from scripts.common import log as common_log
from scripts.common import instantly
from scripts.common import node
from scripts.email import warmup_check
from scripts.email import postmaster
from scripts.email import spam_score

_30_DAYS = timedelta(days=30)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _crm_enabled() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_KEY)


def _db_send_state(email: str) -> dict | None:
    """Send-relevant CRM state for this email, or None if the row is absent.
    Returns {"_lookup_failed": True} on error so the caller can fail closed."""
    try:
        rows = supabase.select(
            "leads",
            {"email": f"eq.{email}"},
            columns="id,stage,outcome,last_contacted_at,instantly_lead_id,pipeline_state",
            limit=1,
        )
        return rows[0] if rows else None
    except Exception:
        return {"_lookup_failed": True}


def _already_sent_or_inflight(st: dict) -> bool:
    """True if this email was already sent or is mid-send — robust even if a prior
    run's result-persist failed, because the 'sending' claim still landed."""
    if st.get("instantly_lead_id") or st.get("pipeline_state") in ("sending", "sent"):
        return True
    if st.get("stage") in ("contacted", "replied", "converted"):
        return True
    lc = _parse_iso(st.get("last_contacted_at"))
    return bool(lc and (datetime.now(timezone.utc) - lc) < _30_DAYS)


def _claim(email: str, now_iso: str) -> None:
    """Persist a 'sending' claim BEFORE the Instantly call (upsert so a cold lead
    not yet in the CRM is created). Touches only these columns on conflict."""
    try:
        supabase.upsert(
            "leads",
            {"email": email, "pipeline_state": "sending", "send_claimed_at": now_iso},
            on_conflict="email",
        )
    except Exception:
        pass


def _revert_claim(email: str) -> None:
    """Send genuinely failed → release the claim so a later run can retry."""
    try:
        supabase.update(
            "leads",
            {"email": f"eq.{email}"},
            {"pipeline_state": "copy_qa_passed", "send_claimed_at": None},
        )
    except Exception:
        pass


def _confirm_sent(lead: dict, now_iso: str, instantly_lead_id: str) -> None:
    """Persist a completed send (lead row incl. pipeline_state='sent' + activity).
    Best-effort; never raises."""
    # company_size may be "120", 120, "50-200", or None — store only a clean int
    raw_size = lead.get("company_size")
    try:
        size_val = int(raw_size) if raw_size not in (None, "") else None
    except (TypeError, ValueError):
        size_val = None

    update_values = {
        "last_contacted_at": now_iso,
        "stage": "contacted",
        "pipeline_state": "sent",
        "updated_at": now_iso,
        "signal_type": lead.get("signal_type"),
        "company_size": size_val,
        "sequence": lead.get("sequence"),
        "spam_score": lead.get("spam_score"),
        "sequence_step": lead.get("sequence_step") or 1,
    }
    if instantly_lead_id:
        update_values["instantly_lead_id"] = instantly_lead_id

    try:
        supabase.update("leads", {"email": f"eq.{lead['email']}"}, update_values)
    except Exception:
        pass

    if lead.get("id"):
        try:
            supabase.insert(
                "activity",
                {
                    "lead_id": lead["id"],
                    "type": "email_sent",
                    "payload": {
                        "subject": lead.get("email_subject"),
                        "sequence": lead.get("sequence"),
                        "score": lead.get("icp_score"),
                        "spam_score": lead.get("spam_score"),
                    },
                },
            )
        except Exception:
            pass


def _send_one(lead: dict) -> tuple[bool, str]:
    """Push one lead into the Instantly campaign. Returns (success, instantly_lead_id).

    instantly_lead_id is Instantly's id for the created campaign lead — our handle
    for matching engagement events back later ("" if the push failed).
    """
    if not config.INSTANTLY_API_KEY or not config.INSTANTLY_CAMPAIGN_ID:
        lead.setdefault("_errors", []).append("no instantly key/campaign")
        return False, ""

    to_email: str = lead.get("email") or ""
    plain_body: str = lead.get("email_body") or ""

    if not to_email or not plain_body:
        lead.setdefault("_errors", []).append("missing email or body — skip send")
        return False, ""

    try:
        # Hand the lead + personalized subject/body to Instantly; it sends on its
        # own warmed schedule and tracks opens/replies/bounces natively (no pixel).
        lead_id = instantly.add_lead_to_campaign(lead)
        if lead_id:
            return True, str(lead_id)
        lead.setdefault("_errors", []).append("instantly: lead not created")
        return False, ""
    except Exception as exc:
        lead.setdefault("_errors", []).append(f"instantly error: {exc}")
        return False, ""


def main(leads: list[dict] | None = None, reputation: dict | None = None) -> list[dict]:
    """Send emails to hot/warm leads up to the warmup headroom.

    Parameters
    ----------
    leads:
        List of lead dicts from the canonical lead-dict contract. None → [].
    reputation:
        Pre-read reputation verdict (from the reputation_read node /
        postmaster.latest_verdict()). When None, send reads the latest snapshot
        itself — so send stays correct whether or not the flow wires the gate.

    Returns
    -------
    The same list with stage/last_contacted_at updated for sent leads.
    """
    if leads is None:
        leads = []

    # Compliance fail-closed: if we CAN send (Instantly live) but CANNOT reach the
    # CRM to verify suppression, refuse the whole batch rather than risk emailing a
    # suppressed/unsubscribed contact. (Keyless smoke: Instantly absent → no-op.)
    if config.INSTANTLY_API_KEY and not _crm_enabled():
        for lead in leads:
            lead.setdefault("_errors", []).append(
                "send refused: CRM unreachable — cannot verify suppression"
            )
        try:
            common_log.log_stage("email/send", {"refused": "instantly_live_no_crm", "sent": 0})
        except Exception:
            pass
        return leads

    # 1. Get today's warmup headroom
    state = warmup_check.main()
    warmup_remaining: int = state["remaining"]

    # 1b. Postmaster reputation gate — throttle (×0.5/×0.25) or pause (×0) the
    #     day's send based on domain/IP reputation. Reads the latest snapshot
    #     (cheap) rather than a live API call; no telemetry → ×1.0 (warmup only).
    if reputation is None:
        reputation = postmaster.latest_verdict()
    multiplier: float = reputation.get("send_multiplier", 1.0)
    remaining: int = int(warmup_remaining * multiplier)

    # Sendable: stage in {hot, warm}, non-empty email AND non-empty email_body
    sendable_stages = {"hot", "warm"}

    sent = 0
    skipped = 0
    held = 0

    # Re-rank: highest-priority first so a late high-priority lead is never starved
    # by earlier low-priority ones hitting the cap first (stable sort preserves
    # arrival order on ties — the priority flywheel realized at the send chokepoint).
    for lead in sorted(leads, key=lambda l: -(l.get("priority") or 0.0)):
        if lead.get("stage") not in sendable_stages:
            skipped += 1
            continue

        # Channel router: only 'email'-channel leads send cold email. investor_intro is
        # handled by crm/handoff (Slack intro ask); linkedin is a Unipile touch/queue.
        # Future plug-in: scripts/outreach/linkedin_touch.py (Unipile DM) for linkedin channel.
        ch = lead.get("channel")
        if ch in ("investor_intro", "linkedin"):
            lead.setdefault("_errors", []).append(f"skip cold email: channel={ch}")
            skipped += 1
            continue

        if not lead.get("email") or not lead.get("email_body"):
            skipped += 1
            continue

        # Spam guard: skip anything the spam scorer held. Fail-safe — if the
        # spam_score stage wasn't run upstream, evaluate inline now so a spammy
        # email can never reach the sender (Instantly) and burn domain reputation.
        if lead.get("spam_score") is None:
            v = spam_score.evaluate(lead.get("email_subject"), lead.get("email_body"))
            lead["spam_score"] = v["score"]
            lead["spam_verdict"] = v["verdict"]
            if v["verdict"] == "block":
                lead["_hold"] = "spam_risk"
                lead["held_reason"] = f"spam_score={v['score']}"
        if lead.get("_hold"):
            lead.setdefault("_errors", []).append(
                f"held: {lead.get('held_reason') or lead['_hold']}"
            )
            held += 1
            continue

        # Compliance gate (covers new + follow-up leads at the send chokepoint).
        if lead.get("suppressed") or lead.get("_skip"):
            lead.setdefault("_errors", []).append(
                f"suppressed: {lead.get('_skip_reason') or 'suppressed'}"
            )
            skipped += 1
            continue

        if sent >= remaining:
            # Cap hit (warmup ramp × reputation multiplier) — defer the rest
            lead.setdefault("_errors", []).append("send cap reached")
            skipped += 1
            continue

        email = lead["email"]
        is_followup = bool(lead.get("_followup"))

        # Cross-run idempotency + suppression re-check via the CRM. Follow-ups are
        # exempt from the recency / in-flight check: their cadence is gated by
        # lifecycle.py, so the <30d rule must NOT block a legitimate re-touch.
        if _crm_enabled():
            st = _db_send_state(email)
            if st and st.get("_lookup_failed"):
                lead.setdefault("_errors", []).append("send: state lookup failed — skip (fail closed)")
                skipped += 1
                continue
            if st and (st.get("outcome") or "").lower() in config.SUPPRESS_OUTCOMES:
                lead.setdefault("_errors", []).append("suppressed: terminal outcome")
                skipped += 1
                continue
            if st and not is_followup and _already_sent_or_inflight(st):
                lead.setdefault("_errors", []).append("already sent/in-flight — skip (idempotent)")
                lead["stage"] = "contacted"
                skipped += 1
                continue

        now_iso = _now_iso()
        if _crm_enabled():
            _claim(email, now_iso)            # claim 'sending' BEFORE the Instantly call

        # 2. Send (push to Instantly campaign)
        success, instantly_lead_id = _send_one(lead)

        if success:
            lead["last_contacted_at"] = now_iso
            lead["stage"] = "contacted"
            lead["instantly_lead_id"] = instantly_lead_id
            lead["pipeline_state"] = "sent"
            sent += 1
            if _crm_enabled():
                _confirm_sent(lead, now_iso, instantly_lead_id)
            node.record_run("email/send", lead, node.STATUS_PASSED)        # QA evidence
        elif _crm_enabled():
            _revert_claim(email)              # release the claim so a retry can happen
            node.dead_letter("email/send", node.SEND_FAILED, lead,         # QA evidence
                             detail="; ".join(lead.get("_errors", [])[-3:]) or "send failed")
            node.record_run("email/send", lead, node.STATUS_QUARANTINED)   # QA evidence

    # 4. Bump warmup sends_count in Supabase
    if sent > 0 and config.SUPABASE_URL and config.SUPABASE_KEY:
        try:
            supabase.update(
                "email_warmup",
                {"date": f"eq.{state['date']}"},
                {"sends_count": state["sends_count"] + sent},
            )
        except Exception:
            pass

    summary = {
        "sent": sent,
        "skipped": skipped,
        "held_spam": held,
        "remaining_after": max(0, remaining - sent),
        "warmup_remaining": warmup_remaining,
        "reputation": reputation.get("status"),
        "send_multiplier": multiplier,
    }

    try:
        common_log.log_stage("email/send", summary)
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    print("=== send.py smoke (no keys, no network) ===")
    # Fictional fixture only — no real data
    fixture = [
        {
            "email": "pat@acme.example",
            "name": "Pat Doe",
            "company": "Acme Cloud",
            "company_url": "https://acme.example",
            "domain": "acme.example",
            "source": "exa_web_search",
            "stage": "hot",
            "email_subject": "Saw Acme Cloud's Series B — congrats",
            "email_body": (
                "Congrats on the Series B, Pat — saw the announcement. "
                "Would love 15 mins to show how GTM Engine helps scale outbound "
                "during hyper-growth. Thursday work?"
            ),
            "sequence": "A",
            "icp_score": 82,
            "signal": "Acme Cloud raises $40M Series B",
            "id": None,
            "_errors": [],
        },
        {
            "email": None,
            "name": "Robin Smith",
            "company": "Beta SaaS",
            "company_url": "https://betasaas.example",
            "domain": "betasaas.example",
            "source": "exa_web_search",
            "stage": "warm",
            "email_subject": None,
            "email_body": None,
            "sequence": "B",
            "icp_score": 55,
            "signal": None,
            "id": None,
            "_errors": [],
        },
        {
            "email": "alex@cold.example",
            "name": "Alex Cold",
            "company": "Cold Corp",
            "company_url": "https://cold.example",
            "domain": "cold.example",
            "source": "exa_web_search",
            "stage": "cold",
            "email_subject": None,
            "email_body": None,
            "sequence": None,
            "icp_score": 20,
            "signal": None,
            "id": None,
            "_errors": [],
        },
    ]

    result = main(fixture)
    print(json.dumps(result, default=str)[:2000])
    print()
    print(f"Lead 0 stage={result[0]['stage']} errors={result[0]['_errors']}")
    print(f"Lead 1 stage={result[1]['stage']} errors={result[1]['_errors']}")
    print(f"Lead 2 stage={result[2]['stage']} errors={result[2]['_errors']}")
