"""common/reply_classify.py — Deterministic keyword classifier for inbound reply bodies.

classify(subject, body) -> str

Buckets are:
  interested   — positive buying signal ("let's chat", "tell me more", etc.)
  question     — asking a clarifying question without clear positive/negative intent
  not_now      — deferral / timing objection ("not right now", "next quarter", etc.)
  ooo          — out-of-office auto-reply
  unsubscribe  — opt-out / unsubscribe intent
  negative     — explicit rejection / no interest
  other        — anything that doesn't match above rules

Rules are checked in order — first match wins. Case-insensitive. Pure stdlib (re only).

Usage:
    from scripts.common.reply_classify import classify
    cls = classify(subject="Re: ...", body="Please remove me from your list")
    # -> "unsubscribe"
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Rule tables — (pattern, bucket).  First match wins, IGNORECASE applied.
# Each group is checked independently before falling through to the next group,
# so higher-specificity patterns are listed first within each group.
# ---------------------------------------------------------------------------

# Each entry: (compiled_pattern, bucket_name)
_RULES: list[tuple[re.Pattern, str]] = []


def _add(pattern: str, bucket: str) -> None:
    _RULES.append((re.compile(pattern, re.IGNORECASE | re.DOTALL), bucket))


# ---- 1. OOO (auto-replies) — check early; usually in subject -----------------
_add(r"\bout\s+of\s+(the\s+)?office\b", "ooo")
_add(r"\bauto.?reply\b|\bauto.?response\b|\bautomated\s+reply\b", "ooo")
_add(r"\bI\s+(am|will\s+be)\s+away\b|\bI\s+am\s+currently\s+out\b", "ooo")
_add(r"\bvacation\s+reply\b|\bholiday\s+reply\b|\baway\s+from\s+(my\s+)?email\b", "ooo")
_add(r"\bI\s+(won'?t|will\s+not)\s+be\s+(checking|monitoring|available)\b", "ooo")
_add(r"\breturning\s+on\b|\bback\s+on\b|\bback\s+in\s+the\s+office\b", "ooo")

# ---- 2. UNSUBSCRIBE (opt-out) ------------------------------------------------
_add(r"\bunsubscribe\b", "unsubscribe")
_add(r"\bopt.?out\b", "unsubscribe")
_add(r"\bremove\s+me\s+(from\s+(your|this))?\s*(list|emails?|mailings?|database)\b", "unsubscribe")
_add(r"\bplease\s+(stop|don[''`]?t)\s+(emailing|contacting|sending)\b", "unsubscribe")
_add(r"\bdo\s+not\s+(contact|email|reach\s+out|send)\s+(me|us)\b", "unsubscribe")
_add(r"\btake\s+me\s+off\b", "unsubscribe")
_add(r"\bstop\s+emailing\b|\bstop\s+contacting\b", "unsubscribe")

# ---- 3. NOT_NOW (timing deferral — keep outcome='reply', set re-trigger) -----
_add(r"\bnot\s+(the\s+)?(right\s+)?time\b|\bnot\s+right\s+now\b", "not_now")
_add(r"\bnext\s+quarter\b|\bnext\s+qtr\b|\bq[1-4]\s+\d{4}\b|\bin\s+q[1-4]\b", "not_now")
_add(r"\bcircle\s+back\b|\breach\s+out\s+(again\s+)?in\b", "not_now")
_add(r"\bin\s+a\s+few\s+(weeks|months)\b|\bin\s+\d+\s+(weeks?|months?)\b", "not_now")
_add(r"\bdown\s+the\s+road\b|\brevisit\b|\bcheck\s+back\b", "not_now")
_add(r"\bafter\s+(the\s+)?(round|raise|funding|close)\b", "not_now")
_add(r"\bcurrently\s+(evaluating|in\s+the\s+middle|busy|focused)\b", "not_now")
_add(r"\btoo\s+busy\s+(right\s+now|at\s+the\s+moment)\b", "not_now")
_add(r"\bnot\s+a\s+(priority|focus)\s+(right\s+now|at\s+(the\s+moment|this\s+time))\b", "not_now")

# ---- 4. NEGATIVE (explicit rejection) ----------------------------------------
_add(r"\bnot\s+interested\b|\bno\s+interest\b|\bnot\s+for\s+(us|me|our\s+company)\b", "negative")
_add(r"\bno\s+thank\s+you\b|\bno\s+thanks\b", "negative")
_add(r"\bplease\s+don[''`]?t\s+(contact|reach\s+out|send)\b", "negative")
_add(r"\bnever\s+(contact|email|reach\s+out)\b", "negative")
_add(r"\bnot\s+(a\s+good\s+fit|relevant|applicable)\b", "negative")
_add(r"\bwe\s+(already\s+have|use|are\s+happy\s+with)\b", "negative")
_add(r"\bdon[''`]?t\s+need\s+this\b|\bdon[''`]?t\s+see\s+the\s+value\b", "negative")
_add(r"\bwe['`]?re\s+(going\s+with\s+a\s+competitor|not\s+looking)\b", "negative")

# ---- 5. INTERESTED (positive buying signal) ----------------------------------
_add(r"\blet[''`]?s\s+(chat|talk|connect|schedule|set\s+up\s+a\s+(call|meeting|time))\b", "interested")
_add(r"\binterested\b", "interested")
_add(r"\bbook\s+(a\s+)?(call|meeting|time|slot|demo)\b", "interested")
_add(r"\bschedule\s+(a\s+)?(call|meeting|time|slot|demo)\b", "interested")
_add(r"\bwould\s+love\s+to\s+(hear|learn|see|connect|chat)\b", "interested")
_add(r"\bsend\s+me\s+(more\s+)?(info|information|details|pricing)\b", "interested")
_add(r"\btell\s+me\s+more\b|\blearn\s+more\b", "interested")
_add(r"\bsounds?\s+(great|good|interesting|like\s+(a\s+)?fit)\b", "interested")
_add(r"\bopen\s+to\s+(a\s+)?(call|chat|meeting|discussion|exploring)\b", "interested")
_add(r"\bwhen\s+(are\s+you|can\s+we|is\s+a\s+good\s+time)\b", "interested")
_add(r"\byes[,!\s]", "interested")
_add(r"\babsolutely\b|\bdefin(itely|ately)\b", "interested")
_add(r"\bcan\s+we\s+(set\s+up|hop\s+on|arrange)\b", "interested")

# ---- 6. QUESTION (unclear intent, but asking something) ----------------------
_add(r"\bhow\s+(does|do|much|would|can)\b", "question")
_add(r"\bwhat\s+(is|are|does|would|kind\s+of)\b", "question")
_add(r"\bwho\s+(is|are|would)\b|\bwhere\s+(do|would|can)\b", "question")
_add(r"\bcould\s+you\s+(explain|clarify|tell\s+me|share)\b", "question")
_add(r"\bcan\s+you\s+(send|share|clarify|explain|tell\s+me)\b", "question")
_add(r"\bI\s+(have\s+a\s+question|was\s+wondering)\b", "question")
_add(r"\?\s*$", "question")   # ends with a question mark


def classify(subject: str | None, body: str | None) -> str:
    """Classify an inbound reply into one of 7 buckets.

    Parameters
    ----------
    subject : Subject line of the inbound reply (may be None/empty).
    body    : Plain-text body of the reply (may be None/empty).

    Returns
    -------
    str  One of: interested | question | not_now | ooo | unsubscribe | negative | other
    """
    # Combine subject + body for matching; use a separator to avoid cross-field
    # false positives from pattern breaks at the seam.
    combined = "\n".join(filter(None, [subject or "", body or ""])).strip()
    if not combined:
        return "other"

    for pattern, bucket in _RULES:
        if pattern.search(combined):
            return bucket

    return "other"


if __name__ == "__main__":
    # Smoke tests — assertions printed as PASS/FAIL; exit 0 iff all pass.
    import sys

    _CASES: list[tuple[str | None, str | None, str]] = [
        # (subject, body, expected_bucket)

        # unsubscribe
        ("Re: Quick note", "Please unsubscribe me from your list.", "unsubscribe"),
        (None, "Opt out please, remove me from this mailing list.", "unsubscribe"),
        ("Unsubscribe", "", "unsubscribe"),
        (None, "Do not contact me again.", "unsubscribe"),

        # ooo
        ("Out of Office: Re: Quick note", "I am out of the office until Monday.", "ooo"),
        (None, "Auto-reply: I am currently out and will be back on June 30.", "ooo"),
        ("Automated response", "I won't be checking email until next week.", "ooo"),

        # interested
        ("Re: Quick note", "Sounds interesting, let's set up a call!", "interested"),
        (None, "I'd love to learn more — can we hop on a 15-min call?", "interested"),
        ("Re: Quick idea", "Yes, send me more details.", "interested"),
        (None, "I'm open to a quick chat — when are you free?", "interested"),

        # not_now
        ("Re:", "Not right now, but circle back next quarter.", "not_now"),
        (None, "After the funding round closes, reach out again.", "not_now"),
        (None, "Too busy right now. Check back in a few months.", "not_now"),
        (None, "Not the right time for us, but revisit later.", "not_now"),

        # negative
        ("Re: Quick note", "Thanks but not interested.", "negative"),
        (None, "We already have a solution in place, no thank you.", "negative"),
        (None, "We're not looking for this type of product.", "negative"),

        # question
        ("Re: Quick note", "What is the pricing structure?", "question"),
        (None, "How does your platform integrate with Salesforce?", "question"),

        # other
        (None, None, "other"),
        ("", "", "other"),
        (None, "Forwarding this to my colleague.", "other"),
    ]

    print("=== scripts/common/reply_classify.py smoke ===")
    all_pass = True
    for i, (subj, body, expected) in enumerate(_CASES):
        got = classify(subj, body)
        status = "PASS" if got == expected else "FAIL"
        if got != expected:
            all_pass = False
        snippet = repr((body or "")[:50])
        print(f"  {status}: classify({repr(subj)[:30]}, {snippet}) -> {got!r}  (expected {expected!r})")

    if all_pass:
        print("ALL PASS")
        sys.exit(0)
    else:
        print("SOME FAILURES — see above")
        sys.exit(1)
