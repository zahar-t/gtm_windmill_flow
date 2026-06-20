-- GTM Engine — Supabase (Postgres) schema
-- Apply in the Supabase SQL editor (or via `psql $SUPABASE_DB_URL -f schema.sql`).
-- Source of truth for all tables the pipeline reads/writes.
--
-- Sections:
--   1. Core CRM tables       (leads, activity, pipeline_runs)   — from GTM-PIPELINE.md
--   2. Operational tables    (linkedin_cache, email_warmup,
--                             website_visitor_events)           — added per build decisions

-- ============================================================
-- 1. CORE CRM
-- ============================================================

CREATE TABLE IF NOT EXISTS leads (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email              TEXT UNIQUE,
  name               TEXT,
  company            TEXT,
  company_url        TEXT,
  title              TEXT,
  linkedin_url       TEXT,
  source             TEXT,              -- exa_web_search | linkedin_visitor | website_visitor
  icp_score          INTEGER,           -- 0-100
  signal             TEXT,              -- most recent enrichment signal (NULL if none found)
  stage              TEXT DEFAULT 'new',-- new | hot | warm | cold | contacted | replied | converted
  last_contacted_at  TIMESTAMPTZ,
  enriched_at        TIMESTAMPTZ,
  created_at         TIMESTAMPTZ DEFAULT now(),
  updated_at         TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS activity (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id     UUID REFERENCES leads(id),
  type        TEXT,                      -- email_sent | signal_found | stage_change | reply
  payload     JSONB,
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date        DATE,
  leads_found     INTEGER,
  leads_enriched  INTEGER,
  emails_sent     INTEGER,
  leads_queued    INTEGER,
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- 2. OPERATIONAL
-- ============================================================

-- Apify LinkedIn scrape cache. 168h (7d) TTL is enforced in application code
-- (intake/linkedin.py) by comparing now() - cached_at; rows are not auto-expired.
CREATE TABLE IF NOT EXISTS linkedin_cache (
  company_url  TEXT PRIMARY KEY,        -- cache key = scraped profile URL (lead.linkedin_url preferred); legacy column name
  payload      JSONB,                   -- normalized Apify/Exa profile payload
  cached_at    TIMESTAMPTZ DEFAULT now()
);

-- Instantly.ai warmup ramp tracking. One row per send-day.
-- daily_limit starts at 10 and increments +5 each Monday (logic in email/warmup_check.py).
CREATE TABLE IF NOT EXISTS email_warmup (
  date         DATE PRIMARY KEY,
  sends_count  INTEGER DEFAULT 0,
  daily_limit  INTEGER DEFAULT 10
);

-- RB2B deanonymized website visitors. Windmill handles webhook intake and
-- writes raw events here; intake/website_visitors.py drains unprocessed rows.
CREATE TABLE IF NOT EXISTS website_visitor_events (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email        TEXT,
  name         TEXT,
  company      TEXT,
  company_url  TEXT,
  linkedin_url TEXT,
  payload      JSONB,                   -- full raw RB2B event
  processed    BOOLEAN DEFAULT FALSE,
  received_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wve_unprocessed
  ON website_visitor_events (received_at)
  WHERE processed = FALSE;

-- LinkedIn company-page visitors. A LinkedIn page-visitor source (Apify
-- LinkedIn page-visitor actor, or a "who viewed your page" export job) writes
-- raw events here; intake/linkedin_visitors.py drains unprocessed rows.
-- This is the LinkedIn-page-visitor intake feed (source = 'linkedin_visitor').
CREATE TABLE IF NOT EXISTS linkedin_visitor_events (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email                TEXT,
  name                 TEXT,
  company              TEXT,
  company_url          TEXT,
  linkedin_url         TEXT,        -- the visitor's LinkedIn profile
  visitor_linkedin_url TEXT,        -- alias some sources use; either is accepted
  title                TEXT,
  country              TEXT,        -- ISO-2 if the source provides it (feeds ICP geography)
  payload              JSONB,       -- full raw visitor event
  processed            BOOLEAN DEFAULT FALSE,
  received_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lve_unprocessed
  ON linkedin_visitor_events (received_at)
  WHERE processed = FALSE;

-- ============================================================
-- 3. FEEDBACK LOOP (reply-probability model)  — milestones 1-3
-- ============================================================

-- leads: outcome label + send/feature columns used by the model
ALTER TABLE leads ADD COLUMN IF NOT EXISTS outcome        TEXT;          -- reply|bounce|no_open|open_no_reply
ALTER TABLE leads ADD COLUMN IF NOT EXISTS outcome_at     TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_type    TEXT;          -- funding|hiring|launch|other|none
ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_size   INTEGER;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS sequence       TEXT;          -- A|B
ALTER TABLE leads ADD COLUMN IF NOT EXISTS reply_prob     REAL;          -- 0.0-1.0 (produced, not yet consumed)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS instantly_lead_id TEXT;       -- Instantly campaign-lead id (outcome join key)
ALTER TABLE leads ADD COLUMN IF NOT EXISTS sg_message_id  TEXT;          -- legacy SendGrid X-Message-Id (pre-Instantly)

-- models: trained model registry. NOTE: artifact is TEXT (base64 of pickle), NOT BYTEA —
-- the supabase REST client (common/supabase.py) is JSON-only and cannot write raw bytes.
CREATE TABLE IF NOT EXISTS models (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT,
  version     TEXT,
  artifact    TEXT,                       -- base64(pickle(pipeline))  [deviates from BYTEA — see plan_feedback.md R3]
  metrics     JSONB,
  is_active   BOOLEAN DEFAULT FALSE,
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- inbound_email_events: reply drain — Instantly reply webhook writes (via Windmill); outcomes.py drains
CREATE TABLE IF NOT EXISTS inbound_email_events (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_email   TEXT,
  to_email     TEXT,
  subject      TEXT,
  in_reply_to  TEXT,
  payload      JSONB,
  matched_lead UUID REFERENCES leads(id),
  processed    BOOLEAN DEFAULT FALSE,
  received_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leads_outcome    ON leads (outcome)            WHERE outcome IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_instantly  ON leads (instantly_lead_id)  WHERE instantly_lead_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_sg_msgid   ON leads (sg_message_id)      WHERE sg_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_iee_unprocessed  ON inbound_email_events (received_at) WHERE processed = FALSE;
CREATE INDEX IF NOT EXISTS idx_models_active    ON models (name)              WHERE is_active = TRUE;

-- ============================================================
-- 4. DELIVERABILITY (spam content scoring + postmaster layer)
-- ============================================================

-- leads: per-email spam content score + hold reason (email/spam_score.py).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS spam_score   INTEGER;   -- 0-100, higher = spammier
ALTER TABLE leads ADD COLUMN IF NOT EXISTS held_reason  TEXT;      -- e.g. 'spam_score=72' when a send was withheld

-- domain_reputation: daily reputation snapshot written by email/postmaster.py.
-- Drives the send-volume multiplier (warmup ramp × reputation multiplier) and
-- feeds the daily summary. One row per (date, domain).
CREATE TABLE IF NOT EXISTS domain_reputation (
  date             DATE,
  domain           TEXT,
  status           TEXT,        -- healthy | watch | degraded | critical | unknown
  flagged          BOOLEAN DEFAULT FALSE,  -- true when degraded/critical; set at START of the run
  send_multiplier  REAL,        -- 1.0 | 0.5 | 0.25 | 0.0
  spam_rate        REAL,        -- SendGrid complaint rate (fraction of delivered)
  bounce_rate      REAL,        -- SendGrid (bounces+blocks)/requests
  gmail_reputation TEXT,        -- Google Postmaster domainReputation (HIGH/MEDIUM/LOW/BAD)
  metrics          JSONB,       -- full raw metrics from every source
  created_at       TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (date, domain)
);

-- ============================================================
-- 5. LIFECYCLE (multi-touch follow-up + reply suppression)
-- ============================================================

-- sequence_step: which touch a lead is on (1 = first email). crm/lifecycle.py
-- re-injects contacted leads with sequence_step < MAX_TOUCHES for the next
-- touch; email/send.py persists the incremented step. 'unsubscribe' joins
-- reply/bounce as a terminal outcome dedup permanently suppresses.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS sequence_step INTEGER DEFAULT 0;

-- Drives the follow-up-due query (stage=contacted, recent enough, under cap).
CREATE INDEX IF NOT EXISTS idx_leads_followup
  ON leads (last_contacted_at)
  WHERE stage = 'contacted' AND outcome IS NULL;

-- ============================================================
-- 6. NODE STATE MACHINE / QA / DLQ / FLYWHEEL  (migration 001)
-- ============================================================
-- Mirrors scripts/sql/migrations/001_node_state_and_dlq.sql. Each node acts
-- only on leads in its input pipeline_state and advances them out of it, so the
-- cron is idempotent + resumable. Failing records are quarantined to
-- dead_letter (never dropped, never silently passed downstream).

-- Resumable cursor + claim columns + flywheel fields on leads
ALTER TABLE leads ADD COLUMN IF NOT EXISTS pipeline_state   TEXT DEFAULT 'sourced';
  -- sourced→enriched→scored→validated→routed→drafted→copy_qa_passed→sending→sent
  --   →(replied|bounced); side states: suppressed | cold_stored | quarantined
ALTER TABLE leads ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE leads ADD COLUMN IF NOT EXISTS send_claimed_at  TIMESTAMPTZ;  -- send claim-pattern
ALTER TABLE leads ADD COLUMN IF NOT EXISTS signal_ts        TIMESTAMPTZ;  -- recency weighting
ALTER TABLE leads ADD COLUMN IF NOT EXISTS intent_score     REAL;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS priority         REAL;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS segment          TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_valid      BOOLEAN;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS email_validation TEXT;          -- ZeroBounce verdict
CREATE INDEX IF NOT EXISTS idx_leads_state ON leads (pipeline_state, state_updated_at);

-- node_runs: per-node execution + QA verdict (also the DeepEval evidence store)
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

-- dead_letter: quarantine store
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

-- scoring_weights: versioned flywheel weights (recency/relevance, per cycle)
CREATE TABLE IF NOT EXISTS scoring_weights (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  version     TEXT,
  weights     JSONB,                    -- {w_fit, w_intent, w_propensity, relevance{...}, tau_days}
  metrics     JSONB,
  is_active   BOOLEAN DEFAULT FALSE,
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_scoring_weights_active ON scoring_weights (version) WHERE is_active = TRUE;

-- ============================================================
-- 7. FUNDING SIGNAL / INVESTOR GRAPH / CHANNEL / OUTCOMES (migration 002)
-- ============================================================

-- 7.1 leads — funding signal + channel + deal-value + re-trigger columns
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

-- 7.2 investors — one row per fund/investor
CREATE TABLE IF NOT EXISTS investors (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name            TEXT UNIQUE,                 -- canonical investor name (upsert key)
  is_relationship BOOLEAN DEFAULT FALSE,       -- in config.RELATIONSHIP_INVESTORS at write time
  country         TEXT,
  created_at      TIMESTAMPTZ DEFAULT now()
);

-- 7.3 company_investors — edges: which investor backed which company (from funding records)
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

-- 7.4 pipeline_runs — value-segmented reporting columns
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS pipeline_value_eur DOUBLE PRECISION;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS metrics            JSONB;  -- by_funding_bracket / by_segment
