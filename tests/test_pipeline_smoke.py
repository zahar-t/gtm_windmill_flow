"""test_pipeline_smoke.py — Pipeline invariant CI test.

Asserts: main(demo=True) returns exactly 2 hot, 0 warm, 2 cold, 1 held(spam),
0 sent. This CI test enforces the demo invariant as a required gate.
"""
from __future__ import annotations

import pytest

from scripts.run_pipeline import main as run_pipeline_main


class TestPipelineDemo:
    """The demo mode pipeline must hit the published invariant every time."""

    def test_demo_invariant_2hot_0warm_2cold_1held_0sent(self):
        summary = run_pipeline_main(demo=True)

        scored = summary["scored"]
        assert scored["hot"] == 2, f"Expected 2 hot, got {scored['hot']}"
        assert scored["warm"] == 0, f"Expected 0 warm, got {scored['warm']}"
        assert scored["cold"] == 2, f"Expected 2 cold, got {scored['cold']}"
        assert summary["held_spam"] == 1, f"Expected 1 held_spam, got {summary['held_spam']}"
        assert summary["sent"] == 0, f"Expected 0 sent (no keys), got {summary['sent']}"

    def test_demo_returns_summary_dict(self):
        summary = run_pipeline_main(demo=True)
        assert isinstance(summary, dict)
        assert "scored" in summary
        assert "found" in summary
        assert "held_spam" in summary
        assert "sent" in summary

    def test_demo_finds_4_leads(self):
        summary = run_pipeline_main(demo=True)
        assert summary["found"] == 4

    def test_demo_no_followups_due(self):
        """Demo mode sets followups=[] so lifecycle node is bypassed."""
        summary = run_pipeline_main(demo=True)
        assert summary["followups_due"] == 0

    def test_demo_reputation_status_is_unknown(self):
        """Keyless → no postmaster → reputation status is 'unknown'."""
        summary = run_pipeline_main(demo=True)
        assert summary["reputation"] == "unknown"
