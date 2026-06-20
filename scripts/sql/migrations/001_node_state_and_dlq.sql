-- Migration 001 — node state machine, QA audit, dead-letter quarantine, flywheel weights.
-- Additive and idempotent (IF NOT EXISTS; every new column nullable/defaulted).
-- Safe to apply to the live DB with the pipeline running.
--   psql $SUPABASE_DB_URL -f scripts/sql/migrations/001_node_state_and_dlq.sql
-- (or paste into the Supabase SQL editor). The same statements are mirrored
-- into scripts/sql/schema.sql, which remains the full source of truth.

-- 1. Resumable cursor + claim columns + flywheel fields on leads
ALTER TABLE leads ADD COLUMN IF NOT EXISTS pipeline_state   TEXT DEFAULT 'sourced';
ALTER TABLE leads ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE leads ADD COLUMN IF NOT EXISTS send_claimed_at  TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_ts        TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS intent_score     REAL;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS priority         REAL;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS segment          TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_valid      BOOLEAN;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_validation TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads (pipeline_state, state_updated_at);

-- 2. node_runs — per-node execution + QA verdict (also the DeepEval evidence store)
CREATE TABLE IF NOT EXISTS node_runs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id      TEXT,
  lead_id     UUID REFERENCES leads(id),
  node        TEXT NOT NULL,
  status      TEXT NOT NULL,            -- passed | quarantined | skipped
  qa_score    REAL,
  qa_checks   JSONB,
  attempts    INTEGER DEFAULT 1,
  duration_ms INTEGER,
  cost_usd    REAL,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_node_runs_lead ON node_runs (lead_id, node);

-- 3. dead_letter — quarantine store (never drop, never silently pass)
CREATE TABLE IF NOT EXISTS dead_letter (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id        TEXT,
  lead_id       UUID REFERENCES leads(id),   -- nullable: pre-id intake failures
  node          TEXT NOT NULL,
  reason_code   TEXT NOT NULL,
  reason_detail TEXT,
  payload       JSONB,
  attempts      INTEGER DEFAULT 1,
  resolved      BOOLEAN DEFAULT FALSE,
  first_seen    TIMESTAMPTZ DEFAULT now(),
  last_seen     TIMESTAMPTZ DEFAULT now(),
  UNIQUE (lead_id, node, reason_code)
);
CREATE INDEX IF NOT EXISTS idx_dl_unresolved ON dead_letter (node) WHERE resolved = FALSE;

-- 4. scoring_weights — versioned flywheel weights (recency/relevance, per cycle)
CREATE TABLE IF NOT EXISTS scoring_weights (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  version     TEXT,
  weights     JSONB,                    -- {w_fit, w_intent, w_propensity, relevance{...}, tau_days}
  metrics     JSONB,
  is_active   BOOLEAN DEFAULT FALSE,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scoring_weights_active ON scoring_weights (version) WHERE is_active = TRUE;
