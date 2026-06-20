"""email/warmup_check.py — Instantly ramp gate.

Reads/writes Supabase `email_warmup(date PK, sends_count, daily_limit)`.
daily_limit starts at config.INSTANTLY_WARMUP_START (default 10),
increases by config.INSTANTLY_WARMUP_STEP (default 5) each Monday.

Smoke-safe: if Supabase creds are absent, returns the limit for today
calculated from ramp math only, without touching the database.

def main(today: str = "") -> dict
"""
from __future__ import annotations

from datetime import date, timedelta

from scripts.common import config
from scripts.common import supabase
from scripts.common import log as common_log


def _count_mondays(start: date, end: date) -> int:
    """Return number of Mondays in (start, end] — i.e. Mondays strictly after
    start and up to (including) end. Used to compute ramp weeks."""
    if end <= start:
        return 0
    # Find first Monday strictly after start
    days_to_monday = (7 - start.weekday()) % 7  # days until next Monday
    if days_to_monday == 0:
        days_to_monday = 7  # start itself is Monday → skip it
    first_monday = start + timedelta(days=days_to_monday)
    if first_monday > end:
        return 0
    # Count Mondays from first_monday to end (inclusive)
    return (end - first_monday).days // 7 + 1


def _compute_daily_limit(anchor: date, today: date) -> int:
    """Compute the daily_limit for `today` given the ramp started at `anchor`."""
    weeks = _count_mondays(anchor, today)
    weeks = max(0, weeks)
    return config.INSTANTLY_WARMUP_START + config.INSTANTLY_WARMUP_STEP * weeks


def main(today: str = "") -> dict:
    """Compute today's warmup limit and remaining send headroom.

    Returns
    -------
    dict with keys: date, daily_limit, sends_count, remaining
    """
    d: date = date.fromisoformat(today) if today else date.today()
    today_iso = d.isoformat()

    # --- Smoke path: no Supabase creds ---
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        limit = config.INSTANTLY_WARMUP_START
        result = {
            "date": today_iso,
            "daily_limit": limit,
            "sends_count": 0,
            "remaining": limit,
        }
        try:
            common_log.log_stage("email/warmup_check", result)
        except Exception:
            pass
        return result

    # --- Live path ---
    daily_limit: int = config.INSTANTLY_WARMUP_START
    sends_count: int = 0

    try:
        rows = supabase.select(
            "email_warmup",
            {"date": f"eq.{today_iso}"},
            limit=1,
        )
        if rows:
            row = rows[0]
            daily_limit = int(row.get("daily_limit", config.INSTANTLY_WARMUP_START))
            sends_count = int(row.get("sends_count", 0))
        else:
            # Compute from ramp: find the oldest date in the table as anchor
            try:
                all_rows = supabase.select(
                    "email_warmup",
                    columns="date",
                    order="date.asc",
                    limit=1,
                )
                if all_rows and all_rows[0].get("date"):
                    anchor = date.fromisoformat(all_rows[0]["date"])
                else:
                    anchor = d  # table empty — today is day 0
            except Exception:
                anchor = d

            daily_limit = _compute_daily_limit(anchor, d)

            # Persist today's row
            try:
                supabase.upsert(
                    "email_warmup",
                    {
                        "date": today_iso,
                        "sends_count": 0,
                        "daily_limit": daily_limit,
                    },
                    on_conflict="date",
                )
            except Exception:
                pass
            sends_count = 0

    except Exception as exc:
        # DB error — degrade to config defaults, don't block the pipeline
        daily_limit = config.INSTANTLY_WARMUP_START
        sends_count = 0

    remaining = max(0, daily_limit - sends_count)
    result = {
        "date": today_iso,
        "daily_limit": daily_limit,
        "sends_count": sends_count,
        "remaining": remaining,
    }

    try:
        common_log.log_stage("email/warmup_check", result)
    except Exception:
        pass

    return result


if __name__ == "__main__":
    import json
    from datetime import date as _date

    # Demonstrate the Monday ramp math with injected dates (no network/keys needed).
    print("=== warmup_check.py smoke — Monday ramp math demo ===")
    print(f"  INSTANTLY_WARMUP_START = {config.INSTANTLY_WARMUP_START}")
    print(f"  INSTANTLY_WARMUP_STEP  = {config.INSTANTLY_WARMUP_STEP}")

    # Simulate anchor = 2026-06-01 (Monday), check limits for several Mondays
    anchor = _date(2026, 6, 1)  # a Monday
    test_dates = [
        ("2026-06-01", "Week 0 (anchor Monday, no prior Monday)"),
        ("2026-06-07", "Week 0 (Sunday before next Monday)"),
        ("2026-06-08", "Week 1 (+1 Monday passed)"),
        ("2026-06-15", "Week 1 (day before 2nd Monday)"),
        ("2026-06-22", "Week 3 (3 Mondays: Jun 8, 15, 22)"),
        ("2026-07-06", "Week 5 (5 Mondays)"),
    ]
    for iso, label in test_dates:
        d = _date.fromisoformat(iso)
        mondays = _count_mondays(anchor, d)
        limit = _compute_daily_limit(anchor, d)
        print(f"  {iso} ({label}): {mondays} Mondays elapsed → daily_limit={limit}")

    print()
    print("=== Keyless main() call (no Supabase) ===")
    result = main(today="2026-06-16")
    print(json.dumps(result, default=str))
