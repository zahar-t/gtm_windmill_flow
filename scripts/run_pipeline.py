"""run_pipeline.py — Local end-to-end runner for the daily GTM Engine pipeline.

In production each stage is a node in the Windmill DAG (cron 0 8 * * *). This
module sequences the exact same stages in the exact same order so the whole
funnel can be run/observed from one process — for local runs, CI smoke, or a
single Windmill "script" wrapper.

This is the DAILY BATCH (cold lead generation + follow-ups). Warm/inbound
visitors are NOT handled here — they run in REAL TIME via the webhook trigger of
the unified Windmill flow (scripts/intake/webhook_visitor.py), because inbound
intent decays in minutes. The daily batch's three cold sources include a drain
of the visitor tables purely as a backfill for events the webhook missed.

  0a postmaster pull reputation FIRST -> flag failing domains in Supabase + Slack
  0b lifecycle  load_lifecycle (follow-up-due + re-triggers)  [replies: realtime_reply]
  1 intake     funding (PRIMARY) · web_search(Exa, backfill) · apify_cold(backfill)
               · missed-webhook drain (backfill)
  2 enrich     waterfall(email) · signals · company
  3 score      icp(rubric) · priority(recency x size) · dedup(recency+suppression) · route
               · email_validate · channel(investor_intro|linkedin|email)
  4 personalize hot(A) · warm(B) · followup(F)   then merge new + follow-up
  4.5 spam     spam_score  (hold risky copy)
  5 send       send (warmup x reputation, pulled at start) · log
  6 crm        upsert · investors.persist_graph · handoff
  7 report     daily_summary

Every stage is smoke-safe: with no API keys the run is a clean no-op (intake
returns 0 leads, nothing crashes). Pass demo=True to seed synthetic leads (and,
since there is no LLM key, synthetic copy) so the scoring + deliverability gates
are visibly exercised end-to-end.

    python -m scripts.run_pipeline          # real stages, keyless -> 0 leads
    python -m scripts.run_pipeline --demo   # synthetic leads through every stage
"""
from __future__ import annotations

from scripts.common import config, log
from scripts.intake import linkedin_visitors, website_visitors, web_search, apify_cold
from scripts.intake import funding as funding_intake
from scripts.enrich import waterfall, signals, company
from scripts.score import icp, route
from scripts.score import priority, channel
from scripts.crm import dedup, upsert, handoff, lifecycle
from scripts.personalize import hot, warm, followup
from scripts.email import spam_score, postmaster, send, log as email_log, validate as email_validate
from scripts.report import daily_summary


def _count(leads: list[dict], **preds) -> dict:
    return {
        "total": len(leads),
        "hot": sum(1 for l in leads if l.get("stage") == "hot"),
        "warm": sum(1 for l in leads if l.get("stage") == "warm"),
        "cold": sum(1 for l in leads if l.get("stage") == "cold"),
        "contacted": sum(1 for l in leads if l.get("stage") == "contacted"),
        "held_spam": sum(1 for l in leads if l.get("_hold") == "spam_risk"),
    }


# Synthetic leads for demo mode — shaped as if they arrived from the funding feed
# (PRIMARY source) and backfill sources, with firmographics pre-filled so the
# deterministic ICP rubric scores them without an LLM. No real companies.
#
# Mix of sources and firmographic profiles to exercise the hot/warm/cold split.
_DEMO_LEADS: list[dict] = [
    {"company": "Northwind Labs", "name": "Dana Reis", "email": "dana@northwind.example",
     "source": "funding_feed", "country": "US", "company_size": 80,
     "funding_stage": "series_a", "industry": "b2b saas", "months_since_last_funding": 3,
     "last_funding_eur": 8_000_000, "signal": "Northwind Labs raises $8M Series A",
     "signal_type": "funding",
     "funding_amount_eur": 8_000_000, "funding_round": "series_a",
     "funding_announced_at": "2026-06-16", "signal_ts": "2026-06-16",
     "investors": [], "_errors": []},
    {"company": "Tagus Analytics", "name": "Luis Marta", "email": "luis@tagus.example",
     "source": "website_visitor", "country": "DE", "company_size": 200,
     "funding_stage": "series_b", "industry": "data analytics saas", "_errors": []},
    {"company": "FarCorp Logistics", "name": "Sam Poe", "email": "sam@farcorp.example",
     "source": "exa_web_search", "country": "XX", "industry": "logistics", "_errors": []},
    {"company": "Quickwin SaaS", "name": "Max Yu", "email": "max@quickwin.example",
     "source": "funding_feed", "country": "GB", "company_size": 55, "funding_stage": "seed",
     "industry": "b2b saas", "months_since_last_funding": 2, "last_funding_eur": 3_000_000,
     "signal": "Quickwin SaaS closes seed round", "signal_type": "funding",
     "funding_amount_eur": 3_000_000, "funding_round": "seed",
     "funding_announced_at": "2026-06-18", "signal_ts": "2026-06-18",
     "investors": [], "_errors": []},
]

