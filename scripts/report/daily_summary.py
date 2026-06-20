"""report/daily_summary.py — Compute daily pipeline counts, persist to pipeline_runs, post Slack.

Inserts a pipeline_runs row (if Supabase creds present) and posts a Slack
summary (if SLACK_WEBHOOK_URL present). Always computes and returns counts.

Extended (Area 4): accepts optional `leads` list to compute pipeline_value_eur
and value-segmented metrics (by_funding_bracket, by_segment).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scripts.common import config, log, slack


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def funding_bracket(amount_eur: float | None) -> str:
    """'unknown'|'<1M'|'1-5M'|'5-20M'|'20M+' — deterministic, None-safe.

    Also used by crm/upsert._build_row to populate leads.funding_bracket.
    """
    if amount_eur is None:
        return "unknown"
    try:
        v = float(amount_eur)
    except (TypeError, ValueError):
        return "unknown"
    if v < 1_000_000:
        return "<1M"
    if v < 5_000_000:
        return "1-5M"
    if v < 20_000_000:
        return "5-20M"
    return "20M+"


def _positive(lead: dict) -> bool:
    """A lead is 'positive interest' if outcome=='reply' OR re_trigger_reason set.
    A deferral is still interest — it just needs a re-touch later.
    """
    return (lead.get("outcome") == "reply") or bool(lead.get("re_trigger_reason"))


def _compute_pipeline_value(leads: list[dict]) -> float:
    """Sum deal_value_eur or funding_amount_eur over sendable (hot/warm/contacted) leads."""
    sendable = {"hot", "warm", "contacted"}
    total = 0.0
    for lead in leads:
        if lead.get("stage") in sendable:
            v = lead.get("deal_value_eur") or lead.get("funding_amount_eur") or 0
            try:
                total += float(v)
            except (TypeError, ValueError):
                pass
    return total


def _compute_by_bracket(rows: list[dict]) -> dict[str, Any]:
    """Group rows by funding_bracket, compute reply_rate + positive_rate per bucket."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        b = row.get("funding_bracket") or "unknown"
        buckets.setdefault(b, []).append(row)

    result: dict[str, Any] = {}
    for bracket, items in buckets.items():
        n = len(items)
        replies = sum(1 for r in items if r.get("outcome") == "reply")
        positives = sum(1 for r in items if _positive(r))
        result[bracket] = {
            "n": n,
            "reply_rate": round(replies / n, 3) if n else 0.0,
            "positive_rate": round(positives / n, 3) if n else 0.0,
        }
    return result


