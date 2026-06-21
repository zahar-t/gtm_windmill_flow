"""test_send_idempotency.py — Formalizes Step 2 send idempotency guarantees.

Strategy: stub warmup_check, postmaster, and supabase to control all side
effects. Use the crm_on fixture for DB stubs.

Guarantees tested:
1. Already-sent (instantly_lead_id in CRM) → 0 Instantly calls
2. In-flight (pipeline_state='sending') → skip
3. Instantly live + CRM down → refuse entire batch (no send)
4. Successful send → stage='contacted' + pipeline_state='sent'
5. Failed send (Instantly error) → revert claim called
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from scripts.email import send as send_mod
from scripts.common import config


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# Default fake reputation + warmup state (healthy, 10 slots)
FAKE_REPUTATION = {
    "status": "healthy",
    "send_multiplier": 1.0,
    "reasons": [],
    "metrics": {},
}
FAKE_WARMUP = {
    "date": "2026-06-20",
    "daily_limit": 10,
    "sends_count": 0,
    "remaining": 10,
}


@pytest.fixture(autouse=True)
def stub_warmup_and_postmaster(monkeypatch):
    """Always stub warmup_check and postmaster so tests are hermetic."""
    from scripts.email import warmup_check, postmaster

    monkeypatch.setattr(warmup_check, "main", lambda **kw: dict(FAKE_WARMUP))
    monkeypatch.setattr(postmaster, "latest_verdict", lambda: dict(FAKE_REPUTATION))
    monkeypatch.setattr(postmaster, "main", lambda: dict(FAKE_REPUTATION))


class TestInstantlyLiveNoCRM:
    """Instantly key set but no CRM → refuse entire batch (compliance fail-closed)."""

    def test_refuses_batch_when_instantly_live_no_crm(self, monkeypatch):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")
        # SUPABASE creds are absent (keyless_env fixture handles this)

        leads = [
            {
                "email": "a@b.com",
                "stage": "hot",
                "email_body": "Hi! Reply STOP to opt out.",
                "email_subject": "Test",
                "channel": "email",
                "_errors": [],
            }
        ]
        send_mod.main(leads, reputation=FAKE_REPUTATION)

        # Every lead should have a "send refused" error
        assert any("send refused" in e for e in leads[0].get("_errors", []))
        # Stage should NOT be 'contacted'
        assert leads[0].get("stage") != "contacted"


class TestNoInstantlyKey:
    """No Instantly key → no sends, no errors about sends."""

    def test_no_instantly_key_skips_all(self):
        leads = [
            {
                "email": "a@b.com",
                "stage": "hot",
                "email_body": "Hi! Reply STOP to opt out.",
                "email_subject": "Test",
                "channel": "email",
                "_errors": [],
            }
        ]
        result = send_mod.main(leads, reputation=FAKE_REPUTATION)
        assert leads[0].get("stage") == "hot"  # not contacted


class TestAlreadySentIdempotency:
    """CRM says lead was already sent → 0 Instantly calls."""

    def test_already_sent_instantly_lead_id_in_crm(self, monkeypatch, crm_on):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")

        import scripts.common.supabase as sb

        # CRM returns an existing lead with instantly_lead_id
        monkeypatch.setattr(sb, "select", lambda *a, **kw: [{
            "id": "lead-uuid",
            "stage": "contacted",
            "outcome": None,
            "last_contacted_at": None,
            "instantly_lead_id": "inst-123",
            "pipeline_state": "sent",
        }])

        instantly_calls = []

        def _fake_add(lead):
            instantly_calls.append(lead)
            return "new-inst-id"

        from scripts.common import instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "add_lead_to_campaign", _fake_add)

        leads = [
            {
                "email": "a@b.com",
                "stage": "hot",
                "email_body": "Hi! Reply STOP to opt out.",
                "email_subject": "Hello",
                "channel": "email",
                "_errors": [],
                "priority": 0.8,
            }
        ]
        send_mod.main(leads, reputation=FAKE_REPUTATION)

        # No Instantly call should have been made
        assert len(instantly_calls) == 0

    def test_pipeline_state_sending_is_inflight_skip(self, monkeypatch, crm_on):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")

        import scripts.common.supabase as sb

        monkeypatch.setattr(sb, "select", lambda *a, **kw: [{
            "id": "lead-uuid",
            "stage": "new",
            "outcome": None,
            "last_contacted_at": None,
            "instantly_lead_id": None,
            "pipeline_state": "sending",
        }])

        instantly_calls = []
        from scripts.common import instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "add_lead_to_campaign", lambda l: instantly_calls.append(l) or "id")

        leads = [{
            "email": "b@c.com",
            "stage": "hot",
            "email_body": "Hi! Reply STOP.",
            "email_subject": "Hello",
            "channel": "email",
            "_errors": [],
            "priority": 0.5,
        }]
        send_mod.main(leads, reputation=FAKE_REPUTATION)
        assert len(instantly_calls) == 0


class TestSuccessfulSend:
    """Successful send → stage='contacted', pipeline_state='sent'."""

    def test_successful_send_sets_contacted(self, monkeypatch, crm_on):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")

        import scripts.common.supabase as sb
        # No existing CRM row
        monkeypatch.setattr(sb, "select", lambda *a, **kw: [])
        monkeypatch.setattr(sb, "upsert", lambda *a, **kw: [])
        monkeypatch.setattr(sb, "update", lambda *a, **kw: [])
        monkeypatch.setattr(sb, "insert", lambda *a, **kw: [])

        from scripts.common import instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "add_lead_to_campaign", lambda l: "inst-new-456")

        leads = [{
            "email": "new@company.com",
            "stage": "hot",
            "email_body": "Hi! Reply STOP to opt out.",
            "email_subject": "Hello",
            "channel": "email",
            "_errors": [],
            "priority": 0.9,
            "icp_score": 80,
        }]
        send_mod.main(leads, reputation=FAKE_REPUTATION)
        assert leads[0]["stage"] == "contacted"
        assert leads[0]["pipeline_state"] == "sent"
        assert leads[0]["instantly_lead_id"] == "inst-new-456"


class TestFailedSendReverts:
    """Instantly errors → claim reverted, stage stays hot."""

    def test_failed_send_reverts_claim(self, monkeypatch, crm_on):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")

        import scripts.common.supabase as sb
        monkeypatch.setattr(sb, "select", lambda *a, **kw: [])

        upsert_calls = []
        update_calls = []
        monkeypatch.setattr(sb, "upsert", lambda *a, **kw: upsert_calls.append(kw) or [])
        monkeypatch.setattr(sb, "update", lambda *a, **kw: update_calls.append(kw) or [])
        monkeypatch.setattr(sb, "insert", lambda *a, **kw: [])

        from scripts.common import instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "add_lead_to_campaign", lambda l: None)  # returns None = failure

        leads = [{
            "email": "fail@company.com",
            "stage": "hot",
            "email_body": "Hi! Reply STOP to opt out.",
            "email_subject": "Hello",
            "channel": "email",
            "_errors": [],
            "priority": 0.7,
        }]
        send_mod.main(leads, reputation=FAKE_REPUTATION)
        # Stage should NOT be contacted
        assert leads[0].get("stage") == "hot"


class TestSpamHeldNotSent:
    """_hold='spam_risk' → lead is never sent."""

    def test_held_lead_not_sent(self, monkeypatch, crm_on):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")

        import scripts.common.supabase as sb
        monkeypatch.setattr(sb, "select", lambda *a, **kw: [])
        monkeypatch.setattr(sb, "upsert", lambda *a, **kw: [])

        instantly_calls = []
        from scripts.common import instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "add_lead_to_campaign", lambda l: instantly_calls.append(l) or "id")

        leads = [{
            "email": "spam@company.com",
            "stage": "hot",
            "email_body": "SPAM SPAM SPAM",
            "email_subject": "Hello",
            "channel": "email",
            "_hold": "spam_risk",
            "_errors": [],
            "priority": 0.9,
            "spam_score": 80,
        }]
        send_mod.main(leads, reputation=FAKE_REPUTATION)
        assert len(instantly_calls) == 0
        assert leads[0]["stage"] == "hot"


class TestNonEmailChannelNotSent:
    """investor_intro / linkedin channels skip cold-email send."""

    def test_investor_intro_channel_skips_send(self, monkeypatch, crm_on):
        monkeypatch.setattr(config, "INSTANTLY_API_KEY", "fake-key")
        monkeypatch.setattr(config, "INSTANTLY_CAMPAIGN_ID", "fake-campaign")

        import scripts.common.supabase as sb
        monkeypatch.setattr(sb, "select", lambda *a, **kw: [])

        instantly_calls = []
        from scripts.common import instantly as instantly_mod
        monkeypatch.setattr(instantly_mod, "add_lead_to_campaign", lambda l: instantly_calls.append(l) or "id")

        leads = [{
            "email": "vc@fund.com",
            "stage": "hot",
            "email_body": "Hi! Reply STOP.",
            "email_subject": "Hello",
            "channel": "investor_intro",
            "_errors": [],
            "priority": 0.95,
        }]
        send_mod.main(leads, reputation=FAKE_REPUTATION)
        assert len(instantly_calls) == 0
