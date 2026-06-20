# Running GTM Engine in Docker

Two independent things live here:

1. **Local Windmill** (`docker-compose.yml`) — the orchestrator UI, so you can
   see and run the DAG (`windmill/daily_pipeline.flow.json`).
2. **The pipeline image** (`Dockerfile`) — runs the funnel end-to-end in a
   container, no Windmill needed (good for `make dry-run` / CI).

## 1. See the Windmill flow

```bash
make windmill          # docker compose up -d  (pulls Postgres + Windmill)
# open http://localhost:8000     login: admin@windmill.dev / changeme
make windmill-logs     # watch it boot
make windmill-down     # stop  (ARGS=-v to wipe the DB volume)
```

First login → create a workspace (e.g. `gtm-engine`). To **view** the DAG:
Flows → **New flow** → menu → **Import from JSON**, and paste the `value` object
from `windmill/daily_pipeline.flow.json` (that object is the OpenFlow — the
`modules` list is the DAG you'll see rendered).

This compose mounts the repo into the worker at `/gtm-engine` and injects a
`gtm-engine.pth`, so `scripts` is importable and an imported flow can **execute**
(Windmill installs each rawscript's `#requirements:` on first run). You still
import the flow once (UI above, or `wmill` CLI).

## 2. Run the pipeline in a container

```bash
make dry-run           # full funnel, keyless, synthetic leads, NO real sends
make run-once          # real run — needs a populated .env (cp .env.example .env)
```

## 3. Apply a migration (needs the direct Postgres URL)

```bash
export SUPABASE_DB_URL="postgresql://postgres:<pwd>@db.<ref>.supabase.co:5432/postgres"
make migrate FILE=scripts/sql/migrations/001_node_state_and_dlq.sql
```

The REST key (`SUPABASE_KEY`) can read/write rows but cannot run DDL; migrations
need the Postgres connection string above (or paste the SQL into the Supabase SQL
editor).
