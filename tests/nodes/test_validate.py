"""test_validate.py — email/validate.py tests with respx mocks."""
from __future__ import annotations

import pytest
import respx
import httpx

from scripts.email import validate


class TestValidateNoKey:
    """No ZEROBOUNCE_API_KEY / NEVERBOUNCE_API_KEY → no-op."""

    def test_no_key_passes_hot_lead(self):
        leads = [{"email": "a@b.com", "stage": "hot"}]
        validate.main(leads)
        assert leads[0].get("email_valid") is None

    def test_no_key_cold_lead_untouched(self):
        leads = [{"email": "a@b.com", "stage": "cold"}]
        validate.main(leads)
        assert leads[0].get("email_valid") is None

    def test_no_key_skip_lead_untouched(self):
        leads = [{"email": "a@b.com", "stage": "hot", "_skip": True}]
        validate.main(leads)
        assert leads[0].get("email_valid") is None

    def test_empty_list_no_crash(self):
        assert validate.main([]) == []


class TestValidateZerobounce:
    """Mock ZeroBounce API calls via respx."""

    def test_valid_email_passes(self, monkeypatch):
        import scripts.common.config as cfg
        monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "fake-zb-key")

        with respx.mock:
            respx.get("https://api.zerobounce.net/v2/validate").mock(
                return_value=httpx.Response(200, json={"status": "valid"})
            )
            leads = [{"email": "good@company.com", "stage": "hot"}]
            validate.main(leads)
            assert leads[0]["email_valid"] is True
            assert leads[0].get("_skip") is not True

    def test_invalid_email_sets_skip_and_demotes(self, monkeypatch):
        import scripts.common.config as cfg
        monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "fake-zb-key")

        with respx.mock:
            respx.get("https://api.zerobounce.net/v2/validate").mock(
                return_value=httpx.Response(200, json={"status": "invalid"})
            )
            leads = [{"email": "bad@spam.com", "stage": "hot"}]
            validate.main(leads)
            assert leads[0]["email_valid"] is False
            assert leads[0]["_skip"] is True
            assert leads[0]["stage"] == "cold"

    def test_catchall_treated_as_valid_by_default(self, monkeypatch):
        import scripts.common.config as cfg
        monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "fake-zb-key")
        monkeypatch.setattr(cfg, "EMAIL_VERIFY_ALLOW_CATCHALL", True)

        with respx.mock:
            respx.get("https://api.zerobounce.net/v2/validate").mock(
                return_value=httpx.Response(200, json={"status": "catch-all"})
            )
            leads = [{"email": "info@bigcorp.com", "stage": "hot"}]
            validate.main(leads)
            assert leads[0]["email_valid"] is True
            assert leads[0].get("_skip") is not True

    def test_spamtrap_is_invalid(self, monkeypatch):
        import scripts.common.config as cfg
        monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "fake-zb-key")

        with respx.mock:
            respx.get("https://api.zerobounce.net/v2/validate").mock(
                return_value=httpx.Response(200, json={"status": "spamtrap"})
            )
            leads = [{"email": "trap@x.com", "stage": "hot"}]
            validate.main(leads)
            assert leads[0]["email_valid"] is False
            assert leads[0]["_skip"] is True
            assert "email_invalid" in leads[0].get("_skip_reason", "")

    def test_warm_lead_also_validated(self, monkeypatch):
        import scripts.common.config as cfg
        monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "fake-zb-key")

        with respx.mock:
            respx.get("https://api.zerobounce.net/v2/validate").mock(
                return_value=httpx.Response(200, json={"status": "invalid"})
            )
            leads = [{"email": "x@y.com", "stage": "warm"}]
            validate.main(leads)
            assert leads[0]["email_valid"] is False
            assert leads[0]["_skip"] is True

    def test_cold_lead_not_validated(self, monkeypatch):
        """Cold leads are not worth validating — skip them."""
        import scripts.common.config as cfg
        monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "fake-zb-key")

        with respx.mock(assert_all_called=False) as mock:
            # No API call should happen for cold leads
            leads = [{"email": "x@y.com", "stage": "cold"}]
            validate.main(leads)
            assert leads[0].get("email_valid") is None
