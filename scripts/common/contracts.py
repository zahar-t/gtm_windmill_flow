"""Typed node contracts — the canonical Lead record + QA envelope.

Design rule: LENIENT on ingest, STRICT at gates. The Lead model coerces messy
vendor data (extra='allow', permissive str fields) so nothing is dropped during
the migration; each node's gate() is where strict validation happens and a
failing record is quarantined to dead_letter.

Nothing imports this yet — adding it is behavior-neutral.
"""
from enum import Enum
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PipelineState(str, Enum):
    """Resumable per-lead cursor. A node acts ONLY on its input state and
    advances the lead out of it — that is what makes the cron idempotent."""
    sourced         = "sourced"
    enriched        = "enriched"
    scored          = "scored"
    validated       = "validated"        # email verified (ZeroBounce) — runs before personalize
    routed          = "routed"
    drafted         = "drafted"
    copy_qa_passed  = "copy_qa_passed"
    sending         = "sending"          # claim-pattern in-flight (send idempotency)
    sent            = "sent"
    replied         = "replied"
    bounced         = "bounced"
    # terminal side-states
    suppressed      = "suppressed"
    cold_stored     = "cold_stored"
    quarantined     = "quarantined"


class Check(BaseModel):
    name: str
    passed: bool
    detail: str | None = None


class QAResult(BaseModel):
    """Output of a node's self-validating gate."""
    passed: bool
    score: float | None = None            # graded gates (spam, G-Eval)
    checks: list[Check] = Field(default_factory=list)
    reason_code: str | None = None        # set when !passed -> dead_letter.reason_code

    @classmethod
    def ok(cls, checks: list[Check] | None = None, score: float | None = None) -> "QAResult":
        return cls(passed=True, score=score, checks=checks or [])

    @classmethod
    def fail(cls, reason_code: str, detail: str | None = None,
             checks: list[Check] | None = None, score: float | None = None) -> "QAResult":
        return cls(passed=False, reason_code=reason_code, score=score,
                   checks=(checks or []) + [Check(name=reason_code, passed=False, detail=detail)])


class NodeResult(BaseModel):
    lead_id: UUID | None = None
    node: str
    status: str                            # passed | quarantined | skipped
    qa: QAResult


class Lead(BaseModel):
    """Canonical lead record. Permissive types by design; gates enforce strictness."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # identity
    id: UUID | None = None
    email: str | None = None
    domain: str | None = None
    company: str | None = None
    name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    source: str | None = None
    # enrichment
    company_size: int | None = None
    industry: str | None = None
    country: str | None = None
    signal: str | None = None
    signal_type: str = "none"
    signal_ts: datetime | None = None
    # email validation (node 9)
    email_valid: bool | None = None
    email_validation: str | None = None              # ZeroBounce verdict
    # scoring + flywheel
    icp_score: int | None = Field(None, ge=0, le=100)
    icp_tier: str | None = None
    intent_score: float | None = Field(None, ge=0, le=1)
    reply_prob: float | None = Field(None, ge=0, le=1)   # shelved — not wired into priority (ML model deferred until volume)
    priority: float | None = Field(None, ge=0, le=1)
    # routing / copy
    stage: str = "new"
    segment: str | None = None
    sequence: str | None = None
    sequence_step: int = 0
    email_subject: str | None = None
    email_body: str | None = None
    spam_score: int | None = None
    # control (first-class + persisted — replaces the dropped _underscore keys)
    pipeline_state: PipelineState = PipelineState.sourced
    suppressed: bool = False
    instantly_lead_id: str | None = None
    last_contacted_at: datetime | None = None
    send_claimed_at: datetime | None = None
    # funding signal (Area 1)
    funding_amount_eur: float | None = None
    funding_round: str | None = None
    funding_announced_at: datetime | None = None
    investors: list[str] = Field(default_factory=list)
    lead_investor: str | None = None
    # channel routing (Area 3)
    channel: str | None = None                 # investor_intro | linkedin | email
    # outcomes by deal value (Area 4)
    deal_value_eur: float | None = None
    funding_bracket: str | None = None         # unknown | <1M | 1-5M | 5-20M | 20M+
    # re-trigger nurture (Area 4)
    re_trigger_at: datetime | None = None
    re_trigger_reason: str | None = None
