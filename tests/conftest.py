"""conftest.py — shared fixtures for the GTM Engine test suite.

(a) keyless_env   — ensures no real creds are present (default for most tests)
(b) crm_on        — sets fake URL/KEY + monkeypatches supabase verbs to record calls
(c) respx_router  — httpx mock router (respx) reset per-test
(d) golden        — loads JSON fixtures from tests/golden/
(e) make_lead     — factory that builds a minimal lead dict
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

GOLDEN_DIR = Path(__file__).parent / "golden"


# ---------------------------------------------------------------------------
# (a) keyless_env — strip any real creds from the env so tests are hermetic
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def keyless_env(monkeypatch):
    """Remove real credentials from env for every test by default."""
    for var in (
        "SUPABASE_URL", "SUPABASE_KEY",
        "ANTHROPIC_API_KEY",
        "INSTANTLY_API_KEY", "INSTANTLY_CAMPAIGN_ID",
        "ZEROBOUNCE_API_KEY", "NEVERBOUNCE_API_KEY",
        "UNIPILE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    # Also patch the already-imported config module values
    import scripts.common.config as cfg
    monkeypatch.setattr(cfg, "SUPABASE_URL", "")
    monkeypatch.setattr(cfg, "SUPABASE_KEY", "")
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(cfg, "INSTANTLY_API_KEY", "")
    monkeypatch.setattr(cfg, "INSTANTLY_CAMPAIGN_ID", "")
    monkeypatch.setattr(cfg, "ZEROBOUNCE_API_KEY", "")
    monkeypatch.setattr(cfg, "NEVERBOUNCE_API_KEY", "")
    monkeypatch.setattr(cfg, "UNIPILE_API_KEY", "")

    yield


# ---------------------------------------------------------------------------
# (b) crm_on — fake creds + stub supabase verbs, records calls in a dict
# ---------------------------------------------------------------------------
@pytest.fixture
def crm_calls():
    """Dict that accumulates calls: {'select': [...], 'insert': [...], ...}"""
    return {"select": [], "insert": [], "upsert": [], "update": []}


@pytest.fixture
def crm_on(monkeypatch, crm_calls):
    """Enable fake CRM creds and stub all supabase verbs to record calls."""
    import scripts.common.config as cfg
    import scripts.common.node as node_mod

    monkeypatch.setattr(cfg, "SUPABASE_URL", "http://fake-supabase.example")
    monkeypatch.setattr(cfg, "SUPABASE_KEY", "fake-key")

    import scripts.common.supabase as sb

    def _fake_select(table, filters=None, columns="*", order=None, limit=None):
        crm_calls["select"].append({"table": table, "filters": filters})
        return []

    def _fake_insert(table, rows):
        crm_calls["insert"].append({"table": table, "rows": rows})
        return []

    def _fake_upsert(table, rows, on_conflict="email"):
        crm_calls["upsert"].append({"table": table, "rows": rows, "on_conflict": on_conflict})
        return []

    def _fake_update(table, filters, values):
        crm_calls["update"].append({"table": table, "filters": filters, "values": values})
        return []

    monkeypatch.setattr(sb, "select", _fake_select)
    monkeypatch.setattr(sb, "insert", _fake_insert)
    monkeypatch.setattr(sb, "upsert", _fake_upsert)
    monkeypatch.setattr(sb, "update", _fake_update)

    # Also re-patch node's internal supabase reference
    monkeypatch.setattr(node_mod, "_supabase", sb)
    monkeypatch.setattr(node_mod, "_SUPABASE_OK", True)

    yield crm_calls


# ---------------------------------------------------------------------------
# (c) respx_router — an httpx mock router, active per-test
# ---------------------------------------------------------------------------
@pytest.fixture
def respx_mock():
    """A fresh respx Router for mocking httpx calls."""
    import respx as respx_lib
    with respx_lib.mock(assert_all_called=False) as router:
        yield router


# ---------------------------------------------------------------------------
# (d) golden — load JSON fixture files
# ---------------------------------------------------------------------------
@pytest.fixture
def golden():
    def _load(name: str) -> Any:
        path = GOLDEN_DIR / name
        return json.loads(path.read_text())
    return _load


# ---------------------------------------------------------------------------
# (e) make_lead — factory for minimal lead dicts
# ---------------------------------------------------------------------------
@pytest.fixture
def make_lead():
    _counter = [0]

    def _factory(**kwargs) -> dict:
        _counter[0] += 1
        n = _counter[0]
        defaults: dict[str, Any] = {
            "company": f"TestCo {n}",
            "email": f"lead{n}@testco{n}.example",
            "domain": f"testco{n}.example",
            "stage": "new",
            "icp_score": 50,
            "signal": None,
            "signal_type": "none",
            "_errors": [],
        }
        defaults.update(kwargs)
        return defaults

    return _factory
