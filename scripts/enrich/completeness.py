"""enrich/completeness.py — Enrichment completeness gate.

Runs AFTER enrich (waterfall / signals / company) and BEFORE scoring (icp).

A lead is "scoreable" when it has at least a company name OR a domain — the ICP
rubric needs a firmographic anchor to score against. A lead with NEITHER after
enrichment cannot produce a meaningful ICP score and is quarantined here instead
of flowing into scoring where it would silently produce a floor score.

Conservative definition (we gate on the minimum required for ANY scoring, not on
completeness of ALL enrichment signals):
  - company OR domain present  →  scoreable  →  record_run STATUS_PASSED
  - neither present            →  unscoreable →  node.quarantine(..., ENRICH_INCOMPLETE)

Deliberately NOT gating on:
  - email   — cold leads legitimately lack email (waterfall enriches best-effort)
  - signal  — many valid leads have no recent signal and route warm/cold correctly

Never raises; per-lead errors go to lead["_errors"].
"""
from __future__ import annotations

from scripts.common import log, node


def main(leads: list[dict] | None = None) -> list[dict]:
    """Gate leads on enrichment completeness. Mutates and returns the list.

    Scoreable leads (company OR domain) receive STATUS_PASSED in node_runs.
    Unscoreable leads (neither) are quarantined with ENRICH_INCOMPLETE and
    _skip=True so all downstream nodes bypass them.
    """
    if leads is None:
        leads = []

    passed = 0
    quarantined = 0

    for lead in leads:
        if lead.get("_skip"):
            # Already quarantined upstream — leave untouched.
            continue

        has_anchor = bool(lead.get("company") or lead.get("domain"))

        if has_anchor:
            node.record_run("enrich/completeness", lead, node.STATUS_PASSED)
            passed += 1
        else:
            # Build a brief detail string for the dead_letter triage row.
            keys_present = [
                k for k in ("email", "linkedin_url", "name", "signal")
                if lead.get(k)
            ]
            detail = (
                f"no company or domain after enrichment; "
                f"present keys: {keys_present or 'none'}"
            )
            node.quarantine(
                lead,
                "enrich/completeness",
                node.ENRICH_INCOMPLETE,
                detail=detail,
            )
            quarantined += 1

    try:
        log.log_stage(
            "enrich/completeness",
            {"passed": passed, "quarantined": quarantined, "total": len(leads)},
        )
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    # Keyless smoke — all DB writes are no-ops; just verify mutation behaviour.
    print("enrich/completeness.py smoke (keyless):")

    # 1. Lead with company — should pass, no _skip.
    lead_with_company = {"company": "Acme Cloud", "email": None, "_errors": []}
    main([lead_with_company])
    assert not lead_with_company.get("_skip"), "company present → must NOT be quarantined"
    print("  PASS: lead with company → not quarantined")

    # 2. Lead with domain only (no company) — should pass.
    lead_with_domain = {"domain": "acme.example", "email": None, "_errors": []}
    main([lead_with_domain])
    assert not lead_with_domain.get("_skip"), "domain present → must NOT be quarantined"
    print("  PASS: lead with domain only → not quarantined")

    # 3. Lead with NEITHER company NOR domain — must be quarantined.
    lead_bare = {"email": "x@x.com", "name": "Nobody", "_errors": []}
    main([lead_bare])
    assert lead_bare.get("_skip") is True, "no company/domain → must be quarantined"
    assert lead_bare.get("pipeline_state") == "quarantined"
    assert lead_bare.get("_skip_reason") == node.ENRICH_INCOMPLETE
    print("  PASS: lead with neither company nor domain → quarantined")

    # 4. Lead already _skip=True — must be left untouched (no double-quarantine).
    lead_already_skipped = {"_skip": True, "pipeline_state": "quarantined",
                             "_skip_reason": "no_identity", "_errors": []}
    main([lead_already_skipped])
    assert lead_already_skipped.get("_skip_reason") == "no_identity", \
        "_skip leads must not be re-quarantined"
    print("  PASS: already-skipped lead → untouched")

    # 5. Empty list — no crash.
    main([])
    main(None)
    print("  PASS: empty / None list → no crash")

    print("  ALL SMOKE CHECKS PASSED (keyless no-op DB writes)")
