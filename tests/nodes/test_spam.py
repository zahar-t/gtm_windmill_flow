"""test_spam.py — spam_score.py tests.

evaluate() is a pure function — no network, no mocks needed.
Tests cover: clean copy → ok/review, spammy → block + _hold.
"""
from __future__ import annotations

import pytest

from scripts.email.spam_score import evaluate, main
from scripts.common import config


# ---------------------------------------------------------------------------
# evaluate() — pure function
# ---------------------------------------------------------------------------
class TestEvaluate:
    def test_clean_b2b_email_is_ok(self):
        result = evaluate(
            "Quick question on your Series A",
            "Saw the funding news — congrats. We help B2B SaaS teams accelerate "
            "outbound pipeline without adding headcount. Worth 15 "
            "minutes Thursday?\n\nReply STOP to opt out.",
        )
        assert result["verdict"] == "ok"
        assert result["score"] < config.SPAM_REVIEW_THRESHOLD

    def test_spammy_email_is_blocked(self):
        result = evaluate(
            "RE: ACT NOW!! 100% FREE limited time offer!!!",
            "CONGRATULATIONS YOU have been SELECTED!! Click here http://bit.ly/x "
            "to claim your FREE cash bonus. Risk-free, guaranteed, no obligation. "
            "Buy now $$$ http://193.43.12.9/win",
        )
        assert result["verdict"] == "block"
        assert result["score"] >= config.SPAM_BLOCK_THRESHOLD

    def test_borderline_review(self):
        result = evaluate(
            "Free consultation — limited time, best price",
            "Hi, act now — we offer a free consultation. Click here to book!",
        )
        assert result["verdict"] in ("review", "block")
        assert result["score"] >= config.SPAM_REVIEW_THRESHOLD

    def test_empty_subject_adds_points(self):
        result = evaluate("", "Some body text. Unsubscribe here.")
        flags_text = " ".join(result["flags"])
        assert "empty subject" in flags_text
        assert result["score"] >= 20

    def test_empty_body_adds_points(self):
        result = evaluate("Normal subject", "")
        assert result["score"] >= 20

    def test_no_optout_adds_points(self):
        result = evaluate("Subject", "Body with no optout language.")
        flags_text = " ".join(result["flags"])
        assert "opt-out" in flags_text or "unsubscribe" in flags_text or "no opt" in flags_text.lower()

    def test_url_shortener_adds_points(self):
        result = evaluate("Subject", "Check this out bit.ly/xyz — unsubscribe anytime.")
        flags_text = " ".join(result["flags"])
        assert "shortener" in flags_text or "bit.ly" in flags_text.lower() or any("URL" in f for f in result["flags"])

    def test_score_bounds(self):
        """Score is always 0-100."""
        for subject, body in [
            (None, None),
            ("", ""),
            ("Hello", "Hi there. Unsubscribe below."),
            ("ACT NOW!!! FREE!!!", "WIN WIN WIN $$$"),
        ]:
            r = evaluate(subject, body)
            assert 0 <= r["score"] <= 100

    def test_none_inputs_handled(self):
        result = evaluate(None, None)
        assert "score" in result
        assert "verdict" in result


# ---------------------------------------------------------------------------
# main() — pipeline entrypoint
# ---------------------------------------------------------------------------
class TestSpamMain:
    def test_spammy_lead_gets_hold(self):
        lead = {
            "email_subject": "RE: ACT NOW!! FREE!!!",
            "email_body": "CONGRATULATIONS!! Click here http://bit.ly/x. Buy now $$$. "
                          "Guaranteed! Risk-free! no obligation!",
        }
        main([lead])
        assert lead.get("_hold") == "spam_risk"
        assert lead["spam_verdict"] == "block"

    def test_clean_lead_not_held(self):
        lead = {
            "email_subject": "Your Series B — congrats",
            "email_body": "Saw your raise. We help B2B teams scale outbound. 15 min Thursday?\n\nReply STOP to opt out.",
        }
        main([lead])
        assert lead.get("_hold") is None
        assert lead["spam_verdict"] in ("ok", "review")

    def test_lead_without_body_skipped(self):
        lead = {"email_subject": "Subject", "stage": "hot"}
        main([lead])
        assert "spam_score" not in lead

    def test_skip_lead_not_scored(self):
        lead = {"_skip": True, "email_body": "WIN WIN WIN guaranteed!"}
        main([lead])
        assert "spam_score" not in lead

    def test_review_verdict_lead_not_held(self):
        """Review verdict = flagged but not blocked — still sendable."""
        result = evaluate(
            "Free consultation — limited time, best price",
            "Hi, act now — we offer a free consultation. Click here to book!",
        )
        if result["verdict"] == "review":
            lead = {
                "email_subject": "Free consultation — limited time, best price",
                "email_body": "Hi, act now — we offer a free consultation. Click here to book!",
            }
            main([lead])
            assert lead.get("_hold") is None
            assert lead["spam_verdict"] == "review"
