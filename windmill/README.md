# Windmill — feedback loop schedules

Two scheduled jobs for the feedback scoring loop. **Both are separate from the
daily 8am intake→send pipeline** and from each other — different cadences.

| Flow file | Job | Cron | Cadence | Entrypoint |
|-----------|-----|------|---------|------------|
| `feedback_outcomes.flow.json` | Drain outcomes into `leads.outcome` | `0 * * * *` | hourly | `scripts.feedback.run.main("poll")` |
| `feedback_train.flow.json` | Retrain reply-prob model → `models` | `0 9 * * 1` | weekly, Mon 09:00 UTC | `scripts.feedback.run.main("train")` |

## Why these cadences

- **Outcomes — hourly.** Replies and opens trickle in continuously. The poller
  is cheap, idempotent, and reply-precedence-safe, so frequent runs only help
  freshness. Note the `no_open` verdict only settles 48h after send (the
  poller's own rule), so hourly polling doesn't prematurely label leads.
- **Training — Monday 09:00 UTC.** Runs after the daily 8am pipeline and
  alongside the warmup-ramp Monday reset. Weekly is enough — the model only
  shifts as new labels accumulate, and `train()` no-ops below 50 labels anyway.

## Important: enable flags first

Both flows ship with `"enabled": false` and the whole loop is gated by
`ENABLE_FEEDBACK_LOOP`. Nothing runs until you:

1. Apply the schema additions in `scripts/sql/schema.sql` (the new `leads`
   columns, `models`, `inbound_email_events`).
2. Add `scikit-learn` + `numpy` (already pinned in `requirements.txt`) to the
   Windmill worker image / dependencies.
3. Set the Windmill Variable `ENABLE_FEEDBACK_LOOP=true`.
4. Set `enabled: true` on each schedule (or toggle in the UI).
5. For replies: point an MX subdomain at SendGrid Inbound Parse and send with a
   `Reply-To` on that subdomain, so replies land in `inbound_email_events`.

Don't enable `train` until the `outcomes` poller has accumulated **≥50 labelled
leads** (`SELECT count(*) FROM leads WHERE outcome IS NOT NULL`). Until then
`train()` returns `{skipped: "insufficient_labels"}` and is a harmless no-op.

## Registering

These JSON files are **OpenFlow-shaped scaffolding**, not auto-synced — this
repo has no existing Windmill git-sync layout to mirror, so confirm the exact
shape against your Windmill version on import. Two paths:

- **UI:** create a Flow, paste the `value` block, then attach a Schedule with
  the `cron`/`timezone`/`args` shown above. (Windmill keeps Schedules as
  separate resources from Flows; the `"schedule"` key here is a convenience
  copy, not part of the OpenFlow `value`.)
- **`wmill` CLI:** if you adopt git-sync, move the `value` into a
  `f/feedback/<name>.flow/flow.yaml` and declare the schedule in a
  `*.schedule.yaml`. The rawscript wrapper imports `scripts.feedback.run`, so
  ensure the `scripts` package is on the worker `PYTHONPATH` (same requirement
  as every other stage).

## Secrets / variables used

All read from the environment (Windmill Variables), no new **required** vars:
`ENABLE_FEEDBACK_LOOP` (new, opt-in), `SUPABASE_URL`, `SUPABASE_KEY`,
`SENDGRID_API_KEY` (needs the *Email Activity* scope for the poller).
