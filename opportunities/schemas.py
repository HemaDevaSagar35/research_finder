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


class PaperAssessment(Strict):
    paper_id: str
    role: Literal["supporting", "context_only"]
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    page_ids: list[str] = Field(min_length=1)


class Decision(Strict):
    candidate_id: str
    decision: Literal["accept", "reject", "insufficient", "revise"]
    notes: str = Field(min_length=1)
    page_ids: list[str]
    factual_status: Literal["supported", "needs_correction", "uncertain"]
    required_corrections: list[str]
    paper_assessments: list[PaperAssessment]


class SourceReference(Strict):
    source_id: str
    kind: Literal["finding", "observation", "tension"]
    refines: list[str]


class Opportunity(Strict):
    opportunity_id: str
    candidate: Candidate
    sources: list[SourceReference]
    paper_ids: list[str]  # All cited evidence papers, including context.
    supporting_paper_ids: list[str]
    context_paper_ids: list[str]
    paper_assessments: list[PaperAssessment]
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
        "page_budget", "rejected", "insufficient", "requires_correction", "provider_error", "call_failed",
    ]
    detail: str
    candidate: Candidate | None = None
    review: dict | None = None  # Original review response when promotion was vetoed.


class MiningResult(Strict):
    schema_version: Literal["opportunities_v2"] = "opportunities_v2"
    topic: str
    landscape_ref: dict[str, str]
    reasoning_ref: dict[str, str]
    opportunities: list[Opportunity]
    diagnostics: list[Diagnostic]
    coverage: dict[str, int]
    calls: dict[str, int]
    usage: dict[str, dict[str, int]]
    run: dict
