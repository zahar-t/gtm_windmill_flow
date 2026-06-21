"""common/node.py — the node envelope: QA evidence + dead-letter quarantine.

Every gate in the pipeline calls these helpers to (a) record a per-node run into
node_runs (QA evidence) and (b) quarantine a gate-failing record into dead_letter
(never drop, never silently pass downstream). All writes are BEST-EFFORT:
  - no-op when Supabase creds are absent (keyless smoke / demo),
  - wrapped in try/except — a logging/DLQ failure must NEVER break the pipeline.

run_id() uses uuid deliberately: this is normal runtime glue (a process-stable
correlation id for one pipeline run), not a Windmill workflow step, so the
non-determinism is fine here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from scripts.common import config
from scripts.common import log as common_log

# Guarded import — smoke must work with no creds.
try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False

# QAResult is optional input to record_run; import is cheap and pure.
try:
    from scripts.common.contracts import QAResult  # noqa: F401  (typing/d/duck use)
except Exception:
    QAResult = None  # type: ignore[assignment]

# --- node_runs.status values ---
STATUS_PASSED      = "passed"
STATUS_QUARANTINED = "quarantined"
STATUS_SKIPPED     = "skipped"

# --- dead_letter.reason_code taxonomy ---
# Intake
NO_IDENTITY        = "no_identity"        # produced lead has no usable identity
# Enrichment (DEFERRED to Step 5 — see §6; constant defined now for API stability)
ENRICH_INCOMPLETE  = "enrich_incomplete"
# CRM dedup  (matches dedup.py _skip_reason prefix "dedup_unverified")
DEDUP_UNVERIFIED   = "dedup_unverified"
# Email validate (matches validate.py _skip_reason prefix "email_invalid")
EMAIL_INVALID      = "email_invalid"
# Spam gate (spam_score.py sets _hold='spam_risk'; reason_code is the gate name)
SPAM_BLOCK         = "spam_block"
# Send
SEND_FAILED        = "send_failed"
# Scoring
SCORE_OUT_OF_RANGE = "score_out_of_range"
EXTRACT_FAILED     = "extract_failed"     # icp extractor / rubric raised

_RUN_ID: str | None = None


def run_id() -> str:
    """Process-stable correlation id for one pipeline run (uuid4 hex, cached).

    Fine to use uuid here — runtime glue, not a deterministic workflow step.
    """
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = uuid.uuid4().hex
    return _RUN_ID


# Alias so dead_letter/record_run can call it even though their `run_id` PARAMETER
# shadows the function name inside their bodies. (See §6 risk #6.)
_run_id_fn = run_id


def _creds() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_KEY
                and _SUPABASE_OK and _supabase is not None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Keys dropped from the dead_letter payload snapshot: bulky / internal / volatile.
_PAYLOAD_DROP = {
    "_linkedin",        # large scraped profile blob (icp.py:85 reads it)
    "_icp_inputs",      # derived debug dict (icp.py:151)
    "tech_stack",       # can be a long list from company enrich
    "spam_flags",       # verbose rule list — summarized via `detail` instead
    "email_body",       # full draft body — not needed to triage, can be long
}


def _snapshot(lead: dict | None) -> dict:
    """A TRIMMED, JSON-safe lead snapshot for dead_letter.payload. Never raises."""
    if not isinstance(lead, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in lead.items():
        if k in _PAYLOAD_DROP:
            continue
        try:
            if v is None or isinstance(v, (str, int, float, bool, list, dict)):
                out[k] = v
            else:
                out[k] = str(v)
        except Exception:
            continue
    return out


def _lead_id(lead: dict | None) -> str | None:
    """String lead id, or None (pre-id intake failures)."""
    if not isinstance(lead, dict):
        return None
    lid = lead.get("id")
    return str(lid) if lid else None


def has_identity(lead: dict) -> bool:
    """A lead is routable iff it has email OR linkedin_url OR (domain AND company)."""
    if not isinstance(lead, dict):
        return False
    return bool(
        lead.get("email")
        or lead.get("linkedin_url")
        or (lead.get("domain") and lead.get("company"))
    )


def dead_letter(
    node: str,
    reason_code: str,
    lead: dict | None,
    detail: str | None = None,
    run_id: str | None = None,
) -> None:
    """Quarantine a gate-failing record into dead_letter (best-effort, never raises).

    Upsert on UNIQUE(lead_id, node, reason_code): conflict -> attempts+1, last_seen=now.
    payload is a TRIMMED lead snapshot. lead_id may be None (pre-id intake failures).
    Also writes a log_stage breadcrumb. No-op without Supabase creds.
    """
    # Local breadcrumb even when keyless (cheap; swallows errors).
    try:
        common_log.log_stage(
            "dead_letter",
            {"node": node, "reason_code": reason_code,
             "lead_id": _lead_id(lead), "detail": (detail or "")[:200]},
        )
    except Exception:
        pass

    if not _creds():
        return
    try:
        rid = run_id or _run_id_fn()
        lid = _lead_id(lead)
        # find existing row for the three-tuple to compute attempts
        attempts = 1
        try:
            filters = {"node": f"eq.{node}", "reason_code": f"eq.{reason_code}"}
            filters["lead_id"] = f"eq.{lid}" if lid else "is.null"
            existing = _supabase.select("dead_letter", filters,
                                        columns="attempts", limit=1)
            if existing:
                attempts = int(existing[0].get("attempts") or 1) + 1
        except Exception:
            attempts = 1
        row = {
            "run_id": rid,
            "lead_id": lid,
            "node": node,
            "reason_code": reason_code,
            "reason_detail": (detail or None),
            "payload": _snapshot(lead),
            "attempts": attempts,
            "last_seen": _now_iso(),
        }
        _supabase.upsert("dead_letter", row, on_conflict="lead_id,node,reason_code")
    except Exception:
        pass


def record_run(
    node: str,
    lead: dict | None,
    status: str,                 # STATUS_PASSED | STATUS_QUARANTINED | STATUS_SKIPPED
    qa=None,                     # optional QAResult (or duck-typed)
    run_id: str | None = None,
) -> None:
    """Insert one node_runs row (QA evidence). Best-effort, no-op keyless, never raises."""
    if not _creds():
        return
    try:
        rid = run_id or _run_id_fn()
        qa_score = None
        qa_checks = None
        if qa is not None:
            qa_score = getattr(qa, "score", None)
            checks = getattr(qa, "checks", None)
            if checks is not None:
                try:
                    qa_checks = [
                        c.model_dump() if hasattr(c, "model_dump") else dict(c)
                        for c in checks
                    ]
                except Exception:
                    qa_checks = None
        row = {
            "run_id": rid,
            "lead_id": _lead_id(lead),
            "node": node,
            "status": status,
            "qa_score": qa_score,
            "qa_checks": qa_checks,
        }
        _supabase.insert("node_runs", row)
    except Exception:
        pass


def quarantine(
    lead: dict,
    node: str,
    reason_code: str,
    detail: str | None = None,
    run_id: str | None = None,
) -> None:
    """Fail a gate: mark the lead skipped+quarantined IN-DICT, then DLQ + record_run.

    Sets in the lead dict (so _skip-honoring downstream nodes stop processing it):
      lead["_skip"] = True
      lead["_skip_reason"] = reason_code     (setdefault — preserves a richer existing string)
      lead["pipeline_state"] = "quarantined" (matches contracts.PipelineState.quarantined)
    Then best-effort dead_letter(...) + record_run(status="quarantined"). Never raises.
    """
    try:
        if isinstance(lead, dict):
            lead["_skip"] = True
            lead.setdefault("_skip_reason", reason_code)
            lead["pipeline_state"] = "quarantined"
    except Exception:
        pass
    dead_letter(node, reason_code, lead, detail=detail, run_id=run_id)
    record_run(node, lead, STATUS_QUARANTINED, run_id=run_id)


def alert_dead_letter(run_id: str | None = None) -> None:
    """Count unresolved dead_letter rows and post a Slack alert if any exist.

    Creds-gated: no-op without Supabase or Slack. Best-effort, never raises.
    Called once at the end of run_pipeline.main() after the report.
    """
    if not _creds():
        return
    try:
        from scripts.common import slack as _slack
        rows = _supabase.select(
            "dead_letter",
            {"resolved": "eq.false"},
            columns="id,node,reason_code",
            limit=500,
        ) or []
        if not rows:
            return
        count = len(rows)
        # Summarise by node
        by_node: dict[str, int] = {}
        for r in rows:
            n = r.get("node") or "unknown"
            by_node[n] = by_node.get(n, 0) + 1
        node_lines = "  ".join(f"{n}:{c}" for n, c in sorted(by_node.items()))
        rid = run_id or _run_id_fn()
        _slack.post(
            f":warning: *Dead-letter queue: {count} unresolved row(s)*\n"
            f"run_id: `{rid}`\n"
            f"by node: {node_lines}\n"
            "Investigate via Supabase → dead_letter WHERE resolved = false"
        )
    except Exception:
        pass


if __name__ == "__main__":
    print("node.py smoke (keyless → all DB writes no-op):")
    print(f"  run_id() stable: {run_id() == run_id()}")
    assert has_identity({"email": "a@b.c"})
    assert has_identity({"domain": "x.com", "company": "X"})
    assert not has_identity({"company": "X"})           # company alone is not enough
    dead_letter("intake/test", NO_IDENTITY, {"company": "X", "_linkedin": {"big": 1}})
    record_run("intake/test", {"id": None, "company": "X"}, STATUS_SKIPPED)
    ld = {"company": "X"}   # no identity → exactly what NO_IDENTITY quarantines
    quarantine(ld, "intake/test", NO_IDENTITY, detail="no email/linkedin/domain")
    assert ld["_skip"] is True and ld["pipeline_state"] == "quarantined"
    print("  PASS: keyless no-op writes; has_identity correct; quarantine mutates dict")
