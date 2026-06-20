"""crm/handoff.py — Channel-aware post-send handoff to Slack.

For investor_intro leads (hot/warm, sequence A or B): fire a warm-intro ask
to Slack via slack.post_intro_request(). These skip cold email (enforced in
send.py), so the Slack message IS the action for the operator.

For email-channel hot leads (sequence A): fire slack.post_review() as before,
gated on channel != 'investor_intro' to avoid double-Slacking.

Keyless (no SLACK_WEBHOOK_URL) -> no-op. Returns leads unchanged.

Node-envelope note: wire as current-style main(leads)->leads; Step 3 adds
gate/dead_letter for failed Slack posts.
"""
from __future__ import annotations

from scripts.common import config, log, slack


def main(leads: list[dict] | None = None) -> list[dict]:
    """Channel-aware Slack handoff.

    investor_intro leads (hot/warm, seq A or B):
      -> slack.post_intro_request(lead)   [skip cold email enforced in send.py]
    email-channel hot leads (seq A, stage hot/contacted):
      -> slack.post_review(lead)          [gated: channel != investor_intro]

    No webhook -> all Slack calls return False (no-op). Leads unchanged.
    """
    if leads is None:
        leads = []

    intro_requests = 0
    reviews = 0

    for lead in leads:
        ch = lead.get("channel")
        stage = lead.get("stage") or ""
        seq = lead.get("sequence") or ""

        # investor_intro: warm intro ask for hot/warm leads with sequence A or B
        if ch == "investor_intro" and stage in {"hot", "warm", "contacted"} and seq in {"A", "B"}:
            try:
                slack.post_intro_request(lead)
                intro_requests += 1
            except Exception as exc:
                lead.setdefault("_errors", []).append(f"handoff intro_request error: {exc}")

        # email-channel hot leads: standard review alert (no double-Slack for intro leads)
        elif seq == "A" and stage in {"hot", "contacted"} and ch != "investor_intro":
            try:
                slack.post_review(lead)
                reviews += 1
            except Exception as exc:
                lead.setdefault("_errors", []).append(f"handoff slack error: {exc}")

    try:
        log.log_stage("crm/handoff", {"intro_requests": intro_requests, "reviews": reviews})
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    fixture_leads = [
        {
            "email": "pat@acme.example",
            "name": "Pat Doe",
            "company": "Acme Cloud",
            "title": "CTO",
            "icp_score": 85,
            "signal": "Acme Cloud raises $10M Series A",
            "email_subject": "Congrats on the raise",
            "stage": "contacted",
            "sequence": "A",
        },
        {
            "email": "sam@widget.example",
            "name": "Sam Smith",
            "company": "Widget Corp",
            "title": "VP Sales",
            "icp_score": 55,
            "signal": None,
            "email_subject": "Useful resource for Widget Corp",
            "stage": "warm",
            "sequence": "B",
        },
        {
            "email": "alex@demo.example",
            "name": "Alex Builder",
            "company": "Demo Inc",
            "title": "CEO",
            "icp_score": 30,
            "signal": None,
            "stage": "cold",
            "sequence": None,
        },
    ]

    print("=== crm/handoff.py smoke (keyless — no Slack webhook) ===")
    print("Expected: 1 hot lead eligible, post_review returns False (no webhook), leads unchanged")
    result = main(fixture_leads)
    print(json.dumps(result, default=str, indent=2)[:2000])
