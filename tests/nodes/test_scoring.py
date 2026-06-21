"""test_scoring.py — ICP rubric deterministic tests.

Tests score_company directly (no LLM, no network). Uses a fixed
today=date(2026,6,20) for repeatability. Asserts the rubric's ACTUAL behaviour
for the generic GTM Engine ICP model (firmographic fit + signal recency +
geography + headcount + stage), not an external target profile.
"""
from __future__ import annotations

from datetime import date

import pytest

from scripts.score.icp_rubric import (
    CompanyRecord, ICPConfig, ICPScore, Stage, Tier,
    score_company, record_from_lead, DEFAULT_CONFIG,
)

TODAY = date(2026, 6, 20)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def score(rec: CompanyRecord) -> ICPScore:
    return score_company(rec, today=TODAY)


# ---------------------------------------------------------------------------
# Hard disqualifiers → score=0, tier=DQ
# ---------------------------------------------------------------------------
class TestHardDQ:
    def test_competitor_dq(self):
        r = score(CompanyRecord(name="Rival", is_competitor=True))
        assert r.score == 0
        assert r.tier == Tier.DQ
        assert "competitor" in (r.dq_reason or "")

    def test_enterprise_headcount_dq(self):
        # max_headcount=5000 → strictly greater triggers DQ.
        r = score(CompanyRecord(name="BigCorp", headcount=5001))
        assert r.score == 0
        assert r.tier == Tier.DQ
        assert "enterprise" in (r.dq_reason or "")

    def test_headcount_exactly_at_limit_is_not_dq(self):
        # exactly 5000 is NOT > 5000 → not a hard DQ.
        r = score(CompanyRecord(name="Boundary", headcount=5000))
        assert r.tier != Tier.DQ

    def test_hard_excluded_sector_direct_competitor_dq(self):
        r = score(CompanyRecord(name="X", sector="direct competitor"))
        assert r.score == 0
        assert r.tier == Tier.DQ

    def test_hard_excluded_sector_market_research_dq(self):
        r = score(CompanyRecord(name="ResearchCo", sector="market research tool"))
        assert r.score == 0
        assert r.tier == Tier.DQ


# ---------------------------------------------------------------------------
# Tier boundaries (values verified against the rubric as of TODAY)
# ---------------------------------------------------------------------------
class TestTierBoundaries:
    def test_tier_a_at_or_above_70(self):
        # US home(20) + 3mo(25) + 2M(11) + hc60 sweet(15) + seed(10) + raising(+5) = 81 → A
        r = score(CompanyRecord(
            name="TierA",
            country="US",
            last_funding_date=date(2026, 3, 1),   # ~3 months ago
            last_funding_eur=2_000_000,
            headcount=60,
            stage=Stage.SEED,
            actively_raising=True,
        ))
        assert r.score >= 70
        assert r.tier == Tier.A

    def test_tier_b_range(self):
        # GB adjacent(16) + 20mo(8) + 1M(11) + hc20 small(8) + seed(10) = 53 → B
        r = score(CompanyRecord(
            name="TierB",
            country="GB",
            last_funding_date=date(2024, 10, 1),  # ~20 months ago
            last_funding_eur=1_000_000,
            headcount=20,
            stage=Stage.SEED,
        ))
        assert 50 <= r.score <= 69
        assert r.tier == Tier.B

    def test_tier_c_range(self):
        # GB adjacent(16) + no signal(0) + no strength(2) + hc200 sweet(15) + A(10) = 43 → C
        r = score(CompanyRecord(
            name="TierC",
            country="GB",
            headcount=200,
            stage=Stage.SERIES_A,
        ))
        assert 30 <= r.score <= 49
        assert r.tier == Tier.C

    def test_tier_d_below_30(self):
        r = score(CompanyRecord(name="TierD", country="US"))
        assert r.score < 30
        assert r.tier == Tier.D


