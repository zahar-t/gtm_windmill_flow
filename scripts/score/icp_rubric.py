"""score/icp_rubric.py — Deterministic ICP qualification & scoring.

Transparent, stdlib-only rubric that turns a company profile into a 0-100 ICP
score + tier + human-readable reasons. Replaces the opaque "ask the LLM for a
number" approach with an auditable, tunable model the feedback loop can write
back to (ICPConfig).

------------------------------------------------------------------------------
THE THESIS (why the rubric is shaped this way)
------------------------------------------------------------------------------
The platform targets B2B companies that are a strong product fit: the right
industry/sector, the right size (typically 50-500 employees), in target
geographies, and showing recent buying signals such as a funding round, active
hiring, headcount growth, product launch, or tech adoption. A recent funding
event is particularly valuable because it signals budget availability, growth
momentum, and a clear trigger for outreach.

The current beachhead is the target VC network and companies scaling across
multiple geographies. So geography + recent signals are weighted heavily.

------------------------------------------------------------------------------
SCORING MODEL (0-100, additive, transparent)
------------------------------------------------------------------------------
Positive signals (max 100):
    signal_recency   25   recent trigger (funding, hiring, launch, etc.)
    signal_strength  20   size of funding raise or revenue band
    geography        20   home market, then adjacent, then target regions
    headcount_band   15   sweet spot ~50-500 employees
    stage            10   seed to Series A is ideal, bootstrapped also welcome
    multi_entity     10   multi-geo footprint => expansion hook

Timing bonus (capped at 100 total):
    actively_raising  +5  active raise = a clean outreach trigger

Penalties (subtract, floored at 0):
    capital_intensive_sector  -10  budget gets deployed fast, less discretionary spend
    soft_excluded_sector      -15  regulatory or fit friction
    excluded_competitor       -20  direct competitor flag

Hard disqualifiers (score -> 0, tier DISQUALIFIED):
    is_competitor (hard flag), headcount > max_headcount,
    sector in hard_excluded set.

Tiers map onto the pipeline's hot/warm/cold routing via the 0-100 score:
    A  Priority      >= 70      (score>70 + signal -> hot in score/route.py)
    B  Qualified     50-69
    C  Nurture       30-49
    D  Deprioritize  < 30
    DQ Disqualified  hard gate hit (score 0 -> cold)
------------------------------------------------------------------------------
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
class Stage(Enum):
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    SERIES_C_PLUS = "series_c_plus"
    BOOTSTRAPPED = "bootstrapped"   # profitable / revenue-funded, no priced round
    UNKNOWN = "unknown"


@dataclass
class CompanyRecord:
    """Everything the scorer needs. Most fields are optional — unknowns score 0
    for that signal rather than disqualifying."""
    name: str
    country: Optional[str] = None              # ISO-2, e.g. "DE", "GB", "US"
    operating_countries: list[str] = field(default_factory=list)
    headcount: Optional[int] = None
    stage: Stage = Stage.UNKNOWN
    sector: Optional[str] = None               # free text, lower-cased internally
    last_funding_date: Optional[date] = None
    last_funding_eur: Optional[float] = None   # most recent round size
    total_raised_eur: Optional[float] = None
    annual_revenue_eur: Optional[float] = None
    actively_raising: bool = False
    is_competitor: bool = False


# --------------------------------------------------------------------------- #
# Config (tune these; the GTM Engine feedback loop writes back here)
# --------------------------------------------------------------------------- #
@dataclass
class ICPConfig:
    # geography buckets -> points (home beachhead first, then concentric rings)
    geo_home: set[str] = field(default_factory=lambda: {"US", "CA"})
    geo_adjacent: set[str] = field(default_factory=lambda: {"GB", "AU"})
    geo_eu_eea: set[str] = field(default_factory=lambda: {
        "DE", "FR", "NL", "SE", "IE", "BE", "FI", "DK", "NO", "CH",
        "AT", "PL", "CZ", "EE", "LT", "LV", "PT", "ES", "IT", "LU",
    })
    geo_extended: set[str] = field(default_factory=lambda: {"SG", "IN", "IL", "AE"})
    pts_geo_home: int = 20
    pts_geo_adjacent: int = 16
    pts_geo_eu: int = 10
    pts_geo_extended: int = 6

    # headcount sweet spot (default: 50-500 is the B2B SaaS mid-market)
    hc_sweet_lo: int = 50
    hc_sweet_hi: int = 500
    max_headcount: int = 5000   # hard DQ above this (enterprise, different sales motion)

    # sectors where our platform fits less well (capital spend dominates opex budget)
    capital_intensive_sectors: set[str] = field(default_factory=lambda: {
        "energy", "solar", "renewables", "real estate", "construction",
        "infrastructure", "logistics", "hardware", "manufacturing",
    })
    soft_excluded_sectors: set[str] = field(default_factory=lambda: {
        "crypto", "web3", "defi",
    })
    hard_excluded_sectors: set[str] = field(default_factory=lambda: {
        "direct competitor", "market research tool",
    })
    penalty_capital_intensive: int = 10
    penalty_soft_excluded: int = 15

    # tier thresholds
    tier_a: int = 70
    tier_b: int = 50
    tier_c: int = 30


DEFAULT_CONFIG = ICPConfig()


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
class Tier(Enum):
    A = "A - Priority"
    B = "B - Qualified"
    C = "C - Nurture"
    D = "D - Deprioritize"
    DQ = "DQ - Disqualified"


@dataclass
class ICPScore:
    name: str
    score: int
    tier: Tier
    reasons: list[str] = field(default_factory=list)
    dq_reason: Optional[str] = None

    @property
    def reasoning(self) -> str:
        """One-line, persistence-friendly reason string for the lead dict."""
        return self.dq_reason or " | ".join(self.reasons)


# --------------------------------------------------------------------------- #
# Signal scorers
# --------------------------------------------------------------------------- #
def _months_since(d: Optional[date], today: date) -> Optional[int]:
    if d is None:
        return None
    return (today.year - d.year) * 12 + (today.month - d.month)


def _score_signal_recency(rec: CompanyRecord, today: date) -> tuple[int, str]:
    m = _months_since(rec.last_funding_date, today)
    if m is None:
        return 0, "no known signal date"
    if m <= 6:
        return 25, f"signal {m}mo ago (very recent)"
    if m <= 12:
        return 20, f"signal {m}mo ago (recent)"
    if m <= 18:
        return 14, f"signal {m}mo ago"
    if m <= 24:
        return 8, f"signal {m}mo ago (ageing)"
    return 0, f"signal {m}mo ago (stale)"


def _score_signal_strength(rec: CompanyRecord) -> tuple[int, str]:
    # take the strongest available signal of company scale / budget capacity
    scale = max(
        rec.last_funding_eur or 0,
        rec.total_raised_eur or 0,
        (rec.annual_revenue_eur or 0) * 0.5,  # revenue proxy, discounted
    )
    if scale >= 10_000_000:
        return 20, "10M+ funding/revenue signal"
    if scale >= 3_000_000:
        return 16, "3-10M funding/revenue signal"
    if scale >= 1_000_000:
        return 11, "1-3M funding/revenue signal"
    if scale >= 300_000:
        return 6, "300k-1M funding/revenue signal"
    return 2, "minimal/unknown scale signal"


def _score_geography(rec: CompanyRecord, cfg: ICPConfig) -> tuple[int, str]:
    c = (rec.country or "").upper()
    if c in cfg.geo_home:
        return cfg.pts_geo_home, f"home market ({c})"
    if c in cfg.geo_adjacent:
        return cfg.pts_geo_adjacent, f"adjacent market ({c})"
    if c in cfg.geo_eu_eea:
        return cfg.pts_geo_eu, f"target region ({c})"
    if c in cfg.geo_extended:
        return cfg.pts_geo_extended, f"extended geo ({c})"
    return 0, f"out-of-footprint geo ({c or 'unknown'})"


def _score_headcount(rec: CompanyRecord, cfg: ICPConfig) -> tuple[int, str]:
    h = rec.headcount
    if h is None:
        return 0, "headcount unknown"
    if cfg.hc_sweet_lo <= h <= cfg.hc_sweet_hi:
        return 15, f"{h} staff (sweet spot)"
    if cfg.hc_sweet_hi < h <= 1000:
        return 10, f"{h} staff"
    if 15 <= h < cfg.hc_sweet_lo:
        return 8, f"{h} staff (small)"
    if 1000 < h <= 2000:
        return 5, f"{h} staff (larger org)"
    if h < 15:
        return 2, f"{h} staff (very early)"
    return 0, f"{h} staff (enterprise)"


def _score_stage(rec: CompanyRecord) -> tuple[int, str]:
    pts = {
        Stage.SEED: 10, Stage.SERIES_A: 10,
        Stage.SERIES_B: 8, Stage.BOOTSTRAPPED: 7,
        Stage.PRE_SEED: 5, Stage.SERIES_C_PLUS: 3,
        Stage.UNKNOWN: 0,
    }[rec.stage]
    return pts, f"stage={rec.stage.value}"


def _score_multi_entity(rec: CompanyRecord) -> tuple[int, str]:
    geos = {g.upper() for g in rec.operating_countries if g}
    if len(geos) >= 2:
        return 10, f"multi-geo ({'/'.join(sorted(geos))}) -> expansion opportunity"
    return 0, "single-entity"


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def score_company(
    rec: CompanyRecord,
    cfg: ICPConfig = DEFAULT_CONFIG,
    today: Optional[date] = None,
) -> ICPScore:
    today = today or date.today()
    sector = (rec.sector or "").lower().strip()

    # ---- hard disqualifiers ----
    if rec.is_competitor:
        return ICPScore(rec.name, 0, Tier.DQ, dq_reason="competitor")
    if rec.headcount is not None and rec.headcount > cfg.max_headcount:
        return ICPScore(rec.name, 0, Tier.DQ,
                        dq_reason=f"enterprise ({rec.headcount} staff)")
    if sector and any(x in sector for x in cfg.hard_excluded_sectors):
        return ICPScore(rec.name, 0, Tier.DQ, dq_reason=f"excluded sector ({sector})")

    reasons: list[str] = []
    total = 0
    for fn in (
        lambda: _score_signal_recency(rec, today),
        lambda: _score_signal_strength(rec),
        lambda: _score_geography(rec, cfg),
        lambda: _score_headcount(rec, cfg),
        lambda: _score_stage(rec),
        lambda: _score_multi_entity(rec),
    ):
        pts, why = fn()
        total += pts
        if pts:
            reasons.append(f"+{pts} {why}")

    # ---- timing bonus ----
    if rec.actively_raising:
        total += 5
        reasons.append("+5 actively raising (outreach trigger)")

    # ---- penalties ----
    if sector and any(x in sector for x in cfg.capital_intensive_sectors):
        total -= cfg.penalty_capital_intensive
        reasons.append(f"-{cfg.penalty_capital_intensive} capital-intensive sector ({sector})")
    if sector and any(x in sector for x in cfg.soft_excluded_sectors):
        total -= cfg.penalty_soft_excluded
        reasons.append(f"-{cfg.penalty_soft_excluded} soft-excluded sector ({sector})")

    total = max(0, min(100, total))

    if total >= cfg.tier_a:
        tier = Tier.A
    elif total >= cfg.tier_b:
        tier = Tier.B
    elif total >= cfg.tier_c:
        tier = Tier.C
    else:
        tier = Tier.D

    return ICPScore(rec.name, total, tier, reasons)


# --------------------------------------------------------------------------- #
# Lead-dict adapter — maps the canonical pipeline lead dict -> CompanyRecord
# --------------------------------------------------------------------------- #
_STAGE_ALIASES: dict[str, Stage] = {
    "pre_seed": Stage.PRE_SEED, "preseed": Stage.PRE_SEED, "pre-seed": Stage.PRE_SEED,
    "seed": Stage.SEED,
    "series_a": Stage.SERIES_A, "series a": Stage.SERIES_A, "a": Stage.SERIES_A,
    "series_b": Stage.SERIES_B, "series b": Stage.SERIES_B, "b": Stage.SERIES_B,
    "series_c": Stage.SERIES_C_PLUS, "series c": Stage.SERIES_C_PLUS,
    "series_c_plus": Stage.SERIES_C_PLUS, "series_d": Stage.SERIES_C_PLUS,
    "growth": Stage.SERIES_C_PLUS, "late": Stage.SERIES_C_PLUS,
    "bootstrapped": Stage.BOOTSTRAPPED, "profitable": Stage.BOOTSTRAPPED,
    "revenue-funded": Stage.BOOTSTRAPPED,
}


def _coerce_stage(v) -> Stage:
    if isinstance(v, Stage):
        return v
    if not v:
        return Stage.UNKNOWN
    key = str(v).strip().lower().replace("-", "_")
    if key in _STAGE_ALIASES:
        return _STAGE_ALIASES[key]
    # also tolerate raw enum values like "series_a"
    try:
        return Stage(key)
    except ValueError:
        return _STAGE_ALIASES.get(str(v).strip().lower(), Stage.UNKNOWN)


def _parse_headcount(v) -> Optional[int]:
    """Accept an int, a float, or a range string like '51-200' / '201-500 employees'."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    nums = [int(n) for n in re.findall(r"\d+", str(v).replace(",", ""))]
    if not nums:
        return None
    if len(nums) >= 2:
        return (nums[0] + nums[1]) // 2  # midpoint of a range
    return nums[0]


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _to_date(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _date_n_months_ago(n: Optional[int], today: date) -> Optional[date]:
    """Approximate a signal date from 'months ago' (day clamped to 1)."""
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    total = (today.year * 12 + (today.month - 1)) - n
    year, month = divmod(total, 12)
    try:
        return date(year, month + 1, 1)
    except ValueError:
        return None


def record_from_lead(lead: dict, today: Optional[date] = None) -> CompanyRecord:
    """Map a canonical pipeline lead dict onto a CompanyRecord.

    Reads structured fields where present (set by enrichment or by the optional
    Claude extractor in score/icp.py). Missing fields stay None and simply score
    0 for their signal — they never disqualify. `signal_type == "funding"` is
    treated as an active-raise / recent-signal trigger when nothing more specific
    is known.
    """
    today = today or date.today()

    name = lead.get("company") or lead.get("name") or "(unnamed)"

    # operating_countries may arrive as a list or a delimited string
    ops_raw = lead.get("operating_countries") or []
    if isinstance(ops_raw, str):
        ops = [c.strip() for c in ops_raw.replace(",", ";").split(";") if c.strip()]
    else:
        ops = [str(c).strip() for c in ops_raw if c]

    # signal date: prefer an explicit date, else derive from "months ago"
    last_funding_date = _to_date(lead.get("last_funding_date"))
    if last_funding_date is None:
        last_funding_date = _date_n_months_ago(
            lead.get("months_since_last_funding"), today
        )

    signal_type = (lead.get("signal_type") or "").lower()
    actively_raising = _to_bool(lead.get("actively_raising")) or signal_type == "funding"

    return CompanyRecord(
        name=name,
        country=lead.get("country"),
        operating_countries=ops,
        headcount=_parse_headcount(lead.get("headcount") or lead.get("company_size")),
        stage=_coerce_stage(lead.get("funding_stage")),
        sector=lead.get("sector") or lead.get("industry"),
        last_funding_date=last_funding_date,
        last_funding_eur=_to_float(lead.get("last_funding_eur")),
        total_raised_eur=_to_float(lead.get("total_raised_eur")),
        annual_revenue_eur=_to_float(lead.get("annual_revenue_eur")),
        actively_raising=actively_raising,
        is_competitor=_to_bool(lead.get("is_competitor")),
    )


# --------------------------------------------------------------------------- #
# Demo — synthetic companies (no real names), fixed "today" for repeatability
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    TODAY = date(2026, 6, 17)

    sample = [
        CompanyRecord(
            name="Acme Cloud", country="US",
            operating_countries=["US", "GB", "DE"], headcount=150,
            stage=Stage.SERIES_A, sector="b2b saas",
            last_funding_date=date(2025, 5, 1), last_funding_eur=6_000_000,
            annual_revenue_eur=21_000_000, actively_raising=True),
        CompanyRecord(
            name="Beta Analytics", country="GB", operating_countries=["GB"],
            headcount=40, stage=Stage.SERIES_A, sector="data analytics saas",
            last_funding_date=date(2025, 8, 1), last_funding_eur=15_000_000,
            total_raised_eur=17_000_000),
        CompanyRecord(
            name="Gamma HealthSaaS", country="DE",
            operating_countries=["DE", "AT", "CH"], headcount=80,
            stage=Stage.SERIES_A, sector="healthtech saas",
            last_funding_date=date(2024, 3, 1), total_raised_eur=12_500_000),
        CompanyRecord(
            name="Delta Corp", country="US", headcount=6000,
            stage=Stage.SERIES_C_PLUS, sector="enterprise software"),
    ]

    scored = sorted(
        (score_company(c, today=TODAY) for c in sample),
        key=lambda s: s.score, reverse=True,
    )
    print(f"\nGTM Engine ICP rubric  (as of {TODAY})\n" + "=" * 60)
    for s in scored:
        flag = f"DQ: {s.dq_reason}" if s.dq_reason else " | ".join(s.reasons)
        print(f"{s.name:<24} {s.score:>3}  {s.tier.value}\n    {flag}\n")
