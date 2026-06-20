"""personalize/followup.py — Sequence F: next-touch nurture for follow-up leads.

Drafts touch 2..N for leads re-injected by crm/lifecycle.py (flagged
`_followup=True`). A follow-up must NOT repeat touch 1 — it opens a new angle,
adds a fresh piece of value, and acknowledges the prior note briefly. The touch
number rides in `sequence_step` so the model can vary the framing.

Sets: email_subject, email_body, sequence=f"F{step}". Leaves non-follow-up
leads untouched. Smoke-safe: no ANTHROPIC_API_KEY → subject/body None, never
raises.
"""
from __future__ import annotations

from scripts.common import claude, log

# Reuse the shared Subject:-line parser from hot.py (single source of truth).
from scripts.personalize.hot import _parse_output

_SYSTEM = (
    "You are a senior SDR writing short, respectful follow-up emails. "
    "A follow-up never repeats the first email — it opens a NEW angle, adds one "
    "fresh useful point, and keeps a soft, no-pressure CTA. You are concise and "
    "human; no buzzwords, no guilt-tripping (\"just bumping this\"), no fluff."
)


def _build_user_prompt(lead: dict) -> str:
    name = lead.get("name") or "there"
    company = lead.get("company") or "your company"
    step = lead.get("sequence_step") or 2
    signal = lead.get("signal") or ""
    industry = lead.get("industry") or ""

    ctx = []
    if signal:
        ctx.append(f"Earlier trigger: {signal}")
    if industry:
        ctx.append(f"Industry: {industry}")
    ctx_block = ("\n" + "\n".join(ctx)) if ctx else ""

    return (
        f"Write follow-up touch #{step} (sequence F) for this lead.\n\n"
        f"Lead: {name} at {company}{ctx_block}\n\n"
        f"Rules:\n"
        f"- Do NOT repeat a first email; open with a fresh angle or new value\n"
        f"- 2–4 sentences, soft CTA only\n"
        f"- Briefly acknowledge you reached out before, without nagging\n"
        f"- No buzzwords, no \"just circling back\", no \"checking in\"\n"
        f"- Output: subject line + body only\n\n"
        f"Format exactly as:\nSubject: <subject line>\n<email body>"
    )


def main(leads: list[dict] | None = None) -> list[dict]:
    """Draft sequence-F follow-up emails for `_followup` leads."""
    if leads is None:
        try:
            log.log_stage("personalize/followup", {"drafted": 0})
        except Exception:
            pass
        return []

    drafted = 0
    for lead in leads:
        if not lead.get("_followup"):
            continue

        step = lead.get("sequence_step") or 2
        lead["sequence"] = f"F{step}"

        if not claude.available():
            lead["email_subject"] = None
            lead["email_body"] = None
            continue

        try:
            raw = claude.complete_text(_SYSTEM, _build_user_prompt(lead), max_tokens=1024)
            subject, body = _parse_output(raw)
            lead["email_subject"] = subject
            lead["email_body"] = body
            if subject or body:
                drafted += 1
        except Exception as exc:
            lead.setdefault("_errors", []).append(f"personalize/followup error: {exc}")
            lead["email_subject"] = None
            lead["email_body"] = None

    try:
        log.log_stage("personalize/followup", {"drafted": drafted})
    except Exception:
        pass
    return leads


if __name__ == "__main__":
    import json
    fixture = [
        {"name": "Dana Reis", "company": "Northwind Labs", "_followup": True,
         "sequence_step": 2, "signal": "raised €2M seed", "stage": "warm", "_errors": []},
        {"name": "Ignore Me", "company": "NotFollowup", "stage": "hot", "_errors": []},
    ]
    print("personalize/followup.py smoke (keyless — subject/body None, sequence set):")
    print(json.dumps(main(fixture), default=str)[:1200])
