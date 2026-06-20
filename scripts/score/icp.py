"""Stage 3 — ICP scoring.

Assigns each lead a 0-100 ICP fit score, a tier (A/B/C/D/DQ), and a
human-readable reasoning string, using the deterministic rubric in
score/icp_rubric.py.

Design: the score itself is a transparent, auditable rubric (firmographic fit +
signal recency + geography + headcount + stage), NOT an opaque LLM number.
Claude is kept in the loop only as a structured-input EXTRACTOR — it reads the
lead's free text (company, industry, recent signal, LinkedIn blurb) and pulls
out the structured fields the rubric needs (country, funding stage, months
since last signal, sector, etc.). The rubric then scores deterministically.

Keyless / smoke-safe:
  - No ANTHROPIC_API_KEY -> the extractor is skipped; the rubric scores from
    whatever structured fields enrichment already populated (company_size,
    industry, signal_type). Still deterministic, still end-to-end.
  - Never raises; per-lead errors are appended to lead["_errors"].

ICP target: B2B SaaS and tech companies in the 50-500 employee range, in
target geographies, showing recent buying signals (funding, hiring, growth).
Full thesis + weights live in score/icp_rubric.py.
"""
from __future__ import annotations

from scripts.common import claude, log
from scripts.score import icp_rubric

# ---------------------------------------------------------------------------
# Structured-input extraction (optional LLM step)
# ---------------------------------------------------------------------------
_EXTRACT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "country": {"type": "string"},                       # ISO-2 or ""
        "headcount": {"type": ["integer", "null"]},
        "funding_stage": {
            "type": "string",
            "enum": [
                "pre_seed", "seed", "series_a", "series_b",
                "series_c_plus", "bootstrapped", "unknown",
            ],
        },
        "sector": {"type": "string"},
        "months_since_last_funding": {"type": ["integer", "null"]},
        "last_funding_eur": {"type": ["number", "null"]},
        "total_raised_eur": {"type": ["number", "null"]},
        "annual_revenue_eur": {"type": ["number", "null"]},
        "actively_raising": {"type": "boolean"},
    },
    "required": ["funding_stage", "actively_raising"],
}

_EXTRACT_SYSTEM = (
    "You extract structured firmographics for a B2B SaaS ICP scoring model. "
    "Given a company profile, return ONLY the fields you can justify from the "
    "text — use null/empty/false when unknown, never guess. "
    "country is ISO-2 (e.g. US, DE, GB). funding_stage is the company's latest "
    "round. months_since_last_funding is whole months from the most recent "
    "raise or signal mentioned. Amounts are in EUR (convert approximately if "
    "quoted in another currency). "
    "Do NOT include any client names or proprietary information."
)


def _build_profile(lead: dict) -> str:
    """Build a compact textual profile string for extraction."""
    parts: list[str] = []
    fields = [
        ("Company", lead.get("company")),
        ("Title", lead.get("title")),
        ("Industry", lead.get("industry")),
        ("Company size", lead.get("company_size")),
        ("Country", lead.get("country")),
        ("Recent signal", lead.get("signal")),
        ("Signal type", lead.get("signal_type")),
    ]
    for label, val in fields:
        if val:
            parts.append(f"{label}: {val}")

    li = lead.get("_linkedin") or {}
    if isinstance(li, dict):
        for key in ("headline", "about", "location"):
            if li.get(key):
                parts.append(f"LinkedIn {key}: {li[key]}")

    return "\n".join(parts) if parts else "No profile data available."


def _extract_inputs(lead: dict) -> dict:
    """Use Claude to extract structured ICP inputs from the lead's text.

    Returns {} when Claude is unavailable or on any failure — the rubric then
    falls back to whatever structured fields are already on the lead.
    """
    if not claude.available():
        return {}
    profile = _build_profile(lead)
    if profile == "No profile data available.":
        return {}
    try:
        res = claude.complete_json(
            _EXTRACT_SYSTEM,
            f"Extract ICP fields for this company:\n\n{profile}",
            _EXTRACT_SCHEMA,
            max_tokens=512,
        )
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


def main(leads: list[dict] | None = None) -> list[dict]:
    """Score each lead for ICP fit using the deterministic rubric.

    Sets on each lead:
      icp_score      int 0-100
      icp_tier       "A"|"B"|"C"|"D"|"DQ"
      icp_reasoning  human-readable reason string

    Unknown keys are passed through untouched. Leads flagged ``_skip`` (dedup
    bypass) are left as-is.
    """
    if leads is None:
        leads = []

    scored_count = 0
    score_total = 0
    extracted_count = 0

    for lead in leads:
        if lead.get("_skip"):
            continue

        try:
            # 1. Optionally enrich structured inputs from text via Claude.
            #    Only FILL fields the lead doesn't already have (don't override
            #    hard data from enrichment / the feed).
            extracted = _extract_inputs(lead)
            if extracted:
                extracted_count += 1
                merged = dict(lead)
                for k, v in extracted.items():
                    if v in (None, "", "unknown") or merged.get(k) not in (None, ""):
                        continue
                    merged[k] = v
                lead["_icp_inputs"] = {k: merged.get(k) for k in extracted}
            else:
                merged = lead

            # 2. Deterministic rubric score.
            rec = icp_rubric.record_from_lead(merged)
            result = icp_rubric.score_company(rec)

            lead["icp_score"] = result.score
            lead["icp_tier"] = result.tier.name          # "A".."DQ"
            lead["icp_reasoning"] = result.reasoning

            scored_count += 1
            score_total += result.score

        except Exception as exc:  # belt-and-suspenders
            lead.setdefault("_errors", []).append(f"icp.py: {exc}")
            lead["icp_score"] = 0
            lead["icp_tier"] = "DQ"
            lead["icp_reasoning"] = ""

    avg = round(score_total / scored_count, 1) if scored_count else 0

    try:
        log.log_stage(
            "score/icp",
            {"scored": scored_count, "avg": avg, "llm_extracted": extracted_count},
        )
    except Exception:
        pass

    return leads


# ---------------------------------------------------------------------------
# Keyless smoke block
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    fixture_leads = [
        {
            # In-footprint, recent raise, sweet-spot headcount, multi-geo, raising.
            "company": "Acme Cloud", "title": "VP Engineering",
            "country": "US", "operating_countries": ["US", "GB", "DE"],
            "company_size": 120, "funding_stage": "series_a",
            "industry": "b2b saas",
            "months_since_last_funding": 3, "last_funding_eur": 6_000_000,
            "signal": "Acme Cloud raises $8M Series A", "signal_type": "funding",
            "source": "exa_web_search",
        },
        {
            # Out-of-footprint, no signal/size data -> low score.
            "company": "Beta Analytics", "title": "CTO",
            "country": "XX", "company_size": None, "industry": None,
            "signal": None, "signal_type": None,
            "source": "linkedin_visitor",
        },
        {
            # Hard DQ: enterprise size.
            "company": "Gamma Enterprise", "country": "US",
            "company_size": 8000, "industry": "enterprise software",
            "source": "website_visitor",
        },
    ]

    print("icp.py smoke (keyless — no ANTHROPIC_API_KEY expected; rubric still scores):")
    result = main(fixture_leads)
    for ld in result:
        print(f"  {ld['company']:<18} score={ld['icp_score']:>3}  "
              f"tier={ld['icp_tier']:<2}  {ld['icp_reasoning']}")
    print()
    print(json.dumps(result, default=str, indent=2)[:1500])
