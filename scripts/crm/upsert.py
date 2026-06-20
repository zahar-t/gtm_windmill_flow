"""crm/upsert.py — Upsert leads into Supabase + write activity rows.

Persists only the columns that exist in the leads table (per plan section B).
Leads without an email are SKIPPED (cannot upsert on email conflict).
company_size, industry, tech_stack, icp_reasoning, email_subject, email_body,
sequence, domain, signal_type, and _* keys are NOT stored.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.common import config, log

# Columns that exist in the leads table (plan section B persisted-columns list)
_LEAD_COLUMNS = {
    "email",
    "name",
    "company",
    "company_url",
    "title",
    "linkedin_url",
    "source",
    "signal",
    "icp_score",
    "stage",
    "last_contacted_at",
    "enriched_at",
    # funding / priority / channel (Area 1-3, migration 002)
    "signal_type",
    "signal_ts",
    "intent_score",
    "priority",
    "segment",
    "funding_amount_eur",
    "funding_round",
    "funding_announced_at",
    "lead_investor",
    "channel",
    "deal_value_eur",
    "funding_bracket",
    # re-trigger nurture (Area 4, migration 002)
    "re_trigger_at",
    "re_trigger_reason",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_row(lead: dict) -> dict:
    """Extract only the columns that exist in the leads table.

    Derives funding_bracket from funding_amount_eur if not already set on the lead.
    'investors' (an array) is NOT a leads column — it flows to the investor graph
    via investors.persist_graph (§4.1).
    """
    row: dict = {}
    for col in _LEAD_COLUMNS:
        if col in lead:
            row[col] = lead[col]

    # Derive funding_bracket if not already set
    if not row.get("funding_bracket"):
        amount = lead.get("funding_amount_eur")
        if amount is not None:
            try:
                from scripts.report.daily_summary import funding_bracket
                row["funding_bracket"] = funding_bracket(amount)
            except Exception:
                pass

    row["updated_at"] = _now_iso()
    return row


def main(leads: list[dict] | None = None) -> list[dict]:
    """Upsert each lead (with email) into Supabase leads table.

    For each upserted lead:
      - Captures returned id → lead["id"]
      - Inserts activity row type=stage_change
      - If lead has a signal, inserts activity row type=signal_found

    Leads without email are skipped with a flag in _errors.
    No creds → returns leads unchanged (no id set, no network calls).
    """
    if leads is None:
        leads = []

    upserted = 0
    skipped_no_email = 0

    no_creds = not (config.SUPABASE_URL and config.SUPABASE_KEY)

    for lead in leads:
        email = lead.get("email")

        if not email:
            lead.setdefault("_errors", []).append(
                "upsert skipped: no email (cannot upsert on email conflict)"
            )
            skipped_no_email += 1
            continue

        if no_creds:
            # Smoke path — nothing to upsert
            continue

        try:
            from scripts.common import supabase

            row = _build_row(lead)
            returned = supabase.upsert("leads", row, on_conflict="email")
            if returned:
                lead["id"] = returned[0].get("id")

            upserted += 1

            # Activity: stage change
            stage = lead.get("stage")
            lead_id = lead.get("id")
            if lead_id and stage:
                try:
                    supabase.insert(
                        "activity",
                        {
                            "lead_id": lead_id,
                            "type": "stage_change",
                            "payload": {"to": stage},
                        },
                    )
                except Exception as act_exc:
                    lead.setdefault("_errors", []).append(
                        f"activity stage_change error: {act_exc}"
                    )

            # Activity: signal found
            signal = lead.get("signal")
            if lead_id and signal:
                try:
                    supabase.insert(
                        "activity",
                        {
                            "lead_id": lead_id,
                            "type": "signal_found",
                            "payload": {
                                "signal": signal,
                                "type": lead.get("signal_type"),
                            },
                        },
                    )
                except Exception as sig_exc:
                    lead.setdefault("_errors", []).append(
                        f"activity signal_found error: {sig_exc}"
                    )

        except Exception as exc:
            lead.setdefault("_errors", []).append(f"upsert error: {exc}")

    try:
        log.log_stage(
            "crm/upsert",
            {"upserted": upserted, "skipped_no_email": skipped_no_email},
        )
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
            "company_url": "https://acme.example",
            "title": "CTO",
            "linkedin_url": None,
            "source": "exa_web_search",
            "signal": "Acme Cloud raises $10M Series A",
            "signal_type": "funding",
            "icp_score": 85,
            "icp_reasoning": "Strong ICP fit — Series A SaaS with hiring signals",
            "stage": "hot",
            "enriched_at": "2026-06-16T08:00:00+00:00",
            "last_contacted_at": None,
            # Non-column fields (should NOT be stored)
            "company_size": "50-200",
            "industry": "SaaS",
            "tech_stack": ["Python", "Postgres"],
            "email_subject": "Congrats on the raise",
            "email_body": "Hi Pat...",
            "sequence": "A",
            "domain": "acme.example",
        },
        {
            # No email — should be skipped
            "email": None,
            "name": "Alex Builder",
            "company": "Widget Corp",
            "company_url": "https://widget.example",
            "source": "exa_web_search",
            "stage": "warm",
        },
    ]

    print("=== crm/upsert.py smoke (keyless — no Supabase creds) ===")
    print("Expected: emailless lead flagged, email lead passed through (no id set)")
    result = main(fixture_leads)
    print(json.dumps(result, default=str, indent=2)[:2000])
