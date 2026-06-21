"""test_priority.py — score/priority.py tests.

All deterministic; no network needed. Tests verify ordering (fresh > stale,
big > small) and that priority is in [0,1].
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.score.priority import main, _recency_decay, _size_weight, _clamp01


NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Component tests
# ---------------------------------------------------------------------------
class TestRecencyDecay:
    def test_very_recent_near_one(self):
        ts = _iso(NOW - timedelta(hours=1))
        decay = _recency_decay(ts, tau_days=14.0)
        assert decay > 0.99

    def test_old_signal_near_zero(self):
        ts = _iso(NOW - timedelta(days=90))
        decay = _recency_decay(ts, tau_days=14.0)
        assert decay < 0.01

    def test_future_ts_clamped_to_one(self):
        ts = _iso(NOW + timedelta(days=5))
        decay = _recency_decay(ts, tau_days=14.0)
        assert decay == pytest.approx(1.0)

    def test_none_ts_returns_zero(self):
        assert _recency_decay(None, 14.0) == 0.0

    def test_empty_string_returns_zero(self):
        assert _recency_decay("", 14.0) == 0.0

    def test_recent_beats_old(self):
        recent = _recency_decay(_iso(NOW - timedelta(days=2)), 14.0)
        old = _recency_decay(_iso(NOW - timedelta(days=60)), 14.0)
        assert recent > old


class TestSizeWeight:
    def test_large_round_near_one(self):
        # €50M = cap → 1.0
        w = _size_weight(50_000_000)
        assert w == pytest.approx(1.0)

    def test_none_returns_zero(self):
        assert _size_weight(None) == 0.0

    def test_zero_returns_zero(self):
        assert _size_weight(0) == 0.0

    def test_negative_returns_zero(self):
        assert _size_weight(-100) == 0.0

    def test_larger_beats_smaller(self):
        assert _size_weight(10_000_000) > _size_weight(500_000)

    def test_in_range(self):
        for v in [100_000, 1_000_000, 10_000_000, 50_000_000]:
            assert 0.0 <= _size_weight(v) <= 1.0


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------
class TestPriorityMain:
    def test_recent_large_funding_high_priority(self):
        lead = {
            "signal_type": "funding",
            "signal_ts": _iso(NOW - timedelta(days=2)),
            "funding_amount_eur": 10_000_000,
            "icp_score": 70,
        }
        main([lead])
        assert "priority" in lead
        assert "intent_score" in lead
        assert 0.0 <= lead["priority"] <= 1.0
        assert 0.0 <= lead["intent_score"] <= 1.0

    def test_fresh_beats_stale_same_amount(self):
        fresh = {
            "signal_type": "funding",
            "signal_ts": _iso(NOW - timedelta(days=2)),
            "funding_amount_eur": 10_000_000,
            "icp_score": 70,
        }
        stale = {
            "signal_type": "funding",
            "signal_ts": _iso(NOW - timedelta(days=60)),
            "funding_amount_eur": 10_000_000,
            "icp_score": 70,
        }
        main([fresh, stale])
        assert fresh["priority"] > stale["priority"]

    def test_large_beats_small_same_recency(self):
        large = {
            "signal_type": "funding",
            "signal_ts": _iso(NOW - timedelta(days=5)),
            "funding_amount_eur": 20_000_000,
            "icp_score": 60,
        }
        small = {
            "signal_type": "funding",
            "signal_ts": _iso(NOW - timedelta(days=5)),
            "funding_amount_eur": 200_000,
            "icp_score": 60,
        }
        main([large, small])
        assert large["priority"] > small["priority"]

    def test_funding_beats_hiring_at_equal_recency(self):
        funding = {
            "signal_type": "funding",
            "signal_ts": _iso(NOW - timedelta(days=2)),
            "funding_amount_eur": None,
            "icp_score": 70,
        }
        hiring = {
            "signal_type": "hiring",
            "signal_ts": _iso(NOW - timedelta(days=2)),
            "funding_amount_eur": None,
            "icp_score": 70,
        }
        main([funding, hiring])
        assert funding["priority"] > hiring["priority"]

    def test_skip_leads_untouched(self):
        lead = {"_skip": True, "signal_type": "funding"}
        main([lead])
        assert "priority" not in lead
        assert "intent_score" not in lead

    def test_priority_in_bounds(self):
        leads = [
            {"signal_type": "none", "signal_ts": None, "funding_amount_eur": None, "icp_score": 0},
            {"signal_type": "funding", "signal_ts": _iso(NOW), "funding_amount_eur": 100_000_000, "icp_score": 100},
        ]
        main(leads)
        for l in leads:
            assert 0.0 <= l["priority"] <= 1.0

    def test_funding_announced_at_fallback(self):
        """signal_ts absent → falls back to funding_announced_at."""
        lead = {
            "signal_type": "funding",
            "signal_ts": None,
            "funding_announced_at": _iso(NOW - timedelta(days=3)),
            "funding_amount_eur": 5_000_000,
            "icp_score": 60,
        }
        main([lead])
        assert lead["priority"] > 0.0
