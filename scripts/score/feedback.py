"""score/feedback.py — Reply-probability model (feedback loop, Milestone 3).

Trains a calibrated sklearn pipeline on labelled leads and produces reply_prob
(0.0-1.0) predictions for unlabelled leads. Model is persisted to the `models`
Supabase table as base64-encoded pickle in a TEXT column (see plan_feedback.md R3).

Keyless / smoke-safe: all public functions degrade silently when
ENABLE_FEEDBACK_LOOP is False, Supabase creds are absent, sklearn is missing,
or the model is untrained. Never raises out of main().

CLI:
    python -m scripts.score.feedback --train
    python -m scripts.score.feedback --status
    python -m scripts.score.feedback --report

Windmill: call main(action="train"|"status"|"report"|"predict").

def main(action: str = "status") -> dict
"""
from __future__ import annotations

import pickle
import base64
from datetime import datetime, timezone
from pathlib import Path

from scripts.common import config
from scripts.common import supabase
from scripts.common import log

# ---------------------------------------------------------------------------
# sklearn availability guard — mirrors claude.py's _ANTHROPIC_OK pattern
# ---------------------------------------------------------------------------
_SKLEARN_OK = False
try:
    from sklearn.pipeline import Pipeline
    from sklearn.compose import ColumnTransformer
    from sklearn.preprocessing import OneHotEncoder
    from sklearn.linear_model import LogisticRegression
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import roc_auc_score
    import numpy as np
    _SKLEARN_OK = True
except Exception:
    _SKLEARN_OK = False

