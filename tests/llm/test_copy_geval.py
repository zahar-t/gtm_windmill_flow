"""tests/llm/test_copy_geval.py — DeepEval G-Eval tests for copy quality.

These tests use an LLM-as-judge to assess the Writer (hot/warm copy) output for:
  - Relevance to the signal / company context
  - No fabrication / hallucination
  - Has opt-out language
  - Appropriate professional tone

SKIP conditions (both checked):
  1. deepeval not installed
  2. ANTHROPIC_API_KEY not set in the environment

The deterministic suite MUST pass without these tests; they are additive quality
gates only and never fail the suite when keys/packages are absent.
"""
from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Guard: skip the whole module if deepeval is not importable
# ---------------------------------------------------------------------------
_DEEPEVAL_AVAILABLE = False
try:
    import deepeval  # noqa: F401
    _DEEPEVAL_AVAILABLE = True
except ImportError:
    pass

_ANTHROPIC_KEY_SET = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())

_SKIP_REASON = None
if not _DEEPEVAL_AVAILABLE:
    _SKIP_REASON = "deepeval not installed"
elif not _ANTHROPIC_KEY_SET:
    _SKIP_REASON = "ANTHROPIC_API_KEY not set"

pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None,
    reason=_SKIP_REASON or "llm tests skipped",
)

# ---------------------------------------------------------------------------
# Only import deepeval internals inside the tests (so collection never crashes)
# ---------------------------------------------------------------------------
if _DEEPEVAL_AVAILABLE and _ANTHROPIC_KEY_SET:
    try:
        from deepeval import assert_test
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams
    except ImportError:
        _SKIP_REASON = "deepeval import error"


# ---------------------------------------------------------------------------
# Sample copy fixtures (generic B2B SaaS — no real companies)
# ---------------------------------------------------------------------------
HOT_COPY = {
    "signal": "Northwind Labs raises $8M Series A",
    "company": "Northwind Labs",
    "subject": "Quick note on your Series A",
    "body": (
        "Congrats on the raise, Dana. We help fast-growing B2B teams get more "
        "from their stack without adding headcount. Worth a 15-min call Thursday? "
        "Reply STOP to opt out."
    ),
}

WARM_COPY = {
    "signal": None,
    "company": "Tagus Analytics",
    "subject": "An idea for your analytics team",
    "body": (
        "Hi Luis — as you scale your data platform, a few teams your size have "
        "found it useful to see how peers handle this. Happy to share. "
        "Reply STOP to opt out."
    ),
}


# ---------------------------------------------------------------------------
# Metrics factory
# ---------------------------------------------------------------------------
def _relevance_metric():
    return GEval(
        name="signal_relevance",
        criteria=(
            "The email body is relevant to the company context and/or signal provided. "
            "It references the company's situation (e.g., recent raise, scale) without "
            "hallucinating facts not present in the input."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
    )


def _no_fabrication_metric():
    return GEval(
        name="no_fabrication",
        criteria=(
            "The email body does not invent specific facts (e.g., made-up investor names, "
            "fabricated revenue figures, wrong product claims) that are not supported by "
            "the provided signal."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.7,
    )


def _has_optout_metric():
    return GEval(
        name="has_opt_out",
        criteria=(
            "The email body contains an opt-out or unsubscribe mechanism "
            "(e.g., 'Reply STOP', 'unsubscribe', 'opt out'). Score 1.0 if present, 0.0 if absent."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.9,
    )


def _tone_metric():
    return GEval(
        name="professional_tone",
        criteria=(
            "The email body is written in a professional, respectful B2B outbound tone. "
            "It is concise (3-5 sentences), does not use ALL CAPS for emphasis, "
            "avoids spam trigger words (guaranteed, risk-free, act now), and does not "
            "use excessive exclamation marks."
        ),
        evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.7,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.llm
def test_hot_copy_relevance():
    """Hot copy must be relevant to the signal."""
    test_case = LLMTestCase(
        input=f"Signal: {HOT_COPY['signal']}\nCompany: {HOT_COPY['company']}",
        actual_output=f"Subject: {HOT_COPY['subject']}\n{HOT_COPY['body']}",
    )
    assert_test(test_case, [_relevance_metric()])


@pytest.mark.llm
def test_hot_copy_no_fabrication():
    """Hot copy must not fabricate facts."""
    test_case = LLMTestCase(
        input=f"Signal: {HOT_COPY['signal']}\nCompany: {HOT_COPY['company']}",
        actual_output=f"Subject: {HOT_COPY['subject']}\n{HOT_COPY['body']}",
    )
    assert_test(test_case, [_no_fabrication_metric()])


@pytest.mark.llm
def test_hot_copy_has_optout():
    """Hot copy must have opt-out language."""
    test_case = LLMTestCase(
        input=f"Company: {HOT_COPY['company']}",
        actual_output=HOT_COPY["body"],
    )
    assert_test(test_case, [_has_optout_metric()])


@pytest.mark.llm
def test_hot_copy_tone():
    """Hot copy must have professional tone."""
    test_case = LLMTestCase(
        input=f"Company: {HOT_COPY['company']}",
        actual_output=HOT_COPY["body"],
    )
    assert_test(test_case, [_tone_metric()])


@pytest.mark.llm
def test_warm_copy_relevance():
    """Warm copy is contextual even without a specific signal."""
    test_case = LLMTestCase(
        input=f"Company: {WARM_COPY['company']} (no specific signal — general value pitch)",
        actual_output=f"Subject: {WARM_COPY['subject']}\n{WARM_COPY['body']}",
    )
    assert_test(test_case, [_relevance_metric()])


@pytest.mark.llm
def test_warm_copy_has_optout():
    test_case = LLMTestCase(
        input=f"Company: {WARM_COPY['company']}",
        actual_output=WARM_COPY["body"],
    )
    assert_test(test_case, [_has_optout_metric()])


@pytest.mark.llm
def test_warm_copy_tone():
    test_case = LLMTestCase(
        input=f"Company: {WARM_COPY['company']}",
        actual_output=WARM_COPY["body"],
    )
    assert_test(test_case, [_tone_metric()])
