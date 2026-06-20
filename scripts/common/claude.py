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

from scripts.common import config

# --- optional import guard ---
try:
    import anthropic as _anthropic_lib
    _ANTHROPIC_OK = True
except Exception:
    _anthropic_lib = None  # type: ignore[assignment]
    _ANTHROPIC_OK = False


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
        msg = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
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
        msg = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
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
        msg = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=json_system,
            messages=[{"role": "user", "content": user}],
        )
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