def _compute_by_segment(rows: list[dict]) -> dict[str, Any]:
    """Group rows by segment, compute reply_rate + positive_rate per bucket."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        seg = row.get("segment") or "none"
        buckets.setdefault(seg, []).append(row)

    result: dict[str, Any] = {}
    for seg, items in buckets.items():
        n = len(items)
        replies = sum(1 for r in items if r.get("outcome") == "reply")
        positives = sum(1 for r in items if _positive(r))
        result[seg] = {
            "n": n,
            "reply_rate": round(replies / n, 3) if n else 0.0,
            "positive_rate": round(positives / n, 3) if n else 0.0,
        }
    return result


def main(
    found: int = 0,
    enriched: int = 0,
    sent: int = 0,
    queued: int = 0,
    leads: list[dict] | None = None,
) -> dict:
    """Summarise pipeline run counts, persist to pipeline_runs, notify Slack.

    Parameters
    ----------
    found:    Number of leads discovered by intake scripts.
    enriched: Number of leads that completed the enrich stage.
    sent:     Number of emails successfully sent.
    queued:   Number of warm leads queued for nurture sequence.
    leads:    Optional final batch list. When passed, computes pipeline_value_eur
              from sendable leads' deal_value_eur/funding_amount_eur, plus
              value-segmented metrics (requires Supabase for historical rates).

    Returns the counts dict regardless of DB / Slack availability.
    """
    counts: dict[str, Any] = {
        "run_date": _today_iso(),
        "leads_found": found,
        "leads_enriched": enriched,
        "emails_sent": sent,
        "leads_queued": queued,
    }

    # pipeline_value_eur — from passed leads (no DB needed)
    pipeline_value = 0.0
    if leads:
        pipeline_value = _compute_pipeline_value(leads)
    counts["pipeline_value_eur"] = pipeline_value

    # value-segmented metrics — needs Supabase (best-effort)
    by_funding_bracket: dict[str, Any] = {}
    by_segment: dict[str, Any] = {}
    if config.SUPABASE_URL and config.SUPABASE_KEY:
        try:
            from scripts.common import supabase
            outcome_rows = supabase.select(
                "leads",
                {"outcome": "not.is.null"},
                columns="outcome,funding_bracket,segment,re_trigger_reason",
                limit=5000,
            ) or []
            by_funding_bracket = _compute_by_bracket(outcome_rows)
            by_segment = _compute_by_segment(outcome_rows)
        except Exception:
            pass

    counts["by_funding_bracket"] = by_funding_bracket
    counts["by_segment"] = by_segment

    # Best bracket for Slack line
    best_bracket = ""
    best_rate = 0.0
    for b, stats in by_funding_bracket.items():
        if stats.get("positive_rate", 0) > best_rate:
            best_rate = stats["positive_rate"]
            best_bracket = b

    # Persist to pipeline_runs if creds available
    if config.SUPABASE_URL and config.SUPABASE_KEY:
        try:
            from scripts.common import supabase
            row = {
                "run_date": counts["run_date"],
                "leads_found": found,
                "leads_enriched": enriched,
                "emails_sent": sent,
                "leads_queued": queued,
                "pipeline_value_eur": pipeline_value,
                "metrics": {"by_funding_bracket": by_funding_bracket, "by_segment": by_segment},
            }
            supabase.insert("pipeline_runs", row)
        except Exception as exc:
            counts.setdefault("_errors", []).append(f"pipeline_runs insert error: {exc}")

    # Post Slack summary (no-op when webhook missing)
    try:
        base_text_counts = dict(counts)
        base_text_counts["leads_found"] = found
        base_text_counts["leads_enriched"] = enriched
        base_text_counts["emails_sent"] = sent
        base_text_counts["leads_queued"] = queued
        slack.post_summary(base_text_counts)

        # Additional value line if we have data
        if pipeline_value > 0 or best_bracket:
            val_m = pipeline_value / 1_000_000 if pipeline_value >= 1_000_000 else None
            val_str = f"€{val_m:.1f}M".replace(".0M", "M") if val_m else f"€{int(pipeline_value / 1000)}k" if pipeline_value > 0 else "—"
            bracket_str = f"best bracket {best_bracket} {best_rate:.0%}" if best_bracket else ""
            from scripts.common.slack import post
            post(f":moneybag: Pipeline {val_str}" + (f" · {bracket_str}" if bracket_str else ""))
    except Exception:
        pass

    try:
        log.log_stage("report/daily_summary", {k: v for k, v in counts.items() if k not in ("by_funding_bracket", "by_segment")})
    except Exception:
        pass

    return counts


if __name__ == "__main__":
    import json

    print("=== report/daily_summary.py smoke (keyless) ===")
    print("Expected: counts returned; pipeline_value_eur computed from passed leads; no DB insert")

    # Keyless base smoke
    result = main(found=12, enriched=9, sent=3, queued=4)
    assert "pipeline_value_eur" in result
    assert result["pipeline_value_eur"] == 0.0
    print(f"  keyless base: pipeline_value_eur={result['pipeline_value_eur']}  PASS")

    # With leads carrying funding_amount_eur
    test_leads = [
        {"stage": "hot", "funding_amount_eur": 5_000_000},
        {"stage": "warm", "funding_amount_eur": 2_000_000},
        {"stage": "cold", "funding_amount_eur": 10_000_000},  # excluded (cold)
    ]
    result2 = main(found=3, enriched=3, sent=0, queued=1, leads=test_leads)
    assert result2["pipeline_value_eur"] == 7_000_000.0, f"expected 7M got {result2['pipeline_value_eur']}"
    print(f"  pipeline_value from leads: {result2['pipeline_value_eur']}  PASS")

    # funding_bracket boundaries
    assert funding_bracket(None) == "unknown"
    assert funding_bracket(500_000) == "<1M"
    assert funding_bracket(2_000_000) == "1-5M"
    assert funding_bracket(10_000_000) == "5-20M"
    assert funding_bracket(25_000_000) == "20M+"
    print("  funding_bracket boundaries: PASS")

    print(json.dumps({k: v for k, v in result.items() if k not in ("by_funding_bracket", "by_segment")}, default=str, indent=2)[:1000])
    print("PASS: all daily_summary.py assertions")
