"""email/spam_score.py — Pre-send spam-content scoring (deterministic guard).

Stage 4.5: runs AFTER personalize, BEFORE send. Scores each lead's drafted
email (subject + body) for spam-likelihood with a transparent, SpamAssassin-
style rubric, and HOLDS anything too risky so it never reaches SendGrid (a
spammy send burns domain reputation the warmup ramp spent weeks building).

Mirrors the project's scoring philosophy (cf. score/icp_rubric.py): a
deterministic, auditable points model — every point is explained in
`spam_flags` — not an opaque LLM verdict. Stdlib only, no keys, never raises.

Output per lead (only for leads that have an email_body):
    spam_score   int 0-100   (higher = spammier)
    spam_flags   list[str]   (one entry per rule that fired, with its points)
    spam_verdict "ok" | "review" | "block"
  and, when verdict == "block":
    _hold        "spam_risk" (send.py skips any held lead)
    held_reason  short string

Thresholds come from config (SPAM_REVIEW_THRESHOLD / SPAM_BLOCK_THRESHOLD) so
the feedback loop can tune them as real complaint/reply data accumulates.
"""
from __future__ import annotations

import re

from scripts.common import config, log, node

# ---------------------------------------------------------------------------
# Rule data
# ---------------------------------------------------------------------------
# Classic unsolicited-bulk / promotional trigger phrases. Word-boundary matched,
# case-insensitive. Kept deliberately outbound-sales-aware: ordinary B2B words
# ("demo", "meeting") are NOT here; only phrases that correlate with spam.
_TRIGGER_PHRASES: tuple[str, ...] = (
    "act now", "buy now", "order now", "click here", "click below",
    "limited time", "limited offer", "offer expires", "while supplies last",
    "risk-free", "risk free", "100% free", "100% satisfied", "no cost",
    "no obligation", "money back", "money-back", "satisfaction guaranteed",
    "double your", "earn money", "earn extra cash", "make money",
    "extra income", "work from home", "be your own boss", "cash bonus",
    "free gift", "free trial", "free access", "free quote", "free consultation",
    "winner", "you have been selected", "you've been selected", "congratulations you",
    "this is not spam", "not a scam", "this isn't spam", "dear friend",
    "wire transfer", "bitcoin", "crypto giveaway", "investment opportunity",
    "guaranteed", "lowest price", "best price", "incredible deal",
    "urgent response", "immediate action", "final notice", "last chance",
    "viagra", "weight loss", "lose weight",
)
_TRIGGER_RE = re.compile(
    "|".join(re.escape(p) for p in _TRIGGER_PHRASES), re.IGNORECASE
)

_SHORTENER_RE = re.compile(
    r"\b(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|ow\.ly|buff\.ly|is\.gd|cutt\.ly)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
_IP_URL_RE = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}", re.IGNORECASE)
_FAKE_REPLY_RE = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_OPTOUT_TOKENS = ("unsubscribe", "opt out", "opt-out", "reply stop",
                  "no longer wish", "remove me", "stop hearing")


def _caps_ratio(text: str) -> tuple[float, int]:
    """Return (ALL-CAPS word ratio, all-caps word count) for words len>=2."""
    words = _WORD_RE.findall(text)
    if not words:
        return 0.0, 0
    caps = sum(1 for w in words if w.isupper())
    return caps / len(words), caps


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------
def evaluate(subject: str | None, body: str | None) -> dict:
    """Score one email's content. Pure function — no I/O, never raises.

    Returns {"score": int0-100, "flags": list[str], "verdict": str}.
    """
    subject = subject or ""
    body = body or ""
    full = f"{subject}\n{body}"
    flags: list[str] = []
    pts = 0

    def add(p: int, why: str) -> None:
        nonlocal pts
        pts += p
        flags.append(f"+{p} {why}")

    # --- trigger phrases (capped so one email can't run away on these) ---
    hits = _TRIGGER_RE.findall(full)
    if hits:
        uniq = sorted({h.lower() for h in hits})
        add(min(40, 8 * len(uniq)), f"spam trigger phrase(s): {', '.join(uniq[:5])}")

    # --- subject-line hygiene ---
    if not subject.strip():
        add(20, "empty subject line")
    else:
        slen = len(subject)
        if slen > 90:
            add(10, f"subject very long ({slen} chars)")
        elif slen > 60:
            add(5, f"subject long ({slen} chars)")
        s_ratio, _ = _caps_ratio(subject)
        if s_ratio >= 0.6:
            add(12, "subject mostly ALL-CAPS")
        if _FAKE_REPLY_RE.match(subject):
            add(12, "subject fakes a reply (Re:/Fwd:)")

    # --- body hygiene ---
    if not body.strip():
        add(20, "empty body")

    b_ratio, b_caps = _caps_ratio(body)
    if b_ratio >= 0.25 and b_caps >= 3:
        add(15, f"shouty body ({b_ratio:.0%} ALL-CAPS words)")
    elif b_ratio >= 0.12 and b_caps >= 3:
        add(7, f"some ALL-CAPS shouting ({b_caps} words)")

    # --- punctuation / symbols ---
    excls = full.count("!")
    if excls >= 3:
        add(min(12, 3 + 2 * excls), f"{excls} exclamation marks")
    elif excls == 2:
        add(3, "multiple exclamation marks")
    if re.search(r"!{2,}|\?{2,}|!\?|\?!", full):
        add(6, "excessive/!? punctuation")
    dollars = full.count("$")
    if dollars >= 3:
        add(8, "money symbols ($$$)")

    # --- links ---
    urls = _URL_RE.findall(full)
    if len(urls) > 3:
        add(10, f"{len(urls)} links")
    if _SHORTENER_RE.search(full):
        add(12, "URL shortener (masks destination)")
    if _IP_URL_RE.search(full):
        add(12, "raw IP-address URL")

    # --- CAN-SPAM / cold-outreach hygiene (low weight, informational) ---
    if body.strip() and not any(tok in full.lower() for tok in _OPTOUT_TOKENS):
        add(6, "no opt-out / unsubscribe language")

    score = max(0, min(100, pts))
    if score >= config.SPAM_BLOCK_THRESHOLD:
        verdict = "block"
    elif score >= config.SPAM_REVIEW_THRESHOLD:
        verdict = "review"
    else:
        verdict = "ok"
    return {"score": score, "flags": flags, "verdict": verdict}


