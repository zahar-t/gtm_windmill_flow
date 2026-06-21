"""test_reply_classify.py — reply_classify.classify() tests.

All class buckets exercised; golden cases loaded from tests/golden/reply_cases.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.common.reply_classify import classify


GOLDEN = json.loads((Path(__file__).parent.parent / "golden" / "reply_cases.json").read_text())


# ---------------------------------------------------------------------------
# Golden fixtures
# ---------------------------------------------------------------------------
class TestGoldenCases:
    @pytest.mark.parametrize("case", GOLDEN, ids=[c.get("expected", "?") + str(i) for i, c in enumerate(GOLDEN)])
    def test_golden(self, case):
        got = classify(case.get("subject"), case.get("body"))
        assert got == case["expected"], (
            f"classify({case.get('subject')!r}, {case.get('body')!r}) "
            f"returned {got!r}, expected {case['expected']!r}"
        )


# ---------------------------------------------------------------------------
# Explicit bucket tests (belt-and-suspenders beyond golden)
# ---------------------------------------------------------------------------
class TestUnsubscribe:
    def test_unsubscribe_keyword(self):
        assert classify(None, "Please unsubscribe me.") == "unsubscribe"

    def test_opt_out(self):
        assert classify(None, "I want to opt out from this list.") == "unsubscribe"

    def test_remove_me(self):
        assert classify(None, "Remove me from your database.") == "unsubscribe"

    def test_do_not_contact(self):
        assert classify(None, "Do not email me again.") == "unsubscribe"

    def test_take_me_off(self):
        assert classify(None, "Please take me off your list.") == "unsubscribe"


class TestOOO:
    def test_out_of_office_subject(self):
        assert classify("Out of Office", "I am out of the office until Monday.") == "ooo"

    def test_auto_reply(self):
        assert classify(None, "Auto-reply: away until next week.") == "ooo"

    def test_back_on_date(self):
        assert classify(None, "I'll be back in the office on Monday.") == "ooo"

    def test_vacation_reply(self):
        assert classify(None, "Vacation reply: I am away.") == "ooo"


class TestNotNow:
    def test_not_right_now(self):
        assert classify(None, "Not right now, thanks.") == "not_now"

    def test_next_quarter(self):
        assert classify(None, "Reach out next quarter.") == "not_now"

    def test_circle_back(self):
        assert classify(None, "Please circle back in a few months.") == "not_now"

    def test_after_funding(self):
        assert classify(None, "After the round closes, reach out again.") == "not_now"


class TestNegative:
    def test_not_interested(self):
        assert classify(None, "Thanks but not interested.") == "negative"

    def test_no_thank_you(self):
        assert classify(None, "No thank you.") == "negative"

    def test_already_have_solution(self):
        assert classify(None, "We already have a solution in place.") == "negative"


class TestInterested:
    def test_lets_chat(self):
        assert classify(None, "Let's set up a call!") == "interested"

    def test_interested_keyword(self):
        assert classify(None, "I'm interested in learning more.") == "interested"

    def test_tell_me_more(self):
        assert classify(None, "Tell me more.") == "interested"

    def test_yes(self):
        assert classify(None, "Yes, send me the details.") == "interested"

    def test_book_a_call(self):
        assert classify(None, "Can we book a call next week?") == "interested"


class TestQuestion:
    def test_pricing_question(self):
        assert classify(None, "What is the pricing structure?") == "question"

    def test_how_does_it_work(self):
        assert classify(None, "How does your platform work?") == "question"

    def test_ends_with_question_mark(self):
        assert classify(None, "Interesting, could you clarify the integration?") == "question"


class TestOther:
    def test_empty(self):
        assert classify(None, None) == "other"

    def test_empty_strings(self):
        assert classify("", "") == "other"

    def test_forwarding(self):
        assert classify(None, "Forwarding this to my colleague.") == "other"
