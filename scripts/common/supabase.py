"""Thin Supabase (PostgREST) client over httpx — keeps deps light.

Only the verbs the pipeline needs: select / insert / upsert / update.
All calls are synchronous (Windmill scripts are sync def main()).

Filters use PostgREST operator syntax passed as params, e.g.
    select("leads", {"email": "eq.foo@bar.com"})
    select("leads", {"icp_score": "gte.70", "stage": "eq.new"})

Returns parsed JSON (list[dict] for select/insert/upsert, list[dict] for update).
Raises httpx.HTTPStatusError on non-2xx so callers fail loud.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import config

TIMEOUT = 20.0


def _base() -> str:
    url = config.require("SUPABASE_URL").rstrip("/")
    return f"{url}/rest/v1"


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    key = config.require("SUPABASE_KEY")
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def select(
    table: str,
    filters: dict[str, str] | None = None,
    columns: str = "*",
    order: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """SELECT rows. filters are PostgREST operator strings (e.g. 'eq.x')."""
    params: dict[str, Any] = {"select": columns}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = limit
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(f"{_base()}/{table}", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


def insert(table: str, rows: dict | list[dict]) -> list[dict[str, Any]]:
    """INSERT one or many rows, returning the inserted representation."""
    payload = rows if isinstance(rows, list) else [rows]
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(
            f"{_base()}/{table}",
            headers=_headers({"Prefer": "return=representation"}),
            json=payload,
        )
        r.raise_for_status()
        return r.json()


def upsert(
    table: str,
    rows: dict | list[dict],
    on_conflict: str = "email",
) -> list[dict[str, Any]]:
    """UPSERT (insert or merge on conflict column). Default conflict key: email."""
    payload = rows if isinstance(rows, list) else [rows]
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(
            f"{_base()}/{table}",
            headers=_headers(
                {"Prefer": "resolution=merge-duplicates,return=representation"}
            ),
            params={"on_conflict": on_conflict},
            json=payload,
        )
        r.raise_for_status()
        return r.json()


def update(
    table: str,
    filters: dict[str, str],
    values: dict[str, Any],
) -> list[dict[str, Any]]:
    """UPDATE rows matching filters (PostgREST operator strings)."""
    if not filters:
        raise ValueError("update() requires filters — refusing to update all rows")
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.patch(
            f"{_base()}/{table}",
            headers=_headers({"Prefer": "return=representation"}),
            params=filters,
            json=values,
        )
        r.raise_for_status()
        return r.json()
