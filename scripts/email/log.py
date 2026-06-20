"""email/log.py — Record sent_at/subject/score/source per sent lead.

For each lead where stage == "contacted":
  - Writes a structured send record via common/log.log_stage (file sink, always)
  - If Supabase creds present AND lead["id"] exists, inserts an activity row
    (type="email_sent") so the DB also has a full send audit trail.

Returns leads unchanged.

Smoke-safe: if Supabase creds absent, only the file log is written. Never raises.

def main(leads: list[dict] | None = None) -> list[dict]
"""
from __future__ import annotations

from scripts.common import config
from scripts.common import supabase
from scripts.common import log as common_log


def main(leads: list[dict] | None = None) -> list[dict]:
    """Log send records for all contacted leads.

    Parameters
    ----------
    leads:
        List of lead dicts from the canonical lead-dict contract. None → [].

    Returns
    -------
    The same list unchanged.
    """
    if leads is None:
        leads = []

    logged = 0
    has_db = bool(config.SUPABASE_URL and config.SUPABASE_KEY)

    for lead in leads:
        if lead.get("stage") != "contacted":
            continue

        record = {
            "sent_at": lead.get("last_contacted_at"),
            "subject": lead.get("email_subject"),
            "score": lead.get("icp_score"),
            "source": lead.get("source"),
            "sequence": lead.get("sequence"),
        }

        # Always write to the file-based daily log
        try:
            common_log.log_stage("email/log", record)
        except Exception:
            pass

        # Write activity row to Supabase when creds + id are available
        if has_db and lead.get("id"):
            try:
                supabase.insert(
                    "activity",
                    {
                        "lead_id": lead["id"],
                        "type": "email_sent",
                        "payload": record,
                    },
                )
            except Exception:
                pass

        logged += 1

    summary = {"logged": logged}
    try:
        common_log.log_stage("email/log", summary)
    except Exception:
        pass

    return leads


if __name__ == "__main__":
    import json

    print("=== email/log.py smoke (no keys, no network) ===")
    # Fictional fixture only — no real data
    fixture = [
        {
            "email": "pat@acme.example",
            "name": "Pat Doe",
            "company": "Acme Cloud",
            "source": "exa_web_search",
            "stage": "contacted",
            "email_subject": "Saw Acme Cloud's Series B — congrats",
            "icp_score": 82,
            "sequence": "A",
            "last_contacted_at": "2026-06-16T08:00:00+00:00",
            "id": None,
            "_errors": [],
        },
        {
            "email": "alex@cold.example",
            "name": "Alex Cold",
            "company": "Cold Corp",
            "source": "exa_web_search",
            "stage": "cold",
            "email_subject": None,
            "icp_score": 20,
            "sequence": None,
            "last_contacted_at": None,
            "id": None,
            "_errors": [],
        },
    ]

    result = main(fixture)
    print(json.dumps(result, default=str)[:2000])
    print()
    print(f"Leads returned unchanged: {len(result)}")
    print(f"  Lead 0 stage={result[0]['stage']} (contacted — should be logged)")
    print(f"  Lead 1 stage={result[1]['stage']} (cold — should be skipped)")
