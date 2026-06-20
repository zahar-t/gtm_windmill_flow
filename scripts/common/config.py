"""Centralized config. All secret/env reading goes through here.

Secrets come from os.environ ONLY. In production Windmill injects its
Variables (u/admin/<NAME>) as environment variables; locally a .env file
populates os.environ via python-dotenv. We never call wmill.get_variable()
and never hardcode a key.

Importing this module is cheap and side-effect-light: it loads .env if present
but does not validate anything at import time. Use require() at call sites that
genuinely need a key, so smoke tests can run no-op without credentials.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Repo root is two levels up from scripts/common/config.py
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_REPO_ROOT / ".env")
except Exception:
    # dotenv missing or .env unreadable — fine, rely on real env vars.
    pass


def _strip(val: str | None) -> str:
    """Trim whitespace and stray surrounding quotes from an env value."""
    if val is None:
        return ""
    return val.strip().strip("'").strip('"')


def get(name: str, default: str = "") -> str:
    """Read an env var, stripped. Empty string if unset."""
    return _strip(os.getenv(name, default))


def require(name: str) -> str:
    """Read a required env var. Raise loudly if missing — better than a silent
    timeout three layers down."""
    val = _strip(os.getenv(name))
    if not val:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            f"Add it to .env (local) or Windmill Variables (prod)."
        )
    return val


# ---------- Intake ----------
EXA_API_KEY = get("EXA_API_KEY")
APIFY_API_TOKEN = get("APIFY_API_TOKEN")
APIFY_LINKEDIN_ACTOR_ID = get(
    "APIFY_LINKEDIN_ACTOR_ID", "dev_fusion~linkedin-profile-scraper"
)
# Optional LinkedIn *search* actor for cold-lead discovery (people/company search).
APIFY_LINKEDIN_SEARCH_ACTOR_ID = get("APIFY_LINKEDIN_SEARCH_ACTOR_ID", "")

# ---------- Unipile (warm LinkedIn engagement intake) ----------
# Inbound LinkedIn intent: people who viewed our profile or reacted/commented on
# our posts. Pulled on a ~20-min poll (Unipile has no webhook for these signals),
# mapped to warm leads, and run through the same funnel as every other source.
# DSN is the per-tenant host:port from the Unipile dashboard, e.g.
# "api1.unipile.com:13111". All empty -> the engagement intake no-ops (smoke-safe).
UNIPILE_DSN = get("UNIPILE_DSN")                     # host:port, no scheme
UNIPILE_API_KEY = get("UNIPILE_API_KEY")             # X-API-KEY / access token
UNIPILE_ACCOUNT_ID = get("UNIPILE_ACCOUNT_ID")       # connected LinkedIn account id
# Profile viewers ("who viewed your page") is not a typed endpoint — it rides the
# raw passthrough (POST /api/v1/linkedin). The exact LinkedIn voyager path is
# account/version-specific: copy it from your Unipile "Get raw data" example or
# LinkedIn dev tools. Empty -> profile-viewers are skipped; post engagement still
# runs off the confirmed typed endpoints.
UNIPILE_PROFILE_VIEWS_PATH = get("UNIPILE_PROFILE_VIEWS_PATH", "")
# How many recent own posts to scan for reactions + comments per poll.
UNIPILE_POST_LOOKBACK = int(get("UNIPILE_POST_LOOKBACK", "5") or 5)

# ---------- Cold-lead discovery ----------
# Warm = inbound intent (visitor webhook, processed in REAL TIME). Cold = outbound
# discovery, run on the 8am cron trigger as three parallel sources: Exa neural
# search (intake/web_search), Apify LinkedIn search (intake/apify_cold), and a
# drain of any visitor events the webhook missed. No urgency -> daily is fine.
# MIN_DAILY_LEADS is a reserved volume target (per-source `limit` governs spend
# today; not enforced as a hard floor since the cold sources run unconditionally).
MIN_DAILY_LEADS = int(get("MIN_DAILY_LEADS", "25") or 25)

# ---------- Enrichment ----------
HUNTER_API_KEY = get("HUNTER_API_KEY")
APOLLO_API_KEY = get("APOLLO_API_KEY")
PDL_API_KEY = get("PDL_API_KEY")
PROXYCURL_API_KEY = get("PROXYCURL_API_KEY")   # primary company data
CLEARBIT_API_KEY = get("CLEARBIT_API_KEY")     # fallback company data

# ---------- AI ----------
ANTHROPIC_API_KEY = get("ANTHROPIC_API_KEY")
CLAUDE_MODEL = get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ---------- CRM ----------
SUPABASE_URL = get("SUPABASE_URL")
SUPABASE_KEY = get("SUPABASE_KEY")

# ---------- Email — Instantly (cold-outreach send + warmup + tracking) ----------
# Instantly owns the cold-outreach send path: campaign send across warmed,
# rotated inboxes + open/reply/bounce tracking. We push personalized leads into
# a campaign; Instantly sends them on its own warmup schedule and reports events
# back via webhooks + campaign analytics. Empty key -> send/poll no-op (smoke).
INSTANTLY_API_KEY = get("INSTANTLY_API_KEY")
INSTANTLY_BASE_URL = get("INSTANTLY_BASE_URL", "https://api.instantly.ai/api/v2")
INSTANTLY_CAMPAIGN_ID = get("INSTANTLY_CAMPAIGN_ID")   # target campaign for sends
# From-identity used for labels/domain fallback (Instantly sets the real sender
# per connected mailbox; this is just our default + reputation-domain hint).
SENDGRID_FROM_EMAIL = get("SENDGRID_FROM_EMAIL", "outbound@example.com")
SENDGRID_FROM_NAME = get("SENDGRID_FROM_NAME", "GTM Engine")
# Legacy SendGrid key — retained only for back-compat; the cold pipeline no
# longer sends/tracks through SendGrid.
SENDGRID_API_KEY = get("SENDGRID_API_KEY")

# ---------- Notifications ----------
SLACK_WEBHOOK_URL = get("SLACK_WEBHOOK_URL")

# ---------- Email warmup ramp (Instantly-compatible) ----------
INSTANTLY_WARMUP_START = int(get("INSTANTLY_WARMUP_START", "10") or 10)
INSTANTLY_WARMUP_STEP = int(get("INSTANTLY_WARMUP_STEP", "5") or 5)

# ---------- ICP targeting ----------
ICP_QUERY = get(
    "ICP_QUERY",
    "Series A to C B2B SaaS companies 50 to 500 employees hiring or recently funded",
)

# ---------- Open-pixel tracking (optional) ----------
SENDGRID_TRACK_URL = get("SENDGRID_TRACK_URL", "")

# ---------- Feedback loop (reply-probability model) ----------
ENABLE_FEEDBACK_LOOP = get("ENABLE_FEEDBACK_LOOP", "false").lower() == "true"

# ---------- Deliverability: spam content scoring (pre-send guard) ----------
# spam_score is 0-100, higher = spammier. >= block -> hold (don't send);
# >= review -> flag but still send. Both are tunable.
SPAM_REVIEW_THRESHOLD = int(get("SPAM_REVIEW_THRESHOLD", "40") or 40)
SPAM_BLOCK_THRESHOLD = int(get("SPAM_BLOCK_THRESHOLD", "70") or 70)

# ---------- Email validation (pre-send deliverability check, node 9) ----------
# Verify addresses before drafting/sending to protect sender reputation. No
# provider key -> validation no-ops. Provider: "zerobounce" (default) | "neverbounce".
EMAIL_VERIFY_PROVIDER = get("EMAIL_VERIFY_PROVIDER", "zerobounce")
ZEROBOUNCE_API_KEY = get("ZEROBOUNCE_API_KEY")
NEVERBOUNCE_API_KEY = get("NEVERBOUNCE_API_KEY")
# Catch-all / unknown domains can't be confirmed; treat as sendable by default
# (set false to suppress them and protect reputation harder).
EMAIL_VERIFY_ALLOW_CATCHALL = get("EMAIL_VERIFY_ALLOW_CATCHALL", "true").lower() == "true"

# ---------- Lifecycle: multi-touch follow-up + reply handling ----------
# A contacted lead with no reply/bounce gets re-touched after this many days,
# up to MAX_TOUCHES total emails. Suppression below is permanent regardless.
FOLLOWUP_GAP_DAYS = int(get("FOLLOWUP_GAP_DAYS", "4") or 4)
MAX_TOUCHES = int(get("MAX_TOUCHES", "3") or 3)
# Outcomes that permanently suppress a lead from any future send.
SUPPRESS_OUTCOMES = {
    o.strip().lower()
    for o in get("SUPPRESS_OUTCOMES", "reply,bounce,unsubscribe").split(",")
    if o.strip()
}

# ---------- Deliverability: postmaster / reputation layer ----------
# The sending domain to monitor (e.g. "mail.example.com"). Empty -> Google
# Postmaster source is skipped; Instantly analytics still work off INSTANTLY_API_KEY.
POSTMASTER_DOMAIN = get("POSTMASTER_DOMAIN")
# Optional Google Postmaster Tools OAuth bearer token (service account / OAuth).
GOOGLE_POSTMASTER_TOKEN = get("GOOGLE_POSTMASTER_TOKEN")
# Trailing window for postmaster stats aggregation.
POSTMASTER_LOOKBACK_DAYS = int(get("POSTMASTER_LOOKBACK_DAYS", "7") or 7)
# Reputation thresholds (rates as fractions of sends). Defaults mirror the
# industry/Gmail complaint ceiling (0.3%) and a 5% hard-bounce ceiling.
POSTMASTER_SPAM_WATCH = float(get("POSTMASTER_SPAM_WATCH", "0.001") or 0.001)
POSTMASTER_SPAM_CRITICAL = float(get("POSTMASTER_SPAM_CRITICAL", "0.003") or 0.003)
POSTMASTER_BOUNCE_WATCH = float(get("POSTMASTER_BOUNCE_WATCH", "0.02") or 0.02)
POSTMASTER_BOUNCE_CRITICAL = float(get("POSTMASTER_BOUNCE_CRITICAL", "0.05") or 0.05)


# ---------- Funding feed (PRIMARY source) ----------
# Provider-agnostic. crunchbase (concrete) | dealroom | sifted (seams). No key -> no-op.
FUNDING_PROVIDER = get("FUNDING_PROVIDER", "crunchbase")
CRUNCHBASE_API_KEY = get("CRUNCHBASE_API_KEY")
DEALROOM_API_KEY = get("DEALROOM_API_KEY")
SIFTED_API_KEY = get("SIFTED_API_KEY")
FUNDING_LOOKBACK_DAYS = int(get("FUNDING_LOOKBACK_DAYS", "7") or 7)
FUNDING_COUNTRIES = [c.strip().upper() for c in get("FUNDING_COUNTRIES", "").split(",") if c.strip()]
# Static minor-FX to EUR (no FX API — keeps deps light). Unknown currency -> amount_eur None.
FX_TO_EUR = {"EUR": 1.0, "USD": 0.92, "GBP": 1.17, "CHF": 1.04, "SEK": 0.088, "NOK": 0.086, "DKK": 0.134}

# ---------- Priority scoring (signal recency x round-size dominate) ----------
PRIORITY_TAU_DAYS = float(get("PRIORITY_TAU_DAYS", "14") or 14)      # recency decay constant
PRIORITY_SIZE_CAP_EUR = float(get("PRIORITY_SIZE_CAP_EUR", "50000000") or 50_000_000)
PRIORITY_W_RECENCY = float(get("PRIORITY_W_RECENCY", "0.40") or 0.40)
PRIORITY_W_SIZE = float(get("PRIORITY_W_SIZE", "0.40") or 0.40)
PRIORITY_W_RELEVANCE = float(get("PRIORITY_W_RELEVANCE", "0.20") or 0.20)
# Optional relevance override per signal_type, csv 'k:v' (else built-in defaults).
RELEVANCE_WEIGHTS = get("RELEVANCE_WEIGHTS", "")   # e.g. "funding:1.0,job_change:0.7,hiring:0.45"

# ---------- Investor graph + channel router ----------
# Funds we can get warm intros from (csv). Drives channel='investor_intro'.
RELATIONSHIP_INVESTORS = [s.strip() for s in get("RELATIONSHIP_INVESTORS", "").split(",") if s.strip()]
# LinkedIn-channel touch falls back to email after this many days with no reply.
LINKEDIN_FALLBACK_DAYS = int(get("LINKEDIN_FALLBACK_DAYS", "5") or 5)


def health_summary() -> dict:
    """Boolean presence map of every secret — no values. For diagnostics."""
    keys = [
        "EXA_API_KEY", "APIFY_API_TOKEN", "HUNTER_API_KEY", "APOLLO_API_KEY",
        "PDL_API_KEY", "PROXYCURL_API_KEY", "CLEARBIT_API_KEY", "ANTHROPIC_API_KEY",
        "SUPABASE_URL", "SUPABASE_KEY", "INSTANTLY_API_KEY", "INSTANTLY_CAMPAIGN_ID",
        "SLACK_WEBHOOK_URL", "UNIPILE_DSN", "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID",
        "CRUNCHBASE_API_KEY",
    ]
    return {k: bool(globals().get(k)) for k in keys} | {"model": CLAUDE_MODEL, "funding_provider": FUNDING_PROVIDER}
