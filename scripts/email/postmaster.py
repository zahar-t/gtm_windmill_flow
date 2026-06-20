"""email/postmaster.py — Domain/IP reputation gate (the "postmaster layer").

Deliverability is a reputation system. The warmup ramp controls *how much* we
send; this layer controls *whether we should send at all right now*, based on
how mailbox providers are actually treating our domain.

It reads two reputation sources (both optional, both smoke-safe):

  1. Instantly campaign analytics  (primary; uses INSTANTLY_API_KEY + campaign)
     Trailing campaign aggregate of sent / delivered / bounces → a bounce rate.
     Instantly owns the cold send, so it owns this telemetry.

  2. Google Postmaster Tools  (optional; GOOGLE_POSTMASTER_TOKEN + POSTMASTER_DOMAIN)
     Gmail's view of the domain: domainReputation (HIGH/MEDIUM/LOW/BAD),
     user-reported spam ratio, SPF/DKIM/DMARC auth success ratios.

It returns a verdict the send stage consults:

    {
      "status": "healthy" | "watch" | "degraded" | "critical" | "unknown",
      "send_multiplier": 1.0 | 0.5 | 0.25 | 0.0,
      "reasons": [...],
      "metrics": {...},
      "sources": [...],
      "date": "YYYY-MM-DD",
    }

send.py multiplies the warmup headroom by `send_multiplier`, so a degrading
reputation automatically throttles (and a critical one pauses) the day's send —
no human in the loop required. With NO reputation data available, status is
"unknown" and the multiplier is 1.0: we degrade to the warmup ramp alone rather
than block the run on missing telemetry.

Never raises. A best-effort snapshot is written to the `domain_reputation`
table when Supabase creds are present (history for trends + the daily summary).
"""
from __future__ import annotations

from datetime import date, timedelta

from scripts.common import config, instantly, log, slack
from scripts.common.http import get_json

# Guarded Supabase import so smoke works without creds
try:
    from scripts.common import supabase as _supabase
    _SUPABASE_OK = True
except Exception:
    _supabase = None  # type: ignore[assignment]
    _SUPABASE_OK = False

_GPT_BASE = "https://gmailpostmastertools.googleapis.com/v1/domains"

# severity ordering → send multiplier
_STATUS_RANK = {"unknown": 0, "healthy": 0, "watch": 1, "degraded": 2, "critical": 3}
_RANK_STATUS = {0: "healthy", 1: "watch", 2: "degraded", 3: "critical"}
_STATUS_MULTIPLIER = {
    "unknown": 1.0, "healthy": 1.0, "watch": 0.5, "degraded": 0.25, "critical": 0.0,
}


def _worse(a: str, b: str) -> str:
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


# ---------------------------------------------------------------------------
# Source 1 — Instantly campaign analytics
# ---------------------------------------------------------------------------
def _instantly_metrics() -> dict | None:
    """Trailing campaign aggregate from Instantly (sent/delivered/bounced/bounce
    rate). None if unavailable; {_empty: True} if configured but no volume yet."""
    m = instantly.campaign_analytics()
    if not isinstance(m, dict):
        return None
    if m.get("_empty"):
        return {"_empty": True}
    return {
        "requests": m.get("sent"),
        "delivered": m.get("delivered"),
        "bounce_rate": m.get("bounce_rate"),
        "replies": m.get("replies"),
        "opens": m.get("opens"),
    }


def _grade_instantly(m: dict, reasons: list[str]) -> str:
    """Map Instantly's bounce rate onto a status, appending human reasons.

    Instantly's analytics don't expose a Gmail-style complaint rate, so the
    bounce rate is the brake here; user-reported spam is graded by Google
    Postmaster (source 2) instead.
    """
    if m.get("_empty"):
        reasons.append("instantly: no send volume in window")
        return "unknown"
    status = "healthy"
    br = m.get("bounce_rate") or 0.0
    if br >= config.POSTMASTER_BOUNCE_CRITICAL:
        status = _worse(status, "critical")
        reasons.append(f"bounce rate {br:.2%} ≥ critical {config.POSTMASTER_BOUNCE_CRITICAL:.2%}")
    elif br >= config.POSTMASTER_BOUNCE_WATCH:
        status = _worse(status, "watch")
        reasons.append(f"bounce rate {br:.2%} ≥ watch {config.POSTMASTER_BOUNCE_WATCH:.2%}")
    if status == "healthy":
        reasons.append(f"instantly healthy (bounce {br:.2%})")
    return status


