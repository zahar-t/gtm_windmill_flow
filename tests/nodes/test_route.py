"""test_route.py — Deterministic routing boundary tests."""
from __future__ import annotations

import pytest

from scripts.score.route import _route, main


# ---------------------------------------------------------------------------
# _route() unit tests — all boundary cases from plan.md spec
# ---------------------------------------------------------------------------
class TestRouteFunction:
    def test_hot(self):
        assert _route(71, True) == "hot"

    def test_hot_score_100(self):
        assert _route(100, True) == "hot"

    def test_warm_score_71_no_signal(self):
        """Score > 70 but no signal → warm."""
        assert _route(71, False) == "warm"

    def test_warm_score_exactly_70_with_signal(self):
        """Exactly 70 with signal → warm (needs strictly > 70)."""
        assert _route(70, True) == "warm"

    def test_warm_score_70_no_signal(self):
        assert _route(70, False) == "warm"

    def test_warm_score_40_boundary(self):
        """40 is the inclusive lower bound of warm."""
        assert _route(40, False) == "warm"

    def test_warm_score_40_with_signal(self):
        assert _route(40, True) == "warm"

    def test_warm_midrange(self):
        assert _route(55, False) == "warm"

    def test_cold_score_39(self):
        """39 is below 40 → cold."""
        assert _route(39, True) == "cold"

    def test_cold_score_0(self):
        assert _route(0, False) == "cold"

    def test_cold_score_39_no_signal(self):
        assert _route(39, False) == "cold"


# ---------------------------------------------------------------------------
# main() — processes the leads list, sets lead["stage"]
# ---------------------------------------------------------------------------
class TestRouteMain:
    def test_routes_a_batch(self):
        leads = [
            {"icp_score": 80, "signal": "Funded"},
            {"icp_score": 55, "signal": None},
            {"icp_score": 20, "signal": None},
        ]
        main(leads)
        assert leads[0]["stage"] == "hot"
        assert leads[1]["stage"] == "warm"
        assert leads[2]["stage"] == "cold"

    def test_none_score_treated_as_zero(self):
        leads = [{"icp_score": None, "signal": "trigger"}]
        main(leads)
        assert leads[0]["stage"] == "cold"

    def test_skip_leads_untouched(self):
        lead = {"_skip": True, "icp_score": 90, "signal": "big news", "stage": "warm"}
        main([lead])
        # stage preserved — not overwritten
        assert lead["stage"] == "warm"

    def test_empty_list_no_crash(self):
        result = main([])
        assert result == []

    def test_none_list_no_crash(self):
        result = main(None)
        assert result == []

    def test_returns_same_list(self, make_lead):
        leads = [make_lead(icp_score=80, signal="news")]
        result = main(leads)
        assert result is leads
