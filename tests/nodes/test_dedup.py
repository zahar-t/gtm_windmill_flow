"""test_dedup.py — CRM dedup node tests.

Tests:
- terminal outcome → _skip=True + suppressed=True
- DB error (fail-closed) → _skip=True + DEDUP_UNVERIFIED dead_letter call recorded
- keyless (no creds) → all leads pass through with _skip=False
- recency: < 30d → skip; ≥ 30d → pass
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.crm import dedup
from scripts.common import node as node_mod


def _iso(dt: datetime) -> str:
    return dt.isoformat()


NOW = datetime.now(timezone.utc)
RECENT = _iso(NOW - timedelta(days=5))
OLD = _iso(NOW - timedelta(days=45))


class TestDedupKeyless:
    """Without creds → pass every lead through."""

    def test_all_leads_pass_keyless(self):
        leads = [
            {"email": "a@ex.com"},
            {"email": "b@ex.com"},
            {"email": None},
        ]
        dedup.main(leads)
        for lead in leads:
            assert lead["_skip"] is False

    def test_empty_list_ok(self):
        assert dedup.main([]) == []

    def test_none_list_ok(self):
        assert dedup.main(None) == []


class TestDedupWithCRM:
    """With crm_on fixture: supabase calls are stubbed; we control return values."""

    def test_terminal_outcome_reply_suppresses(self, monkeypatch, crm_on):
        """Outcome='reply' in SUPPRESS_OUTCOMES → _skip=True + suppressed=True."""
        import scripts.common.supabase as sb

        def _select_reply(table, filters=None, **kw):
            return [{"id": "abc", "outcome": "reply", "last_contacted_at": None, "stage": "replied"}]

        monkeypatch.setattr(sb, "select", _select_reply)

        lead = {"email": "foo@bar.com"}
        dedup.main([lead])
        assert lead["_skip"] is True
        assert lead.get("suppressed") is True

    def test_terminal_outcome_unsubscribe_suppresses(self, monkeypatch, crm_on):
        import scripts.common.supabase as sb

        def _select_unsub(table, filters=None, **kw):
            return [{"id": "xyz", "outcome": "unsubscribe", "last_contacted_at": None, "stage": "replied"}]

        monkeypatch.setattr(sb, "select", _select_unsub)

        lead = {"email": "bar@baz.com"}
        dedup.main([lead])
        assert lead["_skip"] is True
        assert lead.get("suppressed") is True

    def test_recent_contact_skips(self, monkeypatch, crm_on):
        """last_contacted_at < 30d → _skip=True (recency)."""
        import scripts.common.supabase as sb

        def _select_recent(table, filters=None, **kw):
            return [{"id": "r1", "outcome": None, "last_contacted_at": RECENT, "stage": "contacted"}]

        monkeypatch.setattr(sb, "select", _select_recent)

        lead = {"email": "recent@ex.com"}
        dedup.main([lead])
        assert lead["_skip"] is True
        assert lead.get("_skip_reason", "").startswith("recency")

    def test_old_contact_passes(self, monkeypatch, crm_on):
        """last_contacted_at > 30d → _skip=False (stale enough to re-touch)."""
        import scripts.common.supabase as sb

        def _select_old(table, filters=None, **kw):
            return [{"id": "o1", "outcome": None, "last_contacted_at": OLD, "stage": "contacted"}]

        monkeypatch.setattr(sb, "select", _select_old)

        lead = {"email": "old@ex.com"}
        dedup.main([lead])
        assert lead["_skip"] is False

    def test_no_crm_row_passes(self, monkeypatch, crm_on):
        """No CRM row → _skip=False (new lead)."""
        import scripts.common.supabase as sb
        monkeypatch.setattr(sb, "select", lambda *a, **kw: [])

        lead = {"email": "new@ex.com"}
        dedup.main([lead])
        assert lead["_skip"] is False

    def test_db_error_fail_closed(self, monkeypatch, crm_on, crm_calls):
        """DB error → fail-closed: _skip=True + dead_letter called."""
        import scripts.common.supabase as sb

        def _select_raises(table, filters=None, **kw):
            raise RuntimeError("connection timeout")

        monkeypatch.setattr(sb, "select", _select_raises)

        dl_calls = []

        def _fake_dl(node, reason_code, lead, **kw):
            dl_calls.append({"node": node, "reason_code": reason_code})

        monkeypatch.setattr(node_mod, "dead_letter", _fake_dl)

        lead = {"email": "err@ex.com"}
        dedup.main([lead])

        assert lead["_skip"] is True
        assert "dedup_unverified" in lead.get("_skip_reason", "")
        # dead_letter must have been called with DEDUP_UNVERIFIED
        assert any(c["reason_code"] == node_mod.DEDUP_UNVERIFIED for c in dl_calls)

    def test_no_email_passes_through_with_crm(self, monkeypatch, crm_on):
        """No email → no DB lookup → _skip=False (email-less leads pass)."""
        import scripts.common.supabase as sb
        called = []
        monkeypatch.setattr(sb, "select", lambda *a, **kw: called.append(1) or [])

        lead = {"email": None, "company": "X"}
        dedup.main([lead])
        assert lead["_skip"] is False
        # No select call was made for this lead
        assert len(called) == 0
