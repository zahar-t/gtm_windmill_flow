"""personalize/hot.py — Sequence A: 3-line signal-led cold email for hot leads.

Hot lead criteria: stage == "hot" (icp_score > 70 AND signal present).
Sets: email_subject, email_body, sequence="A".
Leaves non-hot leads unchanged.
Smoke-safe: no ANTHROPIC_API_KEY -> sets email_subject=None, email_body=None,
sequence="A" per plan spec, never raises.
"""
from __future__ import annotations

from scripts.common import config, claude, log
from scripts.personalize import value


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a senior SDR writing concise, signal-led cold emails. "
    "You write with clarity and brevity — no buzzwords, no fluffy openers. "
    "Every email you write earns a reply because it is hyper-relevant."
)


def _build_user_prompt(lead: dict) -> str:
    name = lead.get("name") or "there"
    title = lead.get("title") or "leader"
    company = lead.get("company") or "your company"
    signal = lead.get("signal") or ""
    icp_score = lead.get("icp_score") or 0

    # Compute relevance context — None-safe, never fabricated
    v = value.value_line(lead)

    # Build optional context lines
    context_lines: list[str] = []
    if v["amount_str"]:
        context_lines.append(f"Funding: raised {v['amount_str']}")
    if v["hook"]:
        context_lines.append(f"Context: {v['hook']}")
    context_block = ("\n" + "\n".join(context_lines)) if context_lines else ""

    # Fabrication guard — only added when figures are present
    no_fabricate_rule = (
        "\n- Do NOT invent funding amounts or statistics; only use numbers provided above."
        if context_lines else ""
    )

    return (
        f"You are a senior SDR. Write a cold email for this lead.\n\n"
        f"Lead: {name}, {title} at {company}\n"
        f"Signal: {signal}\n"
        f"ICP fit: {icp_score}/100{context_block}\n\n"
        f"Rules:\n"
        f"- 3 lines max (sequence A)\n"
        f"- Reference the specific signal in line 1\n"
        f"- Explain briefly why our platform is relevant to them now\n"
        f"- One clear CTA in line 3\n"
        f"- No buzzwords, no \"I hope this finds you well\""
        f"{no_fabricate_rule}\n"
        f"- Output: subject line + body only\n\n"
        f"Format your output exactly as:\n"
        f"Subject: <subject line>\n"
        f"<line 1>\n"
        f"<line 2>\n"
        f"<line 3>"
    )


# ---------------------------------------------------------------------------
# Subject/body parser
# ---------------------------------------------------------------------------

def _parse_output(text: str) -> tuple[str | None, str | None]:
    """Parse Claude output into (subject, body).

    Looks for a line starting with 'Subject:' (case-insensitive).
    Everything after that line (stripped) is the body.
    If no Subject line found: first non-empty line = subject, rest = body.
    Returns (None, None) if text is empty.
    """
    if not text or not text.strip():
        return None, None

    lines = text.strip().splitlines()

    # Find a Subject: line
    subject: str | None = None
    subject_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith("subject:"):
            subject = stripped[len("subject:"):].strip()
            subject_idx = i
            break

    if subject is not None and subject_idx is not None:
        body_lines = [l for l in lines[subject_idx + 1:] if l.strip()]
        body = "\n".join(body_lines).strip() or None
        return subject or None, body

    # No explicit Subject line — first non-empty line becomes subject
    non_empty = [l.strip() for l in lines if l.strip()]
    if not non_empty:
        return None, None
    subject = non_empty[0]
    body = "\n".join(non_empty[1:]).strip() or None
    return subject, body


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(leads: list[dict] | None = None) -> list[dict]:
    """Draft sequence-A emails for hot leads.

    Parameters
    ----------
    leads:
        List of lead dicts from the canonical contract. Non-hot leads pass through
        unchanged. None -> returns [].

    Returns
    -------
    Same list with email_subject, email_body, sequence set on hot leads.
    """
    if leads is None:
        try:
            log.log_stage("personalize/hot", {"drafted": 0})
        except Exception:
            pass
        return []

    drafted = 0

    for lead in leads:
        # Only process hot leads; leave all others completely untouched
        if lead.get("stage") != "hot":
            continue

        # Mark sequence regardless of key availability (plan spec)
        lead["sequence"] = "A"

        if not claude.available():
            # Smoke / no-key path: set None fields, do not raise
            lead["email_subject"] = None
            lead["email_body"] = None
            continue

        try:
            system_prompt = _SYSTEM
            user_prompt = _build_user_prompt(lead)
            raw = claude.complete_text(system_prompt, user_prompt, max_tokens=1024)
            subject, body = _parse_output(raw)
            lead["email_subject"] = subject
            lead["email_body"] = body
            if subject or body:
                drafted += 1
        except Exception as exc:
            errors = lead.setdefault("_errors", [])
            errors.append(f"personalize/hot error: {exc}")
            lead["email_subject"] = None
            lead["email_body"] = None

    try:
        log.log_stage("personalize/hot", {"drafted": drafted})
    except Exception:
        pass

    return leads


# ---------------------------------------------------------------------------
# Keyless smoke block — must compile and run to completion with no env vars
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    fixture_leads = [
        {
            "email": "alex@acme.example",
            "name": "Alex Rivera",
            "title": "VP of Engineering",
            "company": "Acme AI",
            "company_url": "https://acme.example",
            "domain": "acme.example",
            "source": "exa_web_search",
            "signal": "Acme AI raises $12M Series B to expand platform",
            "signal_type": "funding",
            "icp_score": 82,
            "stage": "hot",
            "_errors": [],
        },
        {
            "email": "pat@warmco.example",
            "name": "Pat Smith",
            "title": "Head of Sales",
            "company": "WarmCo",
            "company_url": "https://warmco.example",
            "domain": "warmco.example",
            "source": "exa_web_search",
            "signal": None,
            "icp_score": 55,
            "stage": "warm",
            "_errors": [],
        },
    ]

    result = main(fixture_leads)
    print(json.dumps(result, default=str)[:2000])
