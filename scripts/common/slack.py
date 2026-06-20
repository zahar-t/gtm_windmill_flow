"""Slack webhook notifications — smoke-safe.

All functions return False (no-op) when SLACK_WEBHOOK_URL is missing.
Uses common/http.post_json — never raises.
"""
from __future__ import annotations

from scripts.common import config
from scripts.common.http import post_json


def post(text: str, *, blocks: list | None = None) -> bool:
    """POST a message to the configured Slack webhook.

    Returns True on success, False on any failure or when webhook is not set.
    Never raises.
    """
    if not config.SLACK_WEBHOOK_URL:
        return False
    try:
        payload: dict = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        result = post_json(config.SLACK_WEBHOOK_URL, json=payload)
        return result is not None
    except Exception:
        return False


def post_review(lead: dict) -> bool:
    """Post a hot-lead review DM to Slack.

    Includes name / title / company / score / signal / email subject.
    Returns True on success, False otherwise.
    """
    if not config.SLACK_WEBHOOK_URL:
        return False
    try:
        name = lead.get("name") or "Unknown"
        title = lead.get("title") or ""
        company = lead.get("company") or "Unknown"
        score = lead.get("icp_score") or 0
        signal = lead.get("signal") or "—"
        subject = lead.get("email_subject") or "—"

        text = (
            f":fire: *Hot Lead Review*\n"
            f"*Name:* {name}  |  *Title:* {title}  |  *Company:* {company}\n"
            f"*ICP Score:* {score}/100\n"
            f"*Signal:* {signal}\n"
            f"*Email Subject:* {subject}"
        )
        return post(text)
    except Exception:
        return False


def post_reply(lead: dict) -> bool:
    """Notify a human that a lead REPLIED — the hottest handoff in the funnel.

    Fired event-driven at reply detection (the hourly outcomes poller), not in
    the daily flow, so the human hears about it within the hour, not the day.
    """
    if not config.SLACK_WEBHOOK_URL:
        return False
    try:
        name = lead.get("name") or lead.get("email") or "A lead"
        title = lead.get("title") or ""
        company = lead.get("company") or ""
        score = lead.get("icp_score")
        text = (
            ":envelope_with_arrow: *Lead replied — take it over*\n"
            f"*{name}*"
            + (f" ({title})" if title else "")
            + (f" at *{company}*" if company else "")
            + (f"  ·  ICP {score}/100" if score is not None else "")
            + f"\n{lead.get('email', '')}  ·  sequence halted, stage=replied"
        )
        return post(text)
    except Exception:
        return False


def post_intro_request(lead: dict) -> bool:
    """Ask the team to request a warm intro via a relationship investor.

    No webhook -> False. Mirrors post_review shape.
    Text: ':handshake: *Ask <fund> for an intro to <company>*
           <name? title?> · ICP <score>/100 · raised <amount_str?> · signal: <signal>'
    """
    if not config.SLACK_WEBHOOK_URL:
        return False
    try:
        fund = lead.get("lead_investor") or "the investor"
        company = lead.get("company") or "Unknown"
        name = lead.get("name") or ""
        title = lead.get("title") or ""
        score = lead.get("icp_score") or 0
        signal = lead.get("signal") or "—"

        # Funding amount for context (optional)
        amount_eur = lead.get("funding_amount_eur") or lead.get("last_funding_eur")
        if amount_eur:
            try:
                v = float(amount_eur)
                if v >= 1_000_000:
                    amount_str = f"~€{v / 1_000_000:.1f}M".replace(".0M", "M")
                else:
                    amount_str = f"~€{int(v / 1000)}k"
            except (TypeError, ValueError):
                amount_str = None
        else:
            amount_str = None

        person_line = " · ".join(p for p in [name, title] if p)
        amount_part = f" · raised {amount_str}" if amount_str else ""
        text = (
            f":handshake: *Ask {fund} for an intro to {company}*\n"
            + (f"{person_line}" if person_line else "")
            + f" · ICP {score}/100{amount_part}\n"
            + f"signal: {signal}"
        )
        return post(text)
    except Exception:
        return False


def post_summary(counts: dict) -> bool:
    """Post a daily pipeline summary line to Slack.

    Expects keys: run_date, leads_found, leads_enriched, emails_sent, leads_queued.
    Returns True on success, False otherwise.
    """
    if not config.SLACK_WEBHOOK_URL:
        return False
    try:
        run_date = counts.get("run_date", "today")
        found = counts.get("leads_found", 0)
        enriched = counts.get("leads_enriched", 0)
        sent = counts.get("emails_sent", 0)
        queued = counts.get("leads_queued", 0)

        text = (
            f":bar_chart: *Daily Pipeline Summary — {run_date}*\n"
            f"Found: {found}  |  Enriched: {enriched}  |  Sent: {sent}  |  Queued: {queued}"
        )
        return post(text)
    except Exception:
        return False


if __name__ == "__main__":
    print("slack.py smoke (no webhook expected — all return False):")
    print(f"  SLACK_WEBHOOK_URL present: {bool(config.SLACK_WEBHOOK_URL)}")
    print(f"  post('test'):             {post('test')}")
    print(f"  post_review({{}}):         {post_review({})}")
    print(f"  post_summary({{}}):        {post_summary({})}")
