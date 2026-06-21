"""test_node_envelope.py — tests for scripts/common/node.py helpers.

Verifies:
- quarantine() sets _skip + pipeline_state='quarantined'
- dead_letter() and record_run() are no-ops when keyless (never raise)
- has_identity() correct for all identity combos
"""
from __future__ import annotations

import pytest

from scripts.common import node as node_mod
from scripts.common.node import (
    quarantine, dead_letter, record_run, has_identity,
    STATUS_PASSED, STATUS_QUARANTINED, STATUS_SKIPPED,
    DEDUP_UNVERIFIED, EMAIL_INVALID, NO_IDENTITY, SPAM_BLOCK,
)


# ---------------------------------------------------------------------------
# has_identity
# ---------------------------------------------------------------------------
class TestHasIdentity:
    def test_email_sufficient(self):
        assert has_identity({"email": "a@b.com"})

    def test_linkedin_url_sufficient(self):
        assert has_identity({"linkedin_url": "https://linkedin.com/in/x"})

    def test_domain_plus_company_sufficient(self):
        assert has_identity({"domain": "x.com", "company": "X Corp"})

    def test_company_alone_not_sufficient(self):
        assert not has_identity({"company": "X Corp"})

    def test_domain_alone_not_sufficient(self):
        assert not has_identity({"domain": "x.com"})

    def test_empty_dict_false(self):
        assert not has_identity({})

    def test_non_dict_false(self):
        assert not has_identity(None)
        assert not has_identity("string")
        assert not has_identity(42)

    def test_all_none_fields_false(self):
        assert not has_identity({"email": None, "linkedin_url": None, "domain": None, "company": None})


# ---------------------------------------------------------------------------
# quarantine() — mutates lead dict
# ---------------------------------------------------------------------------
class TestQuarantine:
    def test_sets_skip_true(self):
        lead = {"company": "X"}
        quarantine(lead, "enrich/completeness", NO_IDENTITY)
        assert lead["_skip"] is True

    def test_sets_pipeline_state_quarantined(self):
        lead = {"company": "X"}
        quarantine(lead, "enrich/completeness", NO_IDENTITY)
        assert lead["pipeline_state"] == "quarantined"

    def test_sets_skip_reason(self):
        lead = {"company": "X"}
        quarantine(lead, "some/node", "my_reason_code")
        assert lead["_skip_reason"] == "my_reason_code"

    def test_preserves_existing_skip_reason(self):
        """setdefault: existing _skip_reason is preserved."""
        lead = {"company": "X", "_skip_reason": "earlier_reason"}
        quarantine(lead, "some/node", "later_reason")
        assert lead["_skip_reason"] == "earlier_reason"

    def test_never_raises_on_bad_input(self):
        # Should not raise even with weird inputs
        quarantine(None, "node", "code")
        quarantine({}, "node", "code", detail="d")


# ---------------------------------------------------------------------------
# dead_letter() + record_run() — no-op when keyless, never raise
# ---------------------------------------------------------------------------
class TestDeadLetterKeyless:
    def test_dead_letter_no_creds_no_raise(self):
        dead_letter("node/test", NO_IDENTITY, {"email": "x@y.com"}, detail="test")

    def test_dead_letter_none_lead_no_raise(self):
        dead_letter("node/test", NO_IDENTITY, None)

    def test_record_run_no_creds_no_raise(self):
        record_run("node/test", {"email": "x@y.com"}, STATUS_PASSED)

    def test_record_run_none_lead_no_raise(self):
        record_run("node/test", None, STATUS_QUARANTINED)

    def test_dead_letter_with_qa_no_raise(self):
        try:
            from scripts.common.contracts import QAResult
            qa = QAResult.ok()
        except Exception:
            qa = None
        record_run("node/test", {"email": "x@y.com"}, STATUS_PASSED, qa=qa)


# ---------------------------------------------------------------------------
# dead_letter() + record_run() with crm_on — calls are recorded
# ---------------------------------------------------------------------------
class TestDeadLetterWithCRM:
    def test_dead_letter_calls_upsert(self, crm_on):
        dead_letter("crm/dedup", DEDUP_UNVERIFIED, {"email": "x@y.com", "id": None})
        # Check that an upsert call was made to dead_letter table
        upserts = crm_on["upsert"]
        assert any(u["table"] == "dead_letter" for u in upserts)

    def test_record_run_calls_insert(self, crm_on):
        record_run("email/validate", {"email": "x@y.com", "id": None}, STATUS_QUARANTINED)
        inserts = crm_on["insert"]
        assert any(i["table"] == "node_runs" for i in inserts)


# ---------------------------------------------------------------------------
# _snapshot() — payload trimming
# ---------------------------------------------------------------------------
class TestSnapshot:
    def test_drops_email_body(self):
        lead = {"company": "X", "email_body": "Long email content here"}
        snap = node_mod._snapshot(lead)
        assert "email_body" not in snap
        assert "company" in snap

    def test_drops_spam_flags(self):
        lead = {"company": "X", "spam_flags": ["+8 trigger phrase"]}
        snap = node_mod._snapshot(lead)
        assert "spam_flags" not in snap

    def test_non_dict_returns_empty(self):
        assert node_mod._snapshot(None) == {}
        assert node_mod._snapshot("string") == {}