# ---------------------------------------------------------------------------
# Score ranges for golden leads
# ---------------------------------------------------------------------------
class TestGoldenLeads:
    def test_high_score_us_seed(self, golden):
        leads = golden("scoring_leads.json")
        hit = next(l for l in leads if l["_label"] == "high_score_us_seed_funded")
        result = score_company(record_from_lead(hit, today=TODAY), today=TODAY)
        assert result.score >= 70
        assert result.tier == Tier.A

    def test_mid_score_gb_series_a(self, golden):
        leads = golden("scoring_leads.json")
        hit = next(l for l in leads if l["_label"] == "mid_score_gb_series_a")
        result = score_company(record_from_lead(hit, today=TODAY), today=TODAY)
        assert result.tier in (Tier.B, Tier.C)
        assert result.score < 70

    def test_dq_competitor(self, golden):
        leads = golden("scoring_leads.json")
        rival = next(l for l in leads if l["_label"] == "dq_competitor")
        result = score_company(record_from_lead(rival, today=TODAY), today=TODAY)
        assert result.tier == Tier.DQ

    def test_dq_enterprise(self, golden):
        leads = golden("scoring_leads.json")
        big = next(l for l in leads if l["_label"] == "dq_enterprise_too_large")
        result = score_company(record_from_lead(big, today=TODAY), today=TODAY)
        assert result.tier == Tier.DQ

    def test_dq_excluded_sector(self, golden):
        leads = golden("scoring_leads.json")
        ex = next(l for l in leads if l["_label"] == "dq_excluded_sector")
        result = score_company(record_from_lead(ex, today=TODAY), today=TODAY)
        assert result.tier == Tier.DQ


# ---------------------------------------------------------------------------
# Monotonicity property: a strictly-better lead never scores lower
# ---------------------------------------------------------------------------
class TestMonotonicity:
    def test_fresh_funding_beats_old(self):
        fresh = score(CompanyRecord(
            name="X", country="US",
            last_funding_date=date(2026, 5, 1),   # 1mo ago
            last_funding_eur=5_000_000,
            headcount=50, stage=Stage.SEED,
        ))
        stale = score(CompanyRecord(
            name="X", country="US",
            last_funding_date=date(2023, 5, 1),   # ~37mo ago
            last_funding_eur=5_000_000,
            headcount=50, stage=Stage.SEED,
        ))
        assert fresh.score > stale.score

    def test_home_market_beats_out_of_footprint(self):
        base_kwargs = dict(
            headcount=50, stage=Stage.SEED,
            last_funding_date=date(2026, 3, 1), last_funding_eur=2_000_000,
        )
        home = score(CompanyRecord(name="X", country="US", **base_kwargs))
        out = score(CompanyRecord(name="X", country="XX", **base_kwargs))
        assert home.score > out.score

    def test_larger_funding_beats_smaller(self):
        large = score(CompanyRecord(
            name="X", country="US",
            last_funding_date=date(2026, 5, 1), last_funding_eur=20_000_000,
        ))
        small = score(CompanyRecord(
            name="X", country="US",
            last_funding_date=date(2026, 5, 1), last_funding_eur=100_000,
        ))
        assert large.score > small.score

    def test_capital_intensive_penalty_lowers_score(self):
        base = score(CompanyRecord(
            name="X", country="US",
            last_funding_date=date(2026, 5, 1), last_funding_eur=5_000_000,
            headcount=50, stage=Stage.SEED,
        ))
        penalised = score(CompanyRecord(
            name="X", country="US",
            last_funding_date=date(2026, 5, 1), last_funding_eur=5_000_000,
            headcount=50, stage=Stage.SEED, sector="logistics",
        ))
        assert base.score > penalised.score


# ---------------------------------------------------------------------------
# record_from_lead adapter
# ---------------------------------------------------------------------------
class TestRecordFromLead:
    def test_maps_company_size_to_headcount(self):
        lead = {"company": "X", "company_size": "150"}
        rec = record_from_lead(lead, today=TODAY)
        assert rec.headcount == 150

    def test_funding_signal_type_sets_actively_raising(self):
        lead = {"company": "X", "signal_type": "funding"}
        rec = record_from_lead(lead, today=TODAY)
        assert rec.actively_raising is True

    def test_months_since_funding_derived(self):
        lead = {"company": "X", "months_since_last_funding": 3}
        rec = record_from_lead(lead, today=TODAY)
        assert rec.last_funding_date is not None

    def test_range_headcount_midpointed(self):
        lead = {"company": "X", "company_size": "51-200"}
        rec = record_from_lead(lead, today=TODAY)
        assert rec.headcount == 125  # (51+200)//2