# Column order MUST be consistent between train() and predict().
_FEATURE_ORDER = ["signal_type", "sequence", "company_size_bucket", "icp_score"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Feature engineering helpers
# ---------------------------------------------------------------------------

def _company_size_bucket(size) -> str:
    """Bucketize company_size -> {'1-50','51-200','201-500','500+','unknown'}.
    Tolerates None / '' / non-numeric -> 'unknown'.
    """
    try:
        n = int(size)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 50:
        return "1-50"
    if n <= 200:
        return "51-200"
    if n <= 500:
        return "201-500"
    return "500+"


def _featurize(leads: list[dict]) -> list[list]:
    """Map raw lead dicts -> model input rows (list of lists, _FEATURE_ORDER columns).

    Columns:
      signal_type          str  (None/''/none -> 'none')
      sequence             str  (None/''/none -> 'none')
      company_size_bucket  str  (bucketed via _company_size_bucket)
      icp_score            float (None -> 0.0)

    Returns a list-of-lists with columns in _FEATURE_ORDER. No pandas dependency.
    """
    rows = []
    for lead in leads:
        signal = lead.get("signal_type") or "none"
        seq = lead.get("sequence") or "none"
        bucket = _company_size_bucket(lead.get("company_size"))
        score = float(lead.get("icp_score") or 0.0)
        rows.append([signal, seq, bucket, score])
    return rows


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------

def _build_pipeline() -> "Pipeline":
    """Construct (untrained) the ColumnTransformer + CalibratedClassifierCV pipeline.

    Column order in X: [signal_type, sequence, company_size_bucket, icp_score]
    (indices 0,1,2 are categorical; index 3 is numeric — remainder='passthrough').
    """
    cat = ["signal_type", "sequence", "company_size_bucket"]
    # ColumnTransformer needs column indices (we use lists with named features via dict input,
    # but since we pass list-of-lists we use positional indices 0,1,2)
    pre = ColumnTransformer(
        transformers=[("cat", OneHotEncoder(handle_unknown="ignore"), [0, 1, 2])],
        remainder="passthrough",  # leaves icp_score (index 3) as-is
    )
    base = LogisticRegression(max_iter=1000)
    clf = CalibratedClassifierCV(base, method="isotonic", cv=3)
    return Pipeline([("pre", pre), ("clf", clf)])


# ---------------------------------------------------------------------------
# Serialization round-trip (base64 ASCII text for PostgREST JSON transport)
# ---------------------------------------------------------------------------

def _serialize_model(pipeline) -> str:
    """pickle(pipeline) -> base64 -> ASCII string for storage in TEXT column."""
    return base64.b64encode(pickle.dumps(pipeline)).decode("ascii")


def _deserialize_model(b64: str):
    """base64 ASCII string -> pickle -> pipeline object."""
    return pickle.loads(base64.b64decode(b64.encode("ascii")))


# ---------------------------------------------------------------------------
# Supabase persistence
# ---------------------------------------------------------------------------

def _fetch_labelled(limit: int = 5000) -> list[dict]:
    """SELECT leads WHERE outcome IS NOT NULL. Returns [] if no Supabase. Never raises."""
    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return []
    try:
        return supabase.select(
            "leads",
            {"outcome": "not.is.null"},
            columns="id,email,outcome,signal_type,company_size,sequence,icp_score,stage",
            order="created_at.asc",
            limit=limit,
        )
    except Exception:
        return []


def _persist_model(name: str, version: str, pipeline, metrics: dict) -> bool:
    """Deactivate prior active model of same name, insert new active row.

    Best-effort: returns True on success, False on any failure.
    artifact is stored as base64 ASCII text in a TEXT column (see plan_feedback.md R3).
    """
    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return False
    try:
        # Deactivate prior active model(s) of the same name
        supabase.update(
            "models",
            {"name": f"eq.{name}", "is_active": "eq.true"},
            {"is_active": False},
        )
    except Exception:
        pass  # no prior active model — that's fine
    try:
        supabase.insert(
            "models",
            {
                "name": name,
                "version": version,
                "artifact": _serialize_model(pipeline),  # base64 ascii text -> TEXT column
                "metrics": metrics,
                "is_active": True,
            },
        )
        return True
    except Exception:
        return False


def _load_active_model(name: str = "reply_prob"):
    """Fetch the is_active model row for `name`, deserialize.

    Returns (pipeline, version) or (None, None) on any failure / unavailable.
    """
    if not (config.SUPABASE_URL and config.SUPABASE_KEY and _SKLEARN_OK):
        return None, None
    try:
        rows = supabase.select(
            "models",
            {"name": f"eq.{name}", "is_active": "eq.true"},
            columns="version,artifact",
            order="created_at.desc",
            limit=1,
        )
    except Exception:
        return None, None
    if not rows or not rows[0].get("artifact"):
        return None, None
    try:
        return _deserialize_model(rows[0]["artifact"]), rows[0].get("version")
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def train() -> dict:
    """Fetch labelled leads, build + fit the calibrated pipeline, persist to models table.

    Returns {"trained": bool, "n_samples": int, "auc": float|None, "version": str|None,
             "reason": str|None}. Returns {"trained": False, "reason": ...} when blocked.

    AUC is computed in-sample (optimistic; small dataset) — documented in metrics as
    trained_at context, not as a held-out estimate.
    """
    if not config.ENABLE_FEEDBACK_LOOP:
        return {"trained": False, "reason": "disabled"}
    if not _SKLEARN_OK:
        return {"trained": False, "reason": "sklearn_missing"}
    if not (config.SUPABASE_URL and config.SUPABASE_KEY):
        return {"trained": False, "reason": "no_supabase"}

    rows = _fetch_labelled()
    n = len(rows)
    if n < 50:
        return {"trained": False, "n_samples": n, "reason": "insufficient_labels"}

    X = _featurize(rows)
    y = [1 if r.get("outcome") == "reply" else 0 for r in rows]

    # Guard degenerate target — CalibratedClassifierCV needs both classes
    if len(set(y)) < 2:
        return {"trained": False, "n_samples": n, "reason": "single_class"}

    X_arr = np.array(X, dtype=object)
    pipe = _build_pipeline()
    pipe.fit(X_arr, y)

    # In-sample AUC (optimistic — adequate as status signal, not generalization estimate)
    auc = None
    try:
        probs = pipe.predict_proba(X_arr)[:, 1]
        auc = float(roc_auc_score(y, probs))
    except Exception:
        pass

    version = _now_iso()
    metrics = {"auc": auc, "n_samples": n, "trained_at": version}

    _persist_model("reply_prob", version, pipe, metrics)

    # Best-effort local cache (dev convenience only — Windmill workers are ephemeral;
    # Supabase models table is the source of truth; data/ may not persist between runs)
    try:
        data_dir = Path(__file__).resolve().parents[2] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pkl_path = data_dir / f"reply_prob_{version.replace(':', '-')}.pkl"
        with pkl_path.open("wb") as fh:
            pickle.dump(pipe, fh)
    except Exception:
        pass

    return {"trained": True, "n_samples": n, "auc": auc, "version": version}


def predict(leads: list[dict] | None = None) -> list[dict]:
    """Load the active model and set lead['reply_prob'] (float 0.0-1.0) on each lead.

    No active model / disabled / sklearn missing -> leaves leads untouched (reply_prob unset).
    Never raises. leads None -> [].

    NOTE: predict() only SETS lead['reply_prob']. It does NOT route, re-rank, or touch
    'stage'. Routing consumes reply_prob in a LATER build (explicitly out of scope here).
    """
    if leads is None:
        leads = []
    if not config.ENABLE_FEEDBACK_LOOP or not _SKLEARN_OK:
        return leads
    pipe, _ = _load_active_model()
    if pipe is None:
        return leads
    try:
        X = _featurize(leads)
        X_arr = np.array(X, dtype=object)
        probs = pipe.predict_proba(X_arr)[:, 1]
        for lead, p in zip(leads, probs):
            lead["reply_prob"] = float(p)
    except Exception:
        pass
    try:
        log.log_stage("score/feedback.predict", {"scored": len(leads)})
    except Exception:
        pass
    return leads


def status() -> dict:
    """Return model status summary.

    Returns {"enabled": bool, "n_samples": int, "model_version": str|None,
             "auc": float|None, "is_trained": bool}.
    No Supabase -> zeros/None.
    """
    n_samples = 0
    model_version = None
    auc = None
    is_trained = False

    if config.SUPABASE_URL and config.SUPABASE_KEY:
        try:
            labelled = supabase.select(
                "leads",
                {"outcome": "not.is.null"},
                columns="id",
                limit=10000,
            )
            n_samples = len(labelled)
        except Exception:
            pass

        try:
            rows = supabase.select(
                "models",
                {"name": "eq.reply_prob", "is_active": "eq.true"},
                columns="version,metrics",
                order="created_at.desc",
                limit=1,
            )
            if rows:
                model_version = rows[0].get("version")
                m = rows[0].get("metrics") or {}
                auc = m.get("auc")
                is_trained = True
        except Exception:
            pass

    return {
        "enabled": config.ENABLE_FEEDBACK_LOOP,
        "n_samples": n_samples,
        "model_version": model_version,
        "auc": auc,
        "is_trained": is_trained,
    }


def report() -> dict:
    """Reply-rate breakdown by stage, signal_type, and sequence.

    Returns {"by_stage": {...}, "by_signal_type": {...}, "by_sequence": {...},
             "n_labelled": int}. Empty maps when no data.
    """
    rows = _fetch_labelled()
    n = len(rows)
    if not rows:
        return {"by_stage": {}, "by_signal_type": {}, "by_sequence": {}, "n_labelled": 0}

    def _reply_rate(group: list[dict]) -> float:
        if not group:
            return 0.0
        return round(sum(1 for r in group if r.get("outcome") == "reply") / len(group), 4)

    def _group_by(key: str, none_label: str = "none") -> dict:
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            k = r.get(key) or none_label
            buckets.setdefault(k, []).append(r)
        return {k: _reply_rate(v) for k, v in buckets.items()}

    return {
        "by_stage": _group_by("stage", "unknown"),
        "by_signal_type": _group_by("signal_type", "none"),
        "by_sequence": _group_by("sequence", "none"),
        "n_labelled": n,
    }


def main(action: str = "status") -> dict:
    """CLI / Windmill entrypoint. action in {"train","predict","status","report"}.

    Returns a summary dict. Keyless/smoke-safe; no-ops to a documented empty dict when
    ENABLE_FEEDBACK_LOOP is False, Supabase is absent, sklearn is missing, or the model is
    untrained. Never raises.
    """
    if action == "predict":
        # predict needs leads; CLI predict is a no-op status note
        return {"note": "predict is a pipeline-stage call, not a CLI action"}
    fn = {"train": train, "status": status, "report": report}.get(action)
    if fn is None:
        return {"error": f"unknown action {action}"}
    try:
        return fn()
    except Exception as exc:  # belt-and-suspenders; functions already guard
        return {"error": str(exc), "action": action}


# ---------------------------------------------------------------------------
# Keyless smoke block
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import json

    # CLI dispatch: python scripts/score/feedback.py --train | --status | --report
    # Only dispatch when a recognized flag is explicitly passed; otherwise run smoke.
    _cli_actions = {"train", "status", "report", "predict"}
    _arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if _arg.startswith("--") and _arg.lstrip("-") in _cli_actions:
        _action = _arg.lstrip("-")
        print(json.dumps(main(_action), default=str, indent=2)[:2000])
        sys.exit(0)

    # Smoke test (no args / unrecognized args)
    print("=== score/feedback.py smoke (no keys, no network) ===")

    # 1. status() keyless — ENABLE_FEEDBACK_LOOP defaults False
    s = main("status")
    print("main('status'):", json.dumps(s, default=str))
    assert s["enabled"] is False, f"expected enabled=False, got {s}"
    print("PASS: status() keyless returns enabled=False")

    # 2. report() keyless — no Supabase -> empty maps
    r = main("report")
    print("main('report'):", json.dumps(r, default=str))
    assert r["n_labelled"] == 0, f"expected n_labelled=0, got {r}"
    print("PASS: report() keyless returns empty maps")

    # 3. predict() keyless — flag off, no model -> leads untouched
    fixture_lead = {
        "email": "test@example.com",
        "signal_type": "funding",
        "sequence": "A",
        "company_size": 120,
        "icp_score": 75,
    }
    result = predict([fixture_lead])
    assert "reply_prob" not in result[0], f"reply_prob should not be set keyless"
    print("PASS: predict() keyless leaves lead untouched")

    # 4. main('predict') CLI path -> note dict
    p = main("predict")
    assert "note" in p
    print("PASS: main('predict') returns note dict")

    # 5. main('unknown') -> error dict
    e = main("unknown_action")
    assert "error" in e
    print("PASS: main('unknown') returns error dict")

    # 6. If sklearn is installed, exercise pipeline construction and bucketizer
    if _SKLEARN_OK:
        print(f"sklearn available (_SKLEARN_OK={_SKLEARN_OK}) — exercising pipeline + bucketizer")
        pipe = _build_pipeline()
        print(f"PASS: _build_pipeline() = {type(pipe).__name__}")

        bucket_cases = [
            (50, "1-50"), (51, "51-200"), (200, "51-200"),
            (201, "201-500"), (500, "201-500"), (501, "500+"),
            (None, "unknown"), ("bad", "unknown"),
        ]
        for size, expected in bucket_cases:
            got = _company_size_bucket(size)
            status_str = "PASS" if got == expected else "FAIL"
            print(f"{status_str}: _company_size_bucket({size!r}) -> {got!r} (expected {expected!r})")

        # Featurize smoke
        frows = _featurize([fixture_lead])
        assert len(frows) == 1 and len(frows[0]) == 4
        print(f"PASS: _featurize produces {len(frows[0])}-column row: {frows[0]}")
    else:
        print(f"sklearn not installed (_SKLEARN_OK={_SKLEARN_OK}) — skipping pipeline tests")

    print("=== smoke complete ===")
