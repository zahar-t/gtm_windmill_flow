"""feedback/run.py — Single Windmill entrypoint for the feedback loop.

The feedback loop runs on TWO cadences, both separate from the daily 8am
intake→send pipeline:

  action="poll"  — drain outcomes (SendGrid activity + inbound replies) into
                   leads.outcome. Cheap; schedule frequently (hourly).
  action="train" — retrain the reply-probability model from labelled outcomes
                   and persist to the models table. Schedule weekly (Monday),
                   after enough outcomes have accumulated (>=50 labels).

Both delegate to the existing stage modules — this file only sequences them so
Windmill can schedule a single script with an `action` argument instead of
wiring two separate script steps.

Keyless / smoke-safe: gated by ENABLE_FEEDBACK_LOOP and degrades to a no-op
summary when the flag is off or Supabase is absent. Never raises out of main();
the delegated mains already swallow their own per-source errors.

def main(action: str = "poll") -> dict
"""
from __future__ import annotations

from scripts.common import config, log as common_log
from scripts.feedback import outcomes
from scripts.score import feedback


def main(action: str = "poll") -> dict:
    """Dispatch a feedback-loop job. Returns a summary dict; never raises.

    Parameters
    ----------
    action:
        "poll"  — run the outcome poller (frequent schedule).
        "train" — retrain + persist the reply-probability model (weekly).
        "status"/"report" — passthrough to score.feedback diagnostics.
    """
    if not config.ENABLE_FEEDBACK_LOOP:
        summary = {"action": action, "skipped": "disabled"}
        try:
            common_log.log_stage("feedback/run", summary)
        except Exception:
            pass
        return summary

    try:
        if action == "poll":
            result = outcomes.main()
        elif action == "train":
            result = feedback.main("train")
        elif action in ("status", "report"):
            result = feedback.main(action)
        else:
            result = {"error": f"unknown action: {action!r}"}
    except Exception as exc:  # belt-and-suspenders — delegated mains shouldn't raise
        result = {"error": f"feedback/run: {exc}"}

    summary = {"action": action, "result": result}
    try:
        common_log.log_stage("feedback/run", summary)
    except Exception:
        pass
    return summary


if __name__ == "__main__":
    import json
    import sys

    act = sys.argv[1].lstrip("-") if len(sys.argv) > 1 else "poll"
    print(f"=== feedback/run.py smoke (action={act!r}, keyless) ===")
    out = main(act)
    print(json.dumps(out, default=str, indent=2)[:2000])
    # Keyless default: ENABLE_FEEDBACK_LOOP is false -> skipped/disabled, no raise.
    assert isinstance(out, dict), "main() must return a dict"
    print("PASS: main() returned a dict without raising")
