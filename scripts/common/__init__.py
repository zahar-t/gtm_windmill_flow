"""Shared foundation for the GTM Engine pipeline.

Every stage script imports from here so the whole pipeline shares one
data contract, one config surface, and one set of vendor clients.

Submodules:
  config    — all secrets, read from os.environ only
  supabase  — thin PostgREST client (select/insert/upsert/update)
  llm       — Claude Messages API wrapper (scoring + drafting)
  slack     — Slack webhook poster
  runlog    — structured JSON run log -> logs/daily_{date}.json
  models    — Lead contract, source/stage enums, routing thresholds
"""