# Synthetic copy injected in demo mode when the (keyless) personalize stage
# leaves bodies empty — so the spam gate + send path are exercised. The last
# one is deliberately spammy to demonstrate the spam-score hold.
_DEMO_COPY = {
    "Northwind Labs": ("Quick note on your Series A",
                       "Congrats on the raise, Dana. We help fast-growing B2B teams get more "
                       "from their stack without adding headcount. Worth a 15-min call Thursday? "
                       "Reply STOP to opt out."),
    "Tagus Analytics": ("An idea for your analytics team",
                        "Hi Luis — as you scale your data platform, a few teams your size have "
                        "found it useful to see how peers handle X. Happy to share. "
                        "Reply STOP to opt out."),
    "Quickwin SaaS": ("RE: ACT NOW!! 100% FREE limited time!!!",
                      "CONGRATULATIONS YOU have been SELECTED!! Click here http://bit.ly/x "
                      "to claim your FREE bonus, guaranteed, no obligation $$$"),
}


def main(demo: bool = False, icp_query: str = "", limit: int = 10) -> dict:
    """Run the full daily pipeline once. Returns a summary dict; never raises."""
    log.log_stage("run_pipeline", {"event": "start", "demo": demo})

    # ---- 0a. POSTMASTER — pull reputation FIRST, flag failing domains in
    #          Supabase + Slack immediately, before any send rides on it. ----
    reputation = postmaster.main()   # live pull + snapshot write + alert if failing

    # ---- 0b. LIFECYCLE — load follow-up-due leads. (Replies are actioned
    #          event-driven by the hourly outcomes poller, not in this flow.) ----
    followups = [] if demo else lifecycle.main()  # contacted, no reply, due for next touch

    # ---- 1. INTAKE — cold lead generation (3 sources, same as the 8am flow) ----
    #   Warm/inbound visitors are processed in REAL TIME (the unified flow's
    #   webhook trigger -> scripts/intake/webhook_visitor.py). The daily batch is
    #   cold lead gen: Exa discovery + Apify LinkedIn search + a drain of any
    #   visitor events the webhook missed (the "missed-webhook" backfill).
    if demo:
        leads = [dict(l) for l in _DEMO_LEADS]
    else:
        leads = []
        # funding is PRIMARY — runs first so dedup suppresses duplicates found by backfill
        leads += funding_intake.main(since_days=config.FUNDING_LOOKBACK_DAYS, limit=limit)
        leads += web_search.main(icp_query, limit)    # Exa neural ICP discovery (backfill)
        leads += apify_cold.main(icp_query, limit)    # Apify LinkedIn search discovery (backfill)
        leads += website_visitors.main()              # missed-webhook backfill: website visitors
        leads += linkedin_visitors.main()             # missed-webhook backfill: LinkedIn visitors
    found = len(leads)

    # ---- 2. ENRICH new leads (mutate in place) ----
    waterfall.main(leads)        # email waterfall: Hunter -> Apollo -> PDL (emails for cold leads)
    signals.main(leads)          # Exa funding/hiring/launch signals (7d)
    company.main(leads)          # Proxycurl -> Clearbit: size · industry · country
    enriched = sum(1 for l in leads if l.get("enriched_at")) or (found if demo else 0)

    # ---- 3. SCORE — ICP rubric -> priority -> dedup(+suppress) -> route -> validate -> channel ----
    icp.main(leads)              # deterministic ICP rubric (Claude extracts inputs)
    priority.main(leads)         # intent_score + priority (recency x size dominate); AFTER icp, BEFORE dedup
    dedup.main(leads)            # recency + permanent suppression (reply/bounce/unsub) — fail-closed
    route.main(leads)            # hot / warm / cold
    email_validate.main(leads)   # node 9 — verify hot/warm deliverability before drafting (skips invalid)
    channel.main(leads)          # investor_intro | linkedin | email; AFTER validate, BEFORE personalize

    # ---- 4. PERSONALIZE — new leads (hot/warm) + follow-ups (sequence F) ----
    hot.main(leads)              # sequence A — 3-line signal-led
    warm.main(leads)             # sequence B — value-add nurture
    if demo:
        # No LLM key -> personalize left bodies empty; inject synthetic copy so the
        # spam gate + send path are exercised. (One sample is intentionally spammy.)
        for l in leads:
            if l.get("stage") in ("hot", "warm") and not l.get("email_body"):
                subj, body = _DEMO_COPY.get(l.get("company"), (None, None))
                if body:
                    l["email_subject"], l["email_body"] = subj, body
                    l.setdefault("sequence", "A" if l["stage"] == "hot" else "B")
    followup.main(followups)     # sequence F — next touch for re-injected leads

    # ---- merge new + follow-up leads into one send batch ----
    batch = leads + followups

    # ---- 4.5 SPAM SCORE — hold risky copy before send ----
    spam_score.main(batch)

    # ---- 5. SEND — uses the reputation pulled at the START of the run ----
    send.main(batch, reputation=reputation)      # warmup ramp x reputation multiplier
    email_log.main(batch)

    # ---- 6. CRM ----
    upsert.main(batch)
    # Persist investor graph from funding leads (best-effort, keyless no-op)
    from scripts.common import investors as _investors
    _investors.persist_graph(batch)
    handoff.main(batch)

    # ---- 7. REPORT ----
    counts = _count(batch)
    sent = counts["contacted"]
    queued = counts["warm"]
    report = daily_summary.main(found=found, enriched=enriched, sent=sent, queued=queued, leads=batch)

    summary = {
        "demo": demo,
        "found": found,
        "enriched": enriched,
        "followups_due": len(followups),
        "scored": counts,
        "sent": sent,
        "queued": queued,
        "held_spam": counts["held_spam"],
        "reputation": reputation.get("status"),
        "report": report,
        "leads": [
            {
                "company": l.get("company"), "source": l.get("source"),
                "icp_score": l.get("icp_score"), "icp_tier": l.get("icp_tier"),
                "stage": l.get("stage"), "spam_score": l.get("spam_score"),
                "hold": l.get("_hold"), "followup": bool(l.get("_followup")),
            }
            for l in batch
        ],
    }
    log.log_stage("run_pipeline", {"event": "done", **{k: summary[k] for k in ("found", "sent", "queued", "held_spam")}})
    return summary