# ---------------------------------------------------------------------------
# Stage entrypoint
# ---------------------------------------------------------------------------
def main(leads: list[dict] | None = None) -> list[dict]:
    """Score drafted emails and hold the spammy ones before they reach send.

    Only scores leads that have an `email_body` (i.e. hot/warm with copy). Cold
    leads and leads still missing copy pass through untouched.
    """
    if leads is None:
        leads = []

    scored = ok = review = blocked = 0

    for lead in leads:
        if lead.get("_skip"):
            continue
        body = lead.get("email_body")
        if not body:
            continue

        try:
            res = evaluate(lead.get("email_subject"), body)
        except Exception as exc:  # belt-and-suspenders — evaluate is pure
            lead.setdefault("_errors", []).append(f"spam_score: {exc}")
            continue

        lead["spam_score"] = res["score"]
        lead["spam_flags"] = res["flags"]
        lead["spam_verdict"] = res["verdict"]
        scored += 1

        if res["verdict"] == "block":
            lead["_hold"] = "spam_risk"
            lead["held_reason"] = f"spam_score={res['score']}"
            blocked += 1
            node.dead_letter("email/spam_score", node.SPAM_BLOCK, lead,
                             detail=f"spam_score={res['score']}")
            node.record_run("email/spam_score", lead, node.STATUS_QUARANTINED)
        elif res["verdict"] == "review":
            review += 1
            node.record_run("email/spam_score", lead, node.STATUS_PASSED)  # passed gate, flagged
        else:
            ok += 1
            node.record_run("email/spam_score", lead, node.STATUS_PASSED)

    try:
        log.log_stage(
            "email/spam_score",
            {"scored": scored, "ok": ok, "review": review, "blocked": blocked},
        )
    except Exception:
        pass

    return leads


# ---------------------------------------------------------------------------
# Keyless smoke block
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    fixtures = [
        {
            "_label": "clean B2B (expect ok)",
            "email_subject": "Quick question on your Series A",
            "email_body": (
                "Saw the funding news — congrats. We help B2B SaaS teams accelerate "
                "outbound pipeline without adding headcount. Worth 15 "
                "minutes Thursday?\n\nReply STOP to opt out."
            ),
        },
        {
            "_label": "spammy (expect block)",
            "email_subject": "RE: ACT NOW!! 100% FREE limited time offer!!!",
            "email_body": (
                "CONGRATULATIONS YOU have been SELECTED!! Click here http://bit.ly/x "
                "to claim your FREE cash bonus. Risk-free, guaranteed, no obligation. "
                "Buy now $$$ http://193.43.12.9/win"
            ),
        },
        {
            "_label": "borderline (expect review)",
            "email_subject": "Free consultation — limited time, best price",
            "email_body": "Hi, act now — we offer a free consultation. Click here to book!",
        },
    ]

    print(f"spam_score.py smoke  (review>={config.SPAM_REVIEW_THRESHOLD}, "
          f"block>={config.SPAM_BLOCK_THRESHOLD})\n" + "=" * 64)
    main(fixtures)
    for f in fixtures:
        print(f"  [{f['spam_verdict']:^6}] score={f['spam_score']:>3}  {f['_label']}")
        for fl in f["spam_flags"]:
            print(f"            {fl}")
        if f.get("_hold"):
            print(f"            HELD: {f['held_reason']}")
        print()
    print(json.dumps([{k: v for k, v in f.items() if not k.startswith("email")}
                      for f in fixtures], default=str, indent=2)[:800])
