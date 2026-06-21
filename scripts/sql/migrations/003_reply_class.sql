-- Migration 003 — reply_class column on leads.
-- Additive + idempotent (IF NOT EXISTS; column is nullable TEXT with no default).
-- Safe to apply live.
-- psql $SUPABASE_DB_URL -f scripts/sql/migrations/003_reply_class.sql
-- Mirrored into scripts/sql/schema.sql §8 (source of truth).

-- leads: reply_class — deterministic keyword bucket for inbound replies
-- Values: interested | question | not_now | ooo | unsubscribe | negative | other
-- Written by feedback/outcomes.py (_drain_inbound) and realtime_reply.py on
-- every reply event, using scripts/common/reply_classify.classify().
-- unsubscribe class pairs with outcome='unsubscribe' (permanently suppressed by dedup).
-- not_now / ooo class pairs with re_trigger_at / re_trigger_reason (nurture loop).
-- interested class fires a dedicated Slack highlight (slack.post_interested_reply).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS reply_class TEXT;

-- Optional index: filter/report by reply class (e.g. all 'interested' replies)
CREATE INDEX IF NOT EXISTS idx_leads_reply_class
  ON leads (reply_class)
  WHERE reply_class IS NOT NULL;