if __name__ == "__main__":
    import json
    import sys

    demo = "--demo" in sys.argv
    print(f"=== GTM Engine pipeline run (demo={demo}, keyless={'yes' if not config.ANTHROPIC_API_KEY else 'no'}) ===\n")
    out = main(demo=demo)

    s = out["scored"]
    print("\n" + "=" * 60)
    print(f"  follow-ups due  : {out['followups_due']}")
    print(f"  found (new)     : {out['found']}")
    print(f"  enriched        : {out['enriched']}")
    print(f"  routed          : {s['hot']} hot / {s['warm']} warm / {s['cold']} cold")
    print(f"  held (spam)     : {out['held_spam']}")
    print(f"  sent            : {out['sent']}   queued(warm): {out['queued']}")
    print(f"  reputation      : {out['reputation']}")
    print("=" * 60)

    if out["leads"]:
        print("\nPer-lead detail:")
        print(f"  {'company':<18}{'source':<17}{'score':>6} {'tier':<4} {'stage':<10}{'spam':>5} hold")
        for l in out["leads"]:
            print(f"  {str(l['company']):<18}{str(l['source']):<17}"
                  f"{str(l['icp_score']):>6} {str(l['icp_tier'] or ''):<4} "
                  f"{str(l['stage']):<10}{str(l['spam_score']):>5} {l['hold'] or ''}")
    print("\nReport counts:", json.dumps(out["report"], default=str))
