"""Stage 3 — Lead routing (hot / warm / cold).

Applies the routing decision from plan.md §C, decision #4 EXACTLY:

  hot  = icp_score > 70  AND  signal present
  cold = icp_score < 40
  warm = everything else
         i.e. 40 <= score <= 70  OR  (score > 70 AND no signal)

Boundary examples (verified in smoke block):
  score=71, signal=True  → hot
  score=71, signal=False → warm   (> 70 but no signal)
  score=70, signal=True  → warm   (needs strictly > 70 for hot)
  score=70, signal=False → warm
  score=40, signal=*     → warm   (boundary: >= 40 is warm)
  score=39, signal=*     → cold
  score=0,  signal=*     → cold

Pure function — always runs regardless of key availability.
Never raises; per-lead errors go to lead["_errors"].
"""
from __future__ import annotations

from scripts.common import log


def _route(score: int, has_signal: bool) -> str:
    """Return 'hot', 'warm', or 'cold' for the given score and signal presence.

    Routing table (from plan.md §C score/route.py spec):
      hot  iff score > 70 AND has_signal
      cold iff score < 40
      warm otherwise  (covers 40–70 inclusive, AND score > 70 with no signal)
    """
    if score > 70 and has_signal:
        return "hot"
    if score < 40:
        return "cold"
    return "warm"


def main(leads: list[dict] | None = None) -> list[dict]:
    """Assign lead["stage"] based on icp_score and signal presence.

    Parameters
    ----------
    leads:
        List of lead dicts following the canonical contract (plan.md section B).
        If None, returns [].

    Returns
    -------
    The same list with lead["stage"] set to 'hot', 'warm', or 'cold'.
    Unknown keys are passed through untouched.
    """
    if leads is None:
        leads = []

    hot = warm = cold = 0

    for lead in leads:
        # Skip leads marked for dedup bypass — preserve their existing stage
        if lead.get("_skip"):
            continue

        try:
            score = lead.get("icp_score") or 0
            try:
                score = int(score)
            except (TypeError, ValueError):
                score = 0

            has_signal = bool(lead.get("signal"))

            stage = _route(score, has_signal)
            lead["stage"] = stage

            if stage == "hot":
                hot += 1
            elif stage == "warm":
                warm += 1
            else:
                cold += 1

        except Exception as exc:  # pragma: no cover — belt-and-suspenders
            lead.setdefault("_errors", []).append(f"route.py: {exc}")
            lead["stage"] = "cold"
            cold += 1

    try:
        log.log_stage("score/route", {"hot": hot, "warm": warm, "cold": cold})
    except Exception:
        pass

    return leads


# ---------------------------------------------------------------------------
# Keyless smoke block — exercises all routing boundaries
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    # Boundary cases from plan.md spec:
    #   score > 70 + signal    → hot
    #   score > 70 + no signal → warm
    #   score == 70 + signal   → warm  (needs strictly > 70)
    #   score == 70 + no sig   → warm
    #   score == 40            → warm  (40 is inclusive lower bound of warm)
    #   score == 39            → cold  (below 40 → cold)
    #   score == 0             → cold
    fixture_leads = [
        # --- HOT ---
        {
            "name": "Pat Doe", "company": "Acme Cloud",
            "email": None, "source": "exa_web_search",
            "icp_score": 71,
            "signal": "Acme Cloud raises $12M Series B",
            "_label": "expect:hot (score=71, signal=True)",
        },
        {
            "name": "Alex River", "company": "Nova SaaS",
            "email": "alex@nova.example", "source": "linkedin_visitor",
            "icp_score": 100,
            "signal": "Nova SaaS launches new AI feature",
            "_label": "expect:hot (score=100, signal=True)",
        },
        # --- WARM ---
        {
            "name": "Jordan Kim", "company": "Beta Analytics",
            "email": "jordan@beta.example", "source": "website_visitor",
            "icp_score": 71,
            "signal": None,
            "_label": "expect:warm (score=71, signal=False — > 70 but no signal)",
        },
        {
            "name": "Sam Lee", "company": "Gamma Tools",
            "email": "sam@gamma.example", "source": "exa_web_search",
            "icp_score": 70,
            "signal": "Gamma Tools is hiring engineers",
            "_label": "expect:warm (score=70, signal=True — not strictly > 70)",
        },
        {
            "name": "Chris Park", "company": "Delta Ops",
            "email": None, "source": "linkedin_visitor",
            "icp_score": 70,
            "signal": None,
            "_label": "expect:warm (score=70, signal=False)",
        },
        {
            "name": "Morgan Chen", "company": "Epsilon Cloud",
            "email": "morgan@epsilon.example", "source": "exa_web_search",
            "icp_score": 55,
            "signal": None,
            "_label": "expect:warm (score=55 — mid-range warm)",
        },
        {
            "name": "Taylor Wu", "company": "Zeta Data",
            "email": "taylor@zeta.example", "source": "exa_web_search",
            "icp_score": 40,
            "signal": None,
            "_label": "expect:warm (score=40 — boundary: >= 40 is warm)",
        },
        # --- COLD ---
        {
            "name": "Casey Nguyen", "company": "Eta Corp",
            "email": None, "source": "website_visitor",
            "icp_score": 39,
            "signal": "Eta Corp hiring sales reps",
            "_label": "expect:cold (score=39 — below 40 boundary)",
        },
        {
            "name": "Riley Fox", "company": "Theta Inc",
            "email": "riley@theta.example", "source": "linkedin_visitor",
            "icp_score": 0,
            "signal": None,
            "_label": "expect:cold (score=0)",
        },
        # --- Edge: None score (treated as 0) ---
        {
            "name": "Drew Stone", "company": "Iota Labs",
            "email": None, "source": "exa_web_search",
            "icp_score": None,
            "signal": "Iota Labs hires 50 engineers",
            "_label": "expect:cold (icp_score=None → 0 → cold)",
        },
    ]

    print("route.py smoke — boundary demonstration (keyless):\n")
    result = main(fixture_leads)
    for lead in result:
        label = lead.get("_label", "")
        stage = lead.get("stage", "?")
        score = lead.get("icp_score")
        signal_present = bool(lead.get("signal"))
        verdict = "OK" if label.startswith(f"expect:{stage}") else "FAIL"
        print(f"  [{verdict}] stage={stage:<4}  score={str(score):<4}  "
              f"signal={str(signal_present):<5}  # {label}")

    print()
    print("Full JSON output (first 2000 chars):")
    # Strip _label keys for cleaner output
    clean = [{k: v for k, v in ld.items() if k != "_label"} for ld in result]
    print(json.dumps(clean, default=str, indent=2)[:2000])
