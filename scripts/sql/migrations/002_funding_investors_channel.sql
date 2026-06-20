-- Migration 002 — funding signal fields, investor graph, channel router, outcomes-by-value,
-- re-trigger nurture. Additive + idempotent (IF NOT EXISTS; every column nullable/defaulted).
-- Safe to apply live.  psql $SUPABASE_DB_URL -f scripts/sql/migrations/002_funding_investors_channel.sql
-- Mirrored into scripts/sql/schema.sql §7 (source of truth).

-- 1. leads — funding signal + channel + deal-value + re-trigger columns
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_amount_eur   DOUBLE PRECISION;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_round        TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_announced_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_investor        TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS channel              TEXT;   -- investor_intro|linkedin|email
ALTER TABLE leads ADD COLUMN IF NOT EXISTS deal_value_eur       DOUBLE PRECISION;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_bracket      TEXT;   -- unknown|<1M|1-5M|5-20M|20M+
ALTER TABLE leads ADD COLUMN IF NOT EXISTS re_trigger_at        TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS re_trigger_reason    TEXT;   -- after_round|next_quarter|later

-- index: re-trigger due sweep (lifecycle.py) + channel/bracket reporting
CREATE INDEX IF NOT EXISTS idx_leads_retrigger ON leads (re_trigger_at) WHERE re_trigger_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_channel   ON leads (channel)       WHERE channel IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_bracket   ON leads (funding_bracket);

-- 2. investors — one row per fund/investor
CREATE TABLE IF NOT EXISTS investors (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT UNIQUE,                 -- canonical investor name (upsert key)
  is_relationship BOOLEAN DEFAULT FALSE,       -- in config.RELATIONSHIP_INVESTORS at write time
  country         TEXT,
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- 3. company_investors — edges: which investor backed which company (from funding records)
CREATE TABLE IF NOT EXISTS company_investors (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company       TEXT,
  domain        TEXT,
  investor_name TEXT,
  round         TEXT,
  amount_eur    DOUBLE PRECISION,
  announced_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (domain, investor_name, round)        -- idempotent edge upsert key
);
CREATE INDEX IF NOT EXISTS idx_ci_company  ON company_investors (domain);
CREATE INDEX IF NOT EXISTS idx_ci_investor ON company_investors (investor_name);

-- 4. pipeline_runs — value-segmented reporting columns
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS pipeline_value_eur DOUBLE PRECISION;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS metrics            JSONB;  -- by_funding_bracket / by_segment
