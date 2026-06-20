"""Structured daily log writer.

Writes to logs/daily_{YYYY-MM-DD}.json at repo root.
File format (preserved if exists):
  {
    "run_date": "YYYY-MM-DD",
    "build": "...",
    "stages": { "<stage>/<script>": [<entry>, ...] },
    "agents": []
  }

All functions swallow exceptions — logging must never break the pipeline.
Writes are atomic-ish: write to a .tmp file then os.replace() so a crash
mid-write doesn't corrupt the log.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
from datetime import datetime, timezone

# Repo root is three levels up: scripts/common/log.py -> scripts/common -> scripts -> repo
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LOGS_DIR = _REPO_ROOT / "logs"


def today_path() -> pathlib.Path:
    """Absolute path to today's daily log file."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _LOGS_DIR / f"daily_{today}.json"


def log_stage(script: str, summary: dict) -> None:
    """Append a stage entry to today's daily log.

    Parameters
    ----------
    script:
        Slash-separated stage id, e.g. "intake/web_search".
    summary:
        Small dict of counts / metadata (e.g. {"found": 12}).

    Swallows ALL exceptions — logging failures must never break the pipeline.
    """
    try:
        path = today_path()
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Read existing or seed a fresh structure
        data: dict = {
            "run_date": today_str,
            "build": "",
            "stages": {},
            "agents": [],
        }
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    # Merge: preserve existing top-level keys, only update what we need
                    data.update(loaded)
                    # Ensure stages is a dict (guard against corruption)
                    if not isinstance(data.get("stages"), dict):
                        data["stages"] = {}
            except Exception:
                pass  # Seed fresh on parse error

        # Build entry
        entry: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "script": script,
            **summary,
        }

        stages: dict = data.get("stages", {})
        if script not in stages or not isinstance(stages[script], list):
            stages[script] = []
        stages[script].append(entry)
        data["stages"] = stages

        # Atomic write: tmp file in same directory then os.replace
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(_LOGS_DIR),
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(data, tmp, indent=2, default=str)
            tmp_name = tmp.name

        os.replace(tmp_name, str(path))
    except Exception:
        pass  # Logging must never raise


if __name__ == "__main__":
    import json as _json

    print("log.py smoke:")
    print(f"  today_path() = {today_path()}")
    log_stage("smoke/test", {"records": 0, "note": "keyless smoke run"})
    try:
        with today_path().open("r", encoding="utf-8") as fh:
            content = _json.load(fh)
        print(f"  log written OK — stages: {list(content.get('stages', {}).keys())}")
    except Exception as e:
        print(f"  could not read back log: {e}")
