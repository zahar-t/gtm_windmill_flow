"""Anthropic API helper — official SDK wrapper.

Import is always safe; if the `anthropic` package is absent the module
degrades (available() returns False) so keyless QA can import without issues.

All public functions are synchronous.

Design notes
------------
- Client is built lazily inside each function — no module-level network touch.
- complete_json instructs the model to reply with ONLY valid JSON, then
  extracts+parses JSON from the response (tolerates code fences). Returns {}
  on any failure.
- No temperature / top_p / thinking / budget_tokens — claude-sonnet-4-6 / 4.x
  family rejects those sampling params.
"""
from __future__ import annotations

import json
import re
import time

from scripts.common import config

# --- optional import guard ---
try:
    import anthropic as _anthropic_lib
    _ANTHROPIC_OK = True
except Exception:
    _anthropic_lib = None  # type: ignore[assignment]
    _ANTHROPIC_OK = False

_MAX_RETRIES = 2          # extra attempts after the first (mirrors http.py policy)
_BASE_BACKOFF = 0.5
_MAX_BACKOFF = 8.0        # capped exponential

# ---------------------------------------------------------------------------
# Per-run LLM cost tracking (Task 3)
# Prices for claude-sonnet-4-6 — edit here if pricing changes.
# ---------------------------------------------------------------------------
_COST_INPUT_PER_M  = 3.0    # USD per 1M input tokens
_COST_OUTPUT_PER_M = 15.0   # USD per 1M output tokens

_total_input_tokens:  int = 0
_total_output_tokens: int = 0


def _accumulate(msg) -> None:
    """Add token usage from a successful API response. Safe if usage is absent."""
    global _total_input_tokens, _total_output_tokens
    try:
        usage = getattr(msg, "usage", None)
        if usage is None:
            return
        _total_input_tokens  += int(getattr(usage, "input_tokens",  0) or 0)
        _total_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
    except Exception:
        pass


def cost_usd_total() -> float:
    """Estimated USD cost of all LLM calls this process made.

    Returns 0.0 when no calls succeeded or the anthropic package is absent.
    Never raises.
    """
    try:
        return (
            _total_input_tokens  / 1_000_000 * _COST_INPUT_PER_M
            + _total_output_tokens / 1_000_000 * _COST_OUTPUT_PER_M
        )
    except Exception:
        return 0.0


def reset_cost() -> None:
    """Reset run-level counters (call at the start of each pipeline run if needed)."""
    global _total_input_tokens, _total_output_tokens
    _total_input_tokens = 0
    _total_output_tokens = 0


def _backoff(attempt: int) -> float:
    return min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)


def _is_transient(exc: Exception) -> bool:
    """True for overloaded(529)/rate-limit(429)/5xx/timeout/connection errors."""
    status = getattr(exc, "status_code", None)
    if status in (429, 529) or (isinstance(status, int) and 500 <= status < 600):
        return True
    if _anthropic_lib is not None:
        for attr in ("APITimeoutError", "APIConnectionError", "RateLimitError",
                     "InternalServerError", "OverloadedError"):
            cls = getattr(_anthropic_lib, attr, None)
            if cls is not None and isinstance(exc, cls):
                return True
    name = type(exc).__name__.lower()
    return any(t in name for t in ("timeout", "connection", "overloaded", "ratelimit"))


def _create_with_retry(client, **kwargs):
    """client.messages.create(**kwargs) with bounded retry on transient errors.

    Retries _MAX_RETRIES times with capped exponential backoff on 529/429/5xx/
    timeout/connection. Non-transient errors raise immediately. Final transient
    failure re-raises — callers already catch and return ""/{}.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES and _is_transient(exc):
                time.sleep(_backoff(attempt))
                continue
            raise
    if last_exc:                       # pragma: no cover
        raise last_exc


def available() -> bool:
    """True iff ANTHROPIC_API_KEY is present AND the anthropic package is installed."""
    return bool(config.ANTHROPIC_API_KEY) and _ANTHROPIC_OK


def complete_text(system: str, user: str, *, max_tokens: int = 1024) -> str:
    """Freeform text completion (email drafting).

    Returns concatenated text from all text blocks.
    Returns "" if unavailable or on any error.
    """
    if not available():
        return ""
    try:
        client = _anthropic_lib.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = _create_with_retry(
            client,
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        _accumulate(msg)
        return "".join(
            block.text for block in msg.content if block.type == "text"
        )
    except Exception:
        return ""


def complete_json(
    system: str,
    user: str,
    schema: dict,
    *,
    max_tokens: int = 512,
) -> dict:
    """Structured JSON completion (ICP scoring).

    Primary path: Anthropic structured outputs (`output_config.format` with a
    json_schema) — the model is constrained to emit a JSON object matching
    `schema`, so the first text block is guaranteed-parseable JSON. This is the
    reliable path on claude-sonnet-4-6 and avoids the failure mode where a
    prose preamble makes parsing silently return {} (which would score every
    lead 0 → route everything cold).

    Fallback path: if structured outputs are unavailable (older SDK, or the
    schema uses an unsupported constraint and the API 400s), fall back to a
    JSON-only system instruction + defensive parse.

    Returns {} on any failure or parse error.
    """
    if not available():
        return {}

    client = _anthropic_lib.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    # --- Primary: native structured outputs ---
    try:
        msg = _create_with_retry(
            client,
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        _accumulate(msg)
        raw = "".join(block.text for block in msg.content if block.type == "text")
        parsed = _parse_json(raw)
        if parsed:
            return parsed
    except Exception:
        pass  # fall through — e.g. schema constraint unsupported, or older SDK

    # --- Fallback: JSON-only instruction + defensive parse ---
    try:
        json_system = (
            f"{system}\n\n"
            "IMPORTANT: Reply with ONLY a valid JSON object — no explanation, "
            "no markdown, no code fences. The object must match this schema:\n"
            f"{json.dumps(schema, indent=2)}"
        )
        msg = _create_with_retry(
            client,
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=json_system,
            messages=[{"role": "user", "content": user}],
        )
        _accumulate(msg)
        raw = "".join(block.text for block in msg.content if block.type == "text")
        return _parse_json(raw)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Extract and parse a JSON object from text, tolerating code fences."""
    if not text:
        return {}
    text = text.strip()
    # Strip optional markdown code fences: ```json ... ``` or ``` ... ```
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    # Find the first { ... } block
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        text = brace_match.group(0)
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


if __name__ == "__main__":
    print("claude.py smoke:")
    print(f"  _ANTHROPIC_OK = {_ANTHROPIC_OK}")
    print(f"  available()   = {available()}")
    print(f"  complete_text = {repr(complete_text('sys', 'user'))}")
    print(f"  complete_json = {complete_json('sys', 'user', {})}")
    # cost_usd_total() is 0.0 keyless (no calls succeeded)
    cost = cost_usd_total()
    assert isinstance(cost, float), f"expected float, got {type(cost)}"
    assert cost == 0.0, f"expected 0.0 keyless, got {cost}"
    print(f"  cost_usd_total() keyless = {cost}  PASS")
