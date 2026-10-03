"""Opportunity Miner contracts; model output contains references, not evidence."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reasoning.schemas import Evidence, ReviewSource

Category = Literal[
    "recurring_limitation", "restrictive_assumption", "missing_regime",
    "contradiction", "failure_mode", "missing_evaluation",
    "unresolved_tradeoff", "mechanistic_interaction",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(Strict):
    category: Category
    question: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    uncertainties: list[str]
    source_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class Proposal(Strict):
    candidates: list[Candidate]


class Decision(Strict):
    candidate_id: str
    decision: Literal["accept", "reject", "insufficient"]
    notes: str = Field(min_length=1)
    page_ids: list[str]


class SourceReference(Strict):
    source_id: str
    kind: Literal["finding", "observation", "tension"]
    refines: list[str]


class Opportunity(Strict):
    opportunity_id: str
    candidate: Candidate
    sources: list[SourceReference]
    paper_ids: list[str]
    support: Literal["single_paper", "multiple_papers"]
    evidence: list[Evidence]
    review_sources: list[ReviewSource]
    review_notes: str
    verification: Literal["supported_in_reviewed_corpus"] = "supported_in_reviewed_corpus"
    literature_novelty: Literal["not_assessed"] = "not_assessed"


class Diagnostic(Strict):
    item_id: str
    reason: Literal[
        "no_reviewed_sources", "batch_limit", "input_budget", "call_budget",
        "invalid_proposal", "invalid_review", "invalid_reference", "duplicate",
        "missing_artifact", "stale_evidence", "unresolved_location", "missing_pages",
        "page_budget", "rejected", "insufficient", "provider_error", "call_failed",
    ]
    detail: str
    candidate: Candidate | None = None


class MiningResult(Strict):
    schema_version: Literal["opportunities_v1"] = "opportunities_v1"
    topic: str
    landscape_ref: dict[str, str]
    reasoning_ref: dict[str, str]
    opportunities: list[Opportunity]
    diagnostics: list[Diagnostic]
    coverage: dict[str, int]
    calls: dict[str, int]
    usage: dict[str, dict[str, int]]
    run: dict
