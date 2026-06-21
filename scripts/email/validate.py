"""email/validate.py — pre-send email deliverability verification (node 9).

Runs after route, before personalize: verifies only hot/warm addresses, so it
spends neither a verification credit nor LLM tokens on leads we won't email.
Invalid addresses are demoted (stage='cold' + _skip) so they are never drafted
or sent; in Step 3 these become dead_letter quarantine rows.

Provider-agnostic: ZeroBounce (default) or NeverBounce, selected by
config.EMAIL_VERIFY_PROVIDER. No provider key → no-op (smoke-safe): emails pass
through unvalidated (email_valid stays None).
"""
from __future__ import annotations

from scripts.common import config, log, node
from scripts.common.http import get_json

# Provider statuses we treat as undeliverable.
_ZB_BAD = {"invalid", "spamtrap", "abuse", "do_not_mail"}
_NB_BAD = {"invalid", "disposable"}


def _verify_zerobounce(email: str) -> dict | None:
    if not config.ZEROBOUNCE_API_KEY:
        return None
    data = get_json(
        "https://api.zerobounce.net/v2/validate",
        params={"api_key": config.ZEROBOUNCE_API_KEY, "email": email, "ip_address": ""},
        timeout=15.0,
        retries=1,
    )
    if not isinstance(data, dict) or "status" not in data:
        return None
    s = (data.get("status") or "").lower()
    if s in _ZB_BAD:
        valid = False
    elif s in ("catch-all", "unknown"):
        valid = config.EMAIL_VERIFY_ALLOW_CATCHALL
    else:
        valid = True
    return {"valid": valid, "verdict": s}


def _verify_neverbounce(email: str) -> dict | None:
    if not config.NEVERBOUNCE_API_KEY:
        return None
    data = get_json(
        "https://api.neverbounce.com/v4/single/check",
        params={"key": config.NEVERBOUNCE_API_KEY, "email": email},
        timeout=15.0,
        retries=1,
    )
    if not isinstance(data, dict) or "result" not in data:
        return None
    r = (data.get("result") or "").lower()   # valid|invalid|disposable|catchall|unknown
    if r in _NB_BAD:
        valid = False
    elif r in ("catchall", "unknown"):
        valid = config.EMAIL_VERIFY_ALLOW_CATCHALL
    else:
        valid = True
    return {"valid": valid, "verdict": r}


def evaluate(email: str | None) -> dict | None:
    """Verify a single address. None when validation is unavailable/disabled."""
    if not email:
        return None
    if (config.EMAIL_VERIFY_PROVIDER or "zerobounce").lower() == "neverbounce":
        return _verify_neverbounce(email)
    return _verify_zerobounce(email)


def main(leads: list[dict] | None = None) -> list[dict]:
    """Verify hot/warm addresses; demote invalid ones so they are never sent."""
    if leads is None:
        leads = []

    checked = invalid = 0
    for lead in leads:
        if lead.get("stage") not in ("hot", "warm") or lead.get("_skip"):
            continue
        res = evaluate(lead.get("email"))
        if res is None:                      # disabled / no key / lookup failed
            continue
        checked += 1
        lead["email_valid"] = res["valid"]
        lead["email_validation"] = res["verdict"]
        if not res["valid"]:
            lead["_skip"] = True
            lead["_skip_reason"] = f"email_invalid:{res['verdict']}"
            lead["stage"] = "cold"           # demote: not personalized, not sent
            invalid += 1
            node.dead_letter("email/validate", node.EMAIL_INVALID, lead,
                             detail=f"verdict={res['verdict']}")
            node.record_run("email/validate", lead, node.STATUS_QUARANTINED)
        else:
            node.record_run("email/validate", lead, node.STATUS_PASSED)

    try:
        log.log_stage("email/validate", {"checked": checked, "invalid": invalid})
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    print("=== email/validate.py smoke (keyless → no-op) ===")
    out = main([{"email": "x@y.example", "stage": "hot"}, {"email": None, "stage": "warm"}])
    assert out[0].get("email_valid") is None  # no provider key → unvalidated
    print("PASS: no provider key → hot lead passes through unvalidated")
