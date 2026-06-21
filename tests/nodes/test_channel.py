"""test_channel.py — score/channel.py tests."""
from __future__ import annotations

import pytest

from scripts.score.channel import main, _pick_channel
from scripts.common import config


class TestPickChannel:
    def test_email_default_no_config(self):
        lead = {"stage": "hot", "investors": [], "linkedin_url": None}
        assert _pick_channel(lead) == "email"

    def test_investor_intro_when_relationship(self, monkeypatch):
        monkeypatch.setattr(config, "RELATIONSHIP_INVESTORS", ["Top Fund"])
        lead = {"stage": "hot", "investors": ["Top Fund"], "linkedin_url": None}
        assert _pick_channel(lead) == "investor_intro"

    def test_investor_intro_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(config, "RELATIONSHIP_INVESTORS", ["top fund"])
        lead = {"stage": "hot", "investors": ["Top Fund"], "linkedin_url": None}
        assert _pick_channel(lead) == "investor_intro"

    def test_linkedin_when_url_and_unipile_key(self, monkeypatch):
        monkeypatch.setattr(config, "UNIPILE_API_KEY", "fake-key")
        monkeypatch.setattr(config, "RELATIONSHIP_INVESTORS", [])
        lead = {
            "stage": "hot",
            "investors": ["Non-relationship Fund"],
            "linkedin_url": "https://linkedin.com/in/someone",
        }
        assert _pick_channel(lead) == "linkedin"

    def test_email_when_linkedin_but_no_unipile(self, monkeypatch):
        monkeypatch.setattr(config, "UNIPILE_API_KEY", "")
        monkeypatch.setattr(config, "RELATIONSHIP_INVESTORS", [])
        lead = {
            "stage": "hot",
            "linkedin_url": "https://linkedin.com/in/someone",
            "investors": [],
        }
        assert _pick_channel(lead) == "email"

    def test_investor_intro_beats_linkedin(self, monkeypatch):
        """investor_intro wins over linkedin even when both are available."""
        monkeypatch.setattr(config, "RELATIONSHIP_INVESTORS", ["Alpha Fund"])
        monkeypatch.setattr(config, "UNIPILE_API_KEY", "fake-key")
        lead = {
            "stage": "hot",
            "investors": ["Alpha Fund"],
            "linkedin_url": "https://linkedin.com/in/someone",
        }
        assert _pick_channel(lead) == "investor_intro"


class TestChannelMain:
    def test_hot_warm_get_channel_set(self):
        leads = [
            {"stage": "hot", "investors": [], "linkedin_url": None},
            {"stage": "warm", "investors": [], "linkedin_url": None},
        ]
        main(leads)
        for lead in leads:
            assert lead.get("channel") == "email"

    def test_cold_defaults_to_email(self):
        lead = {"stage": "cold", "investors": [], "linkedin_url": None}
        main([lead])
        assert lead["channel"] == "email"

    def test_skip_leads_get_no_channel(self):
        """Skipped leads are not processed — channel not set."""
        lead = {"_skip": True, "stage": "hot", "investors": []}
        main([lead])
        # channel not set on skipped leads
        assert "channel" not in lead

    def test_lead_investor_resolved(self, monkeypatch):
        monkeypatch.setattr(config, "RELATIONSHIP_INVESTORS", ["Big VC"])
        lead = {"stage": "hot", "investors": ["Big VC"], "linkedin_url": None}
        main([lead])
        assert lead["lead_investor"] == "Big VC"
        assert lead["channel"] == "investor_intro"

    def test_empty_list_no_crash(self):
        assert main([]) == []

    def test_none_list_no_crash(self):
        assert main(None) == []
