"""personalize/warm.py — Sequence B: value-add nurture email for warm leads.

Warm lead criteria: stage == "warm" (icp_score 40-70, or >70 without signal).
Sets: email_subject, email_body, sequence="B".
Leaves non-warm leads unchanged.
Smoke-safe: no ANTHROPIC_API_KEY -> sets email_subject=None, email_body=None,
sequence="B", never raises.
"""
from __future__ import annotations

from scripts.common import config, claude, log
from scripts.personalize import value


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a senior SDR writing thoughtful, value-first nurture emails. "
    "Your emails lead with a useful insight or resource — never with a hard ask. "
    "You are concise, human, and never use buzzwords or fluffy openers. "
    "The goal is to provide genuine value so the prospect remembers you."
)


def _build_user_prompt(lead: dict) -> str:
    name = lead.get("name") or "there"
    title = lead.get("title") or "leader"
    company = lead.get("company") or "your company"
    industry = lead.get("industry") or ""
    company_size = lead.get("company_size") or ""
    signal = lead.get("signal") or ""
    icp_score = lead.get("icp_score") or 0

    # Compute relevance context — None-safe, never fabricated
    v = value.value_line(lead)

    # Build optional context lines
    context_parts = []
    if industry:
        context_parts.append(f"Industry: {industry}")
    if company_size:
        context_parts.append(f"Company size: {company_size}")
    if signal:
        context_parts.append(f"Recent signal: {signal}")
    if v["amount_str"]:
        context_parts.append(f"Funding: raised {v['amount_str']}")
    if v["hook"]:
        context_parts.append(f"Context: {v['hook']}")
    context_block = ("\n" + "\n".join(context_parts)) if context_parts else ""

    # Fabrication guard — only added when figures are present
    no_fabricate_rule = (
        "\n- Do NOT invent funding amounts or statistics; only use numbers provided above."
        if v["amount_str"] else ""
    )

    return (
        f"Write a value-add nurture email (sequence B) for this warm lead.\n\n"
        f"Lead: {name}, {title} at {company}\n"
        f"ICP fit: {icp_score}/100{context_block}\n\n"
        f"Rules:\n"
        f"- Lead with a genuinely useful insight or resource relevant to their role/company\n"
        f"- Explain briefly how our platform could be relevant to their situation\n"
        f"- Soft CTA only (e.g. offer to share more, ask one relevant question) — no hard ask\n"
        f"- Longer and softer than a hot-lead email; 3-5 sentences\n"
        f"- No buzzwords, no \"I hope this finds you well\", no \"synergy\"\n"
        f"- Do not reference things you cannot verify"
        f"{no_fabricate_rule}\n"
        f"- Output: subject line + body only\n\n"
        f"Format your output exactly as:\n"
        f"Subject: <subject line>\n"
        f"<email body>"
    )


# ---------------------------------------------------------------------------
# Subject/body parser (shared logic with hot.py — kept local per file-ownership rules)
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
    """Draft sequence-B emails for warm leads.

    Parameters
    ----------
    leads:
        List of lead dicts from the canonical contract. Non-warm leads pass through
        unchanged. None -> returns [].

    Returns
    -------
    Same list with email_subject, email_body, sequence set on warm leads.
    """
    if leads is None:
        try:
            log.log_stage("personalize/warm", {"drafted": 0})
        except Exception:
            pass
        return []

    drafted = 0

    for lead in leads:
        # Only process warm leads; leave all others completely untouched
        if lead.get("stage") != "warm":
            continue

        # Mark sequence regardless of key availability (plan spec)
        lead["sequence"] = "B"

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
            errors.append(f"personalize/warm error: {exc}")
            lead["email_subject"] = None
            lead["email_body"] = None

    try:
        log.log_stage("personalize/warm", {"drafted": drafted})
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
            "email": "jordan@acme.example",
            "name": "Jordan Lee",
            "title": "Director of Product",
            "company": "Acme AI",
            "company_url": "https://acme.example",
            "domain": "acme.example",
            "source": "exa_web_search",
            "signal": None,
            "signal_type": None,
            "industry": "B2B SaaS",
            "company_size": "120",
            "icp_score": 63,
            "stage": "warm",
            "_errors": [],
        },
        {
            "email": "alex@hotco.example",
            "name": "Alex Rivera",
            "title": "VP of Engineering",
            "company": "HotCo",
            "company_url": "https://hotco.example",
            "domain": "hotco.example",
            "source": "exa_web_search",
            "signal": "HotCo raises $15M Series A",
            "icp_score": 85,
            "stage": "hot",
            "_errors": [],
        },
    ]

    result = main(fixture_leads)
    print(json.dumps(result, default=str)[:2000])
