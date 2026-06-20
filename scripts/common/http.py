"""Shared synchronous HTTP helpers.

All callers use these instead of raw httpx so timeout + retry policy is
centrally enforced. No API keys are read here — callers pass headers.

Retry policy
------------
- Retries on httpx.TimeoutException and HTTP 5xx (up to `retries` extra attempts).
- Backoff: 0.5 * 2**attempt, capped at 4 s.
- HTTP 429: retry honoring Retry-After header (capped at 10 s).
- HTTP 4xx (except 429): return None immediately, no retry.
- Final failure (all retries exhausted): return None, never raise.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

TIMEOUT_DEFAULT: float = 20.0

_MAX_BACKOFF: float = 4.0
_MAX_RETRY_AFTER: float = 10.0


def _backoff(attempt: int) -> float:
    return min(0.5 * (2 ** attempt), _MAX_BACKOFF)


def get_json(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: float = TIMEOUT_DEFAULT,
    retries: int = 2,
) -> dict | list | None:
    """Sync GET -> parsed JSON or None on any failure."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(url, headers=headers, params=params)
            if r.status_code == 429:
                wait = min(
                    float(r.headers.get("Retry-After", 2)),
                    _MAX_RETRY_AFTER,
                )
                if attempt < retries:
                    time.sleep(wait)
                    continue
                return None
            if 400 <= r.status_code < 500:
                # 4xx (non-429) — don't retry
                return None
            if r.status_code >= 500:
                if attempt < retries:
                    time.sleep(_backoff(attempt))
                    continue
                return None
            return r.json()
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(_backoff(attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(_backoff(attempt))
    _ = last_exc  # surfaced for debugging via logs; not re-raised
    return None


def post_json(
    url: str,
    *,
    headers: dict | None = None,
    json: dict | None = None,
    params: dict | None = None,
    timeout: float = TIMEOUT_DEFAULT,
    retries: int = 2,
) -> dict | list | None:
    """Sync POST (JSON body) -> parsed JSON or None on any failure."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url, headers=headers, json=json, params=params)
            if r.status_code == 429:
                wait = min(
                    float(r.headers.get("Retry-After", 2)),
                    _MAX_RETRY_AFTER,
                )
                if attempt < retries:
                    time.sleep(wait)
                    continue
                return None
            if 400 <= r.status_code < 500:
                return None
            if r.status_code >= 500:
                if attempt < retries:
                    time.sleep(_backoff(attempt))
                    continue
                return None
            return r.json()
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(_backoff(attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(_backoff(attempt))
    _ = last_exc
    return None


if __name__ == "__main__":
    # Smoke block — no network, no keys needed.
    print("http.py smoke:")
    print(f"  TIMEOUT_DEFAULT = {TIMEOUT_DEFAULT}")
    print("  get_json / post_json defined OK")
    print("  (no network calls in smoke mode)")
