"""score/priority.py — intent_score + priority. Funding recency x round-size dominate.

Pure function; always runs (keyless). Loads relevance weights + tau from scoring_weights
(Supabase) when available, else config defaults. Never raises.

Run AFTER icp.main (icp_score is set) and BEFORE dedup.main.
Leaves icp_score/icp_tier untouched — rubric still owns those.
reply_prob stays SHELVED — do NOT import/wire score/feedback.predict.

Node-envelope note: folds into the score node group; Step 3 records
intent_score/priority into node_runs.qa_checks.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from scripts.common import config, log

_DEFAULT_RELEVANCE: dict[str, float] = {
    # funding > job_change > headcount > hiring > tech ; others 'none'
    "funding": 1.0,
    "job_change": 0.7,
    "headcount": 0.55,
    "hiring": 0.45,
    "tech": 0.35,
    "launch": 0.4,
    "other": 0.2,
    "none": 0.1,
}


def _parse_relevance_override(raw: str) -> dict[str, float]:
    """Parse RELEVANCE_WEIGHTS csv 'k:v,k2:v2' into a float dict. Silently ignores bad entries."""
    out: dict[str, float] = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" not in item:
            continue
        key, _, val = item.partition(":")
        try:
            out[key.strip()] = float(val.strip())
        except (TypeError, ValueError):
            pass
    return out


def _load_weights() -> dict[str, Any]:
    """Active scoring_weights row -> {relevance{...}, tau_days, w_recency, w_size, w_relevance}.

    No Supabase / no active row -> config defaults (PRIORITY_TAU_DAYS, PRIORITY_W_*,
    _DEFAULT_RELEVANCE merged with config.RELEVANCE_WEIGHTS override). Never raises.
    """
    defaults = {
        "relevance": dict(_DEFAULT_RELEVANCE),
        "tau_days": config.PRIORITY_TAU_DAYS,
        "w_recency": config.PRIORITY_W_RECENCY,
        "w_size": config.PRIORITY_W_SIZE,
        "w_relevance": config.PRIORITY_W_RELEVANCE,
    }

    # Apply config-level relevance overrides (csv env var)
    if config.RELEVANCE_WEIGHTS:
        overrides = _parse_relevance_override(config.RELEVANCE_WEIGHTS)
        defaults["relevance"].update(overrides)

    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return defaults

    try:
        from scripts.common import supabase
        rows = supabase.select(
            "scoring_weights",
            {"is_active": "eq.true"},
            columns="weights",
            limit=1,
        )
        if not rows:
            return defaults
        weights = rows[0].get("weights") or {}
        if not isinstance(weights, dict):
            return defaults

        # Merge DB row into defaults (additive — absent keys fall back to defaults)
        result = dict(defaults)
        result["tau_days"] = float(weights.get("tau_days", defaults["tau_days"]))
        result["w_recency"] = float(weights.get("w_recency", defaults["w_recency"]))
        result["w_size"] = float(weights.get("w_size", defaults["w_size"]))
        result["w_relevance"] = float(weights.get("w_relevance", defaults["w_relevance"]))
        # Merge relevance sub-dict
        if isinstance(weights.get("relevance"), dict):
            merged_rel = dict(defaults["relevance"])
            merged_rel.update(weights["relevance"])
            result["relevance"] = merged_rel
        return result
    except Exception:
        return defaults


def _recency_decay(signal_ts: Any, tau_days: float) -> float:
    """exp(-age_days / tau). Missing/unparseable ts -> 0.0 (no credit, never raises).
    age clamped >= 0 (future-dated ts -> 1.0)."""
    if not signal_ts:
        return 0.0
    try:
        if isinstance(signal_ts, str):
            ts = datetime.fromisoformat(signal_ts.replace("Z", "+00:00"))
        elif isinstance(signal_ts, datetime):
            ts = signal_ts
        else:
            return 0.0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)
        return math.exp(-age_days / tau_days)
    except Exception:
        return 0.0


def _size_weight(amount_eur: Any) -> float:
    """Log-scaled round-size weight in [0,1]. None/<=0 -> 0.0.

    w = min(1.0, log10(max(amount_eur,1)) / log10(config.PRIORITY_SIZE_CAP_EUR)).
    Default cap 50_000_000 -> €50M ~1.0, €5M ~0.86, €500k ~0.65.
    """
    try:
        v = float(amount_eur) if amount_eur is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    cap = max(config.PRIORITY_SIZE_CAP_EUR, 1.0)
    return min(1.0, math.log10(max(v, 1)) / math.log10(cap))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def main(leads: list[dict] | None = None) -> list[dict]:
    """Set lead['intent_score'] (0..1) and lead['priority'] (0..1) on each non-_skip lead.

    intent_score = relevance[signal_type] * recency_decay(signal_ts, tau)
    priority = clamp01(
        w_recency  * recency_decay(signal_ts, tau)
      + w_size     * size_weight(funding_amount_eur or last_funding_eur)
      + w_relevance* relevance[signal_type]
      + 0.10 * (icp_score or 0)/100          # small ICP-fit nudge, never dominates
    )
    Defaults make recency+size dominate: w_recency=0.40, w_size=0.40, w_relevance=0.20.
    signal_ts source order: lead['signal_ts'] -> funding_announced_at -> None.
    Leaves _skip leads untouched. Logs log_stage('score/priority', {scored, avg_priority}).
    """
    if leads is None:
        leads = []

    weights = _load_weights()
    relevance: dict[str, float] = weights["relevance"]
    tau_days: float = weights["tau_days"]
    w_recency: float = weights["w_recency"]
    w_size: float = weights["w_size"]
    w_relevance: float = weights["w_relevance"]

    scored = 0
    total_priority = 0.0

    for lead in leads:
        if lead.get("_skip"):
            continue

        # Signal timestamp: prefer signal_ts, fall back to funding_announced_at
        sig_ts = lead.get("signal_ts") or lead.get("funding_announced_at")

        # Signal type
        sig_type = (lead.get("signal_type") or "none").lower()
        rel_weight = relevance.get(sig_type, relevance.get("other", 0.2))

        # Amount for size scoring
        amount_eur = lead.get("funding_amount_eur") or lead.get("last_funding_eur")

        # Components
        decay = _recency_decay(sig_ts, tau_days)
        size_w = _size_weight(amount_eur)
        icp_nudge = 0.10 * ((lead.get("icp_score") or 0) / 100.0)

        intent_score = _clamp01(rel_weight * decay)
        priority = _clamp01(
            w_recency * decay
            + w_size * size_w
            + w_relevance * rel_weight
            + icp_nudge
        )

        lead["intent_score"] = round(intent_score, 4)
        lead["priority"] = round(priority, 4)
        scored += 1
        total_priority += priority

    avg_priority = round(total_priority / scored, 4) if scored else 0.0

    try:
        log.log_stage("score/priority", {"scored": scored, "avg_priority": avg_priority})
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    from datetime import timedelta
    import json

    print("=== score/priority.py smoke ===")

    now = datetime.now(timezone.utc)
    two_days_ago = (now - timedelta(days=2)).isoformat()
    sixty_days_ago = (now - timedelta(days=60)).isoformat()

    # QA: same lead, €10M round 2d ago has priority > same lead 60d ago
    lead_recent = {
        "signal_type": "funding",
        "signal_ts": two_days_ago,
        "funding_amount_eur": 10_000_000,
        "icp_score": 70,
    }
    lead_old = {
        "signal_type": "funding",
        "signal_ts": sixty_days_ago,
        "funding_amount_eur": 10_000_000,
        "icp_score": 70,
    }
    lead_hiring = {
        "signal_type": "hiring",
        "signal_ts": two_days_ago,
        "funding_amount_eur": None,
        "icp_score": 70,
    }

    main([lead_recent, lead_old, lead_hiring])
    print(f"  recent funding priority: {lead_recent['priority']}")
    print(f"  old funding priority:    {lead_old['priority']}")
    print(f"  hiring (same recency):   {lead_hiring['priority']}")

    assert lead_recent["priority"] > lead_old["priority"], "recent > old"
    assert lead_recent["priority"] > lead_hiring["priority"], "funding > hiring at equal recency"
    assert 0.0 <= lead_recent["intent_score"] <= 1.0
    assert 0.0 <= lead_recent["priority"] <= 1.0
    print("PASS: priority ordering and bounds")

    # _skip leads untouched
    skipped = {"_skip": True, "signal_type": "funding"}
    main([skipped])
    assert "intent_score" not in skipped, "_skip leads must be untouched"
    print("PASS: _skip leads untouched")

    print("PASS: all score/priority.py assertions")