# ---------------------------------------------------------------------------
# Source 2 — Google Postmaster Tools (optional)
# ---------------------------------------------------------------------------
_GPT_REPUTATION_STATUS = {"HIGH": "healthy", "MEDIUM": "watch", "LOW": "degraded", "BAD": "critical"}


def _google_metrics() -> dict | None:
    """Most-recent Gmail Postmaster traffic stat. None if unavailable."""
    if not config.GOOGLE_POSTMASTER_TOKEN or not config.POSTMASTER_DOMAIN:
        return None
    data = get_json(
        f"{_GPT_BASE}/{config.POSTMASTER_DOMAIN}/trafficStats",
        headers={"Authorization": f"Bearer {config.GOOGLE_POSTMASTER_TOKEN}"},
        timeout=15.0,
        retries=1,
    )
    if not isinstance(data, dict):
        return None
    stats = data.get("trafficStats") or []
    if not stats:
        return {"_empty": True}
    latest = stats[-1]  # API returns ascending by date
    return {
        "domain_reputation": latest.get("domainReputation"),
        "spam_ratio": latest.get("userReportedSpamRatio"),
        "spf": latest.get("spfSuccessRatio"),
        "dkim": latest.get("dkimSuccessRatio"),
        "dmarc": latest.get("dmarcSuccessRatio"),
    }


def _grade_google(m: dict, reasons: list[str]) -> str:
    if m.get("_empty"):
        reasons.append("google postmaster: no data yet")
        return "unknown"
    status = "healthy"
    rep = (m.get("domain_reputation") or "").upper()
    if rep in _GPT_REPUTATION_STATUS:
        status = _worse(status, _GPT_REPUTATION_STATUS[rep])
        reasons.append(f"gmail domain reputation {rep}")
    sr = m.get("spam_ratio")
    if isinstance(sr, (int, float)):
        if sr >= config.POSTMASTER_SPAM_CRITICAL:
            status = _worse(status, "critical")
            reasons.append(f"gmail spam ratio {sr:.3%} ≥ critical")
        elif sr >= config.POSTMASTER_SPAM_WATCH:
            status = _worse(status, "watch")
            reasons.append(f"gmail spam ratio {sr:.3%} ≥ watch")
    for label, key in (("SPF", "spf"), ("DKIM", "dkim"), ("DMARC", "dmarc")):
        v = m.get(key)
        if isinstance(v, (int, float)) and v < 0.90:
            status = _worse(status, "watch")
            reasons.append(f"{label} auth success {v:.0%} < 90%")
    return status


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def check_reputation() -> dict:
    """Combine all reputation sources into a single verdict. Never raises."""
    reasons: list[str] = []
    metrics: dict = {}
    sources: list[str] = []
    status = "unknown"

    try:
        inst = _instantly_metrics()
    except Exception:
        inst = None
    if inst is not None:
        sources.append("instantly")
        metrics["instantly"] = inst
        status = _worse(status, _grade_instantly(inst, reasons))

    try:
        g = _google_metrics()
    except Exception:
        g = None
    if g is not None:
        sources.append("google_postmaster")
        metrics["google_postmaster"] = g
        status = _worse(status, _grade_google(g, reasons))

    if not sources:
        status = "unknown"
        reasons.append("no reputation data — not throttling (warmup ramp still applies)")

    verdict = {
        "status": status,
        "send_multiplier": _STATUS_MULTIPLIER[status],
        "reasons": reasons,
        "metrics": metrics,
        "sources": sources,
        "date": date.today().isoformat(),
    }

    # Flag immediately — write the Supabase snapshot AND alert a human up front,
    # at the START of the run, before any send decision rides on it.
    _persist_snapshot(verdict)
    _alert_if_failing(verdict)

    try:
        log.log_stage("email/postmaster", {
            "status": status,
            "send_multiplier": verdict["send_multiplier"],
            "sources": sources,
        })
    except Exception:
        pass

    return verdict


