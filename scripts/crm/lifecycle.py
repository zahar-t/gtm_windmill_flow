"""crm/lifecycle.py — Load existing leads due for a follow-up touch.

The daily flow is acquisition-first (new visitors → one email). Most replies to
cold outbound come from touch 2–3, not touch 1, so a single send leaves the
majority of pipeline value on the table. This node implements multi-touch
nurture: it pulls leads that were already contacted, haven't replied/bounced,
are past the follow-up gap, and are below the touch cap — and re-injects them
into the send path for their NEXT touch.

Returns canonical lead dicts flagged `_followup=True`, with `sequence_step` set
to the upcoming touch and `stage="warm"` so the existing send gate treats them
as sendable. Drafting happens downstream in personalize/followup.py.

Smoke-safe: no Supabase creds → []. Never raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scripts.common import config, log

try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False

# Outcomes that permanently end nurture (never follow up on these).
_TERMINAL = {"reply", "bounce", "unsubscribe"}


def main(limit: int = 200) -> list[dict[str, Any]]:
    """Return follow-up-due leads + due re-triggers as canonical lead dicts. Never raises.

    Two queries:
      (a) Standard follow-up: contacted leads past gap, below touch cap, no terminal outcome.
      (b) Re-trigger re-injection: replied leads where re_trigger_at is now due.
          These BYPASS the terminal outcome filter — they are intentional re-engagements.

    Also applies LinkedIn->email fallback (§4.2): a linkedin-channel lead with no reply
    older than config.LINKEDIN_FALLBACK_DAYS gets channel='email' on the re-injected dict.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_KEY or not _SUPABASE_OK or _supabase is None:
        try:
            log.log_stage("crm/lifecycle", {"due": 0})
        except Exception:
            pass
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.FOLLOWUP_GAP_DAYS)).isoformat()
    linkedin_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=config.LINKEDIN_FALLBACK_DAYS)
    ).isoformat()

    leads: list[dict[str, Any]] = []

    # (a) Standard follow-up query
    try:
        rows = _supabase.select(
            "leads",
            {
                "stage": "eq.contacted",
                "last_contacted_at": f"lt.{cutoff}",
                "sequence_step": f"lt.{config.MAX_TOUCHES}",
            },
            columns=("id,email,name,company,company_url,title,linkedin_url,source,"
                     "signal,signal_type,icp_score,sequence,sequence_step,outcome,"
                     "channel,last_contacted_at"),
            order="last_contacted_at.asc",
            limit=limit,
        )
        for row in rows:
            # Defensive: never follow up on a terminal outcome.
            if (row.get("outcome") or "").lower() in _TERMINAL:
                continue
            step = int(row.get("sequence_step") or 1)

            # LinkedIn -> email fallback: if channel is linkedin and last_contacted_at
            # is older than LINKEDIN_FALLBACK_DAYS with no reply, fall back to email.
            ch = row.get("channel") or "email"
            lca = row.get("last_contacted_at") or ""
            if ch == "linkedin" and lca and lca < linkedin_cutoff:
                ch = "email"

            leads.append({
                "id": row.get("id"),
                "email": row.get("email"),
                "name": row.get("name"),
                "company": row.get("company"),
                "company_url": row.get("company_url"),
                "title": row.get("title"),
                "linkedin_url": row.get("linkedin_url"),
                "source": row.get("source"),
                "signal": row.get("signal"),
                "signal_type": row.get("signal_type"),
                "icp_score": row.get("icp_score"),
                "stage": "warm",            # sendable; copy comes from personalize/followup
                "sequence_step": step + 1,  # the touch we're about to send
                "channel": ch,
                "_followup": True,
                "_errors": [],
            })
    except Exception:
        pass

    # (b) Re-trigger re-injection: replied leads with re_trigger_at now due.
    #     These BYPASS the _TERMINAL filter — intentional re-engagements.
    try:
        due = _supabase.select(
            "leads",
            {"re_trigger_at": f"lte.{now_iso}", "stage": "eq.replied"},
            columns=(
                "id,email,name,company,company_url,title,linkedin_url,source,signal,"
                "signal_type,icp_score,funding_amount_eur,channel,last_contacted_at,"
                "re_trigger_reason,sequence_step"
            ),
            order="re_trigger_at.asc",
            limit=limit,
        ) or []

        for row in due:
            step = int(row.get("sequence_step") or 1)

            # LinkedIn -> email fallback (same rule, applied to re-triggered leads)
            ch = row.get("channel") or "email"
            lca = row.get("last_contacted_at") or ""
            if ch == "linkedin" and lca and lca < linkedin_cutoff:
                ch = "email"

            leads.append({
                "id": row.get("id"),
                "email": row.get("email"),
                "name": row.get("name"),
                "company": row.get("company"),
                "company_url": row.get("company_url"),
                "title": row.get("title"),
                "linkedin_url": row.get("linkedin_url"),
                "source": row.get("source"),
                "signal": row.get("signal"),
                "signal_type": row.get("signal_type"),
                "icp_score": row.get("icp_score"),
                "funding_amount_eur": row.get("funding_amount_eur"),
                "stage": "warm",            # sendable; copy comes from personalize/followup
                "sequence_step": step + 1,
                "channel": ch,
                "re_trigger_reason": row.get("re_trigger_reason"),
                "_retrigger": True,
                "_followup": True,
                "_errors": [],
            })

            # Clear the marker so it isn't re-surfaced on the next run (best-effort)
            try:
                _supabase.update(
                    "leads",
                    {"id": f"eq.{row['id']}"},
                    {"re_trigger_at": None},
                )
            except Exception:
                pass
    except Exception:
        pass

    try:
        log.log_stage("crm/lifecycle", {"due": len(leads)})
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    import json
    print(f"=== lifecycle.py smoke (keyless; gap={config.FOLLOWUP_GAP_DAYS}d, "
          f"max_touches={config.MAX_TOUCHES}) ===")
    out = main()
    print(json.dumps(out, default=str))
    assert out == []
    print("PASS: [] without Supabase creds, no raise")
