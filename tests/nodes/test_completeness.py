"""test_completeness.py — enrich/completeness.py gate tests."""
from __future__ import annotations

import pytest

from scripts.enrich import completeness
from scripts.common import node as node_mod


class TestCompletenessGate:
    def test_company_present_passes(self):
        lead = {"company": "Acme Cloud", "email": None}
        completeness.main([lead])
        assert not lead.get("_skip")

    def test_domain_present_passes(self):
        lead = {"domain": "acme.example", "email": None}
        completeness.main([lead])
        assert not lead.get("_skip")

    def test_both_present_passes(self):
        lead = {"company": "X", "domain": "x.com"}
        completeness.main([lead])
        assert not lead.get("_skip")

    def test_neither_company_nor_domain_quarantines(self):
        lead = {"email": "x@x.com", "name": "Nobody"}
        completeness.main([lead])
        assert lead.get("_skip") is True
        assert lead.get("pipeline_state") == "quarantined"
        assert lead.get("_skip_reason") == node_mod.ENRICH_INCOMPLETE

    def test_quarantined_lead_has_correct_skip_reason(self):
        lead = {"email": "x@x.com"}
        completeness.main([lead])
        assert lead["_skip_reason"] == "enrich_incomplete"

    def test_already_skipped_not_re_quarantined(self):
        lead = {"_skip": True, "_skip_reason": "no_identity", "pipeline_state": "quarantined"}
        completeness.main([lead])
        # Must preserve the ORIGINAL skip reason, not overwrite
        assert lead["_skip_reason"] == "no_identity"

    def test_empty_string_company_quarantines(self):
        """Empty string is falsy — treat as no company."""
        lead = {"company": "", "domain": ""}
        completeness.main([lead])
        assert lead.get("_skip") is True

    def test_empty_list_no_crash(self):
        assert completeness.main([]) == []

    def test_none_list_no_crash(self):
        assert completeness.main(None) == []

    def test_batch_mixed(self):
        leads = [
            {"company": "Good Co"},
            {"email": "x@x.com"},   # no company/domain → quarantine
            {"domain": "y.com"},
        ]
        completeness.main(leads)
        assert not leads[0].get("_skip")
        assert leads[1].get("_skip") is True
        assert not leads[2].get("_skip")