def _persist_snapshot(verdict: dict) -> None:
    """Best-effort write to domain_reputation. Silent on any failure."""
    if not config.SUPABASE_URL or not config.SUPABASE_KEY or not _SUPABASE_OK or _supabase is None:
        return
    inst = verdict["metrics"].get("instantly") or {}
    g = verdict["metrics"].get("google_postmaster") or {}
    try:
        _supabase.upsert(
            "domain_reputation",
            {
                "date": verdict["date"],
                "domain": config.POSTMASTER_DOMAIN or config.SENDGRID_FROM_EMAIL.split("@")[-1],
                "status": verdict["status"],
                "flagged": verdict["status"] in ("degraded", "critical"),
                "send_multiplier": verdict["send_multiplier"],
                "spam_rate": g.get("spam_ratio"),
                "bounce_rate": inst.get("bounce_rate"),
                "gmail_reputation": g.get("domain_reputation"),
                "metrics": verdict["metrics"],
            },
            on_conflict="date,domain",
        )
    except Exception:
        pass


def _alert_if_failing(verdict: dict) -> None:
    """Immediately Slack a human when a sending domain is failing (degraded/critical)."""
    if verdict["status"] not in ("degraded", "critical"):
        return
    domain = config.POSTMASTER_DOMAIN or (
        config.SENDGRID_FROM_EMAIL.split("@")[-1] if "@" in config.SENDGRID_FROM_EMAIL else "sending domain"
    )
    paused = " — SENDS PAUSED" if verdict["send_multiplier"] == 0 else ""
    try:
        slack.post(
            f":rotating_light: *Domain reputation {verdict['status'].upper()}* — {domain}"
            f"  ·  send ×{verdict['send_multiplier']}{paused}\n"
            + " · ".join(verdict.get("reasons", [])[:3])
        )
    except Exception:
        pass


def latest_verdict() -> dict:
    """Cheap PRE-SEND gate: read the most recent persisted reputation snapshot.

    Reputation is a *lagging* signal — it reflects sends already made — so the
    measurement is done by postmaster_monitor (check_reputation, which hits
    SendGrid/Google and writes domain_reputation). The send path should NOT pay
    that network cost every morning; it just reads the latest snapshot here.

    No store / no snapshot → unknown, multiplier 1.0 (degrade to warmup alone).
    Never raises.
    """
    if not config.SUPABASE_URL or not config.SUPABASE_KEY or not _SUPABASE_OK or _supabase is None:
        return {"status": "unknown", "send_multiplier": 1.0,
                "reasons": ["no reputation store — warmup ramp only"], "source": "none"}
    try:
        rows = _supabase.select("domain_reputation", order="date.desc", limit=1)
    except Exception:
        rows = None
    if not rows:
        return {"status": "unknown", "send_multiplier": 1.0,
                "reasons": ["no reputation snapshot yet — warmup ramp only"], "source": "none"}
    row = rows[0]
    status = row.get("status") or "unknown"
    mult = row.get("send_multiplier")
    if mult is None:
        mult = _STATUS_MULTIPLIER.get(status, 1.0)
    return {"status": status, "send_multiplier": float(mult),
            "reasons": [f"snapshot {row.get('date')}: {status}"],
            "metrics": row.get("metrics") or {}, "source": "snapshot"}


def main() -> dict:
    """Standalone monitor entrypoint — live reputation read + snapshot write.

    Run at the END of the daily flow (postmaster_monitor) and/or on a decoupled
    schedule. Hits SendGrid/Google, writes the domain_reputation snapshot that
    latest_verdict() consumes on the next run's send.
    """
    return check_reputation()


# ---------------------------------------------------------------------------
# Keyless smoke block
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    print("=== postmaster.py smoke (keyless — expect status=unknown, mult=1.0) ===")
    v = check_reputation()
    print(json.dumps(v, default=str, indent=2))
    assert v["status"] == "unknown", "keyless run should be 'unknown'"
    assert v["send_multiplier"] == 1.0, "keyless run must not throttle"
    print("\nPASS: degrades to warmup-only (multiplier 1.0) without reputation data")
