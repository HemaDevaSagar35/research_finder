"""Contracts for the Cross-Paper Reasoner.

Three groups of models:

  Input      Landscape imported from landscape.schemas: the shared builder
             contract. LegacyLandscape retains historical reasoner fixtures.
             validate_landscape() checks both formats at the boundary.
  Internal   EvidenceBundle (code → model), ThreadReasoningDraft (model →
             code), SupportReviewRequest / SupportReviewResponse (code ↔
             model). The model only ever emits statements, kinds, stances,
             ids and decisions; code owns paths, locations, values, summaries
             and acceptance.
  Output     CrossPaperReasoning: accepted findings / observations /
             tensions, typed diagnostics for everything else, coverage,
             usage.

All models are strict (extra="forbid"). `SourceLocation` is the extraction
schema's own type so provenance keeps one shape across the pipeline.

    uv run python -m reasoning.schemas --check            # print JSON Schemas
    uv run python -m reasoning.schemas --landscape f.json # validate a file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from extraction.research_extract import SourceLocation
from landscape.schemas import Landscape

# Historical reasoner-only contract, retained for existing fixtures/files.
# New production inputs use landscape.schemas.Landscape.
LANDSCAPE_SCHEMA_VERSION = "landscape_v1"
OUTPUT_SCHEMA_VERSION = "cross_paper_v1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Legacy input: historical reasoner-only landscape_v1
# --------------------------------------------------------------------------

ItemKind = Literal["aggregated_finding", "recurring_limitation",
                   "common_assumption", "underexplored_regime"]

RelationType = Literal["SOLVES", "REDUCES", "INTRODUCES", "CAUSES", "DEPENDS_ON",
                       "REQUIRES", "FAILS_UNDER", "TRADES_OFF_WITH", "HURTS",
                       "CONTRADICTS"]


class PaperRef(Strict):
    paper_id: str
    source_locations: list[SourceLocation] = Field(default_factory=list)


class ConceptGroup(Strict):
    group_id: str
    facet: str
    concept: str
    aliases: list[str] = Field(default_factory=list)
    paper_ids: list[str]


class LandscapeItem(Strict):
    item_id: str
    kind: ItemKind
    statement: str
    group_ids: list[str]
    supporting: list[PaperRef]


class Contradiction(Strict):
    item_id: str
    side_a: LandscapeItem
    side_b: LandscapeItem


class Relationship(Strict):
    item_id: str
    source_group_id: str
    relation: RelationType
    target_group_id: str
    supporting: list[PaperRef]


class LegacyLandscape(Strict):
    schema_version: Literal["landscape_v1"]
    topic: str
    paper_ids: list[str]
    groups: list[ConceptGroup]
    items: list[LandscapeItem] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    def group(self, group_id: str) -> ConceptGroup:
        return next(g for g in self.groups if g.group_id == group_id)


class LandscapeValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("Landscape failed validation:\n  - " + "\n  - ".join(errors))
        self.errors = errors


def landscape_errors(land: LegacyLandscape) -> list[str]:
    """Referential-integrity problems pydantic cannot see. Empty = valid."""
    errs: list[str] = []
    inventory = set(land.paper_ids)
    if len(inventory) != len(land.paper_ids):
        errs.append("paper_ids inventory contains duplicates")
    if not inventory:
        errs.append("paper_ids inventory is empty")

    group_ids = [g.group_id for g in land.groups]
    if len(set(group_ids)) != len(group_ids):
        errs.append("duplicate group_id")
    known_groups = set(group_ids)
    for g in land.groups:
        for pid in g.paper_ids:
            if pid not in inventory:
                errs.append(f"group {g.group_id}: paper {pid} not in inventory")

    item_ids: list[str] = []

    def check_item(item: LandscapeItem, where: str) -> None:
        item_ids.append(item.item_id)
        if not item.group_ids:
            errs.append(f"{where} {item.item_id}: group_ids is empty")
        for gid in item.group_ids:
            if gid not in known_groups:
                errs.append(f"{where} {item.item_id}: unknown group {gid}")
        for ref in item.supporting:
            if ref.paper_id not in inventory:
                errs.append(f"{where} {item.item_id}: paper {ref.paper_id} "
                            f"not in inventory")

    for item in land.items:
        check_item(item, "item")
    for c in land.contradictions:
        item_ids.append(c.item_id)
        check_item(c.side_a, f"contradiction {c.item_id} side_a")
        check_item(c.side_b, f"contradiction {c.item_id} side_b")
    for r in land.relationships:
        item_ids.append(r.item_id)
        for gid in (r.source_group_id, r.target_group_id):
            if gid not in known_groups:
                errs.append(f"relationship {r.item_id}: unknown group {gid}")
        for ref in r.supporting:
            if ref.paper_id not in inventory:
                errs.append(f"relationship {r.item_id}: paper {ref.paper_id} "
                            f"not in inventory")
    if len(set(item_ids)) != len(item_ids):
        errs.append("duplicate item_id across items/contradictions/relationships")
    return errs


def validate_landscape(land: Landscape | LegacyLandscape) -> Landscape | LegacyLandscape:
    if isinstance(land, Landscape):
        # Revalidate nested references even if a caller mutated an existing model.
        return Landscape.model_validate(land.model_dump())
    errs = landscape_errors(land)
    if errs:
        raise LandscapeValidationError(errs)
    return land


def load_landscape(path: Path) -> Landscape | LegacyLandscape:
    data = json.loads(path.read_text())
    if data.get("schema_version") == LANDSCAPE_SCHEMA_VERSION:
        return validate_landscape(LegacyLandscape.model_validate(data))
    return Landscape.model_validate(data)


# --------------------------------------------------------------------------
# Internal: evidence bundle (code → model)
# --------------------------------------------------------------------------

class BundleItem(Strict):
    evidence_id: str
    paper_id: str
    label: str
    value_path: str                      # JSON Pointer into paper.json
    provenance_path: str                 # JSON Pointer of the record owning source_locations
    source_locations: list[SourceLocation]
    source_value: str
    context: str = ""
    boundary: bool = False
    truncated: bool = False


class OmittedItem(Strict):
    paper_id: str
    value_path: str
    reason: str


class EvidenceBundle(Strict):
    thread_id: str
    statement: str
    thread_papers: list[str]
    items: list[BundleItem]
    omitted: list[OmittedItem] = Field(default_factory=list)

    def ids(self) -> set[str]:
        return {i.evidence_id for i in self.items}

    def by_id(self) -> dict[str, BundleItem]:
        return {i.evidence_id: i for i in self.items}


# --------------------------------------------------------------------------
# Internal: draft (model → code)
# --------------------------------------------------------------------------

Stance = Literal["supports", "contradicts", "qualifies"]
FindingKind = Literal["confirms", "qualifies", "overturns", "extends"]
DraftOutcome = Literal["candidates", "insufficient_evidence", "comparability_issue"]


class DraftCondition(Strict):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)   # empty ⇒ hypothesis


class DraftFinding(Strict):
    statement: str
    finding_kind: FindingKind
    conditions: list[DraftCondition] = Field(default_factory=list)
    evidence_ids: list[str]
    stance_by_evidence: dict[str, Stance]


class DraftSide(Strict):
    assertion: str
    evidence_ids: list[str]


class DraftExplanation(Strict):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class DraftTension(Strict):
    statement: str
    sides: list[DraftSide]
    candidate_explanations: list[DraftExplanation] = Field(default_factory=list)


class ThreadReasoningDraft(Strict):
    thread_id: str
    outcome: DraftOutcome
    findings: list[DraftFinding] = Field(default_factory=list)
    tensions: list[DraftTension] = Field(default_factory=list)
    comparability_notes: list[str] = Field(default_factory=list)


STRUCTURAL, CITATION = "structural", "citation"


def draft_errors(draft: ThreadReasoningDraft, bundle: EvidenceBundle
                 ) -> list[tuple[str, str]]:
    """Referential checks beyond the schema, as (category, message).

    STRUCTURAL  the draft as a whole is unusable (thread id mismatch, stance
                keys not equal to evidence_ids, a tension without two sides,
                empty citation lists) → repair, then fail the thread.
    CITATION    an id that is not in the bundle, or a condition/explanation
                id outside its candidate's evidence → repair once; if it
                persists, only the affected candidate is rejected
                (invalid_citation), the rest of the draft proceeds."""
    errs: list[tuple[str, str]] = []
    known = bundle.ids()
    if draft.thread_id != bundle.thread_id:
        errs.append((STRUCTURAL, f"thread_id {draft.thread_id!r} != {bundle.thread_id!r}"))
    for i, f in enumerate(draft.findings):
        cited = set(f.evidence_ids)
        if not cited:
            errs.append((STRUCTURAL, f"findings[{i}]: evidence_ids is empty"))
        for eid in sorted(cited - known):
            errs.append((CITATION, f"findings[{i}]: unknown evidence id {eid}"))
        if set(f.stance_by_evidence) != cited:
            errs.append((STRUCTURAL, f"findings[{i}]: stance_by_evidence keys must equal "
                                     f"evidence_ids"))
        for j, c in enumerate(f.conditions):
            for eid in sorted(set(c.evidence_ids) - cited):
                errs.append((CITATION, f"findings[{i}].conditions[{j}]: id {eid} not in the "
                                       f"finding's evidence_ids"))
    for i, t in enumerate(draft.tensions):
        if len(t.sides) != 2:
            errs.append((STRUCTURAL, f"tensions[{i}]: exactly two sides required"))
        all_ids: set[str] = set()
        for s, side in enumerate(t.sides):
            if not side.evidence_ids:
                errs.append((STRUCTURAL, f"tensions[{i}].sides[{s}]: evidence_ids is empty"))
            for eid in sorted(set(side.evidence_ids) - known):
                errs.append((CITATION, f"tensions[{i}].sides[{s}]: unknown evidence id {eid}"))
            all_ids |= set(side.evidence_ids)
        for j, e in enumerate(t.candidate_explanations):
            for eid in sorted(set(e.evidence_ids) - all_ids):
                errs.append((CITATION, f"tensions[{i}].candidate_explanations[{j}]: id {eid} "
                                       f"not in the tension's evidence"))
    return errs


def format_errors(errs: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {msg}" for _, msg in errs)


# --------------------------------------------------------------------------
# Internal: support review (code ↔ model)
# --------------------------------------------------------------------------

CandidateKind = Literal["finding", "observation", "tension"]
Decision = Literal["accept", "reject", "insufficient"]


class ReviewPage(Strict):
    page_id: str                          # code-owned, keyed (paper_id, page)
    paper_id: str
    page: int
    sha256: str
    text: str


class ReviewCandidate(Strict):
    candidate_id: str
    kind: CandidateKind
    payload: dict


class SupportReviewRequest(Strict):
    thread_id: str
    candidates: list[ReviewCandidate]
    pages: list[ReviewPage]


class ReviewDecision(Strict):
    candidate_id: str
    decision: Decision
    notes: str = ""
    page_ids: list[str] = Field(default_factory=list)


class SupportReviewResponse(Strict):
    decisions: list[ReviewDecision]


def review_errors(resp: SupportReviewResponse,
                  req: SupportReviewRequest) -> list[str]:
    """Decision set must equal the candidate set; every page_id must have
    been supplied. Any violation invalidates the whole review call."""
    errs: list[str] = []
    expected = {c.candidate_id for c in req.candidates}
    got = [d.candidate_id for d in resp.decisions]
    if len(set(got)) != len(got):
        errs.append("duplicate candidate_id in decisions")
    for cid in set(got) - expected:
        errs.append(f"unknown candidate_id {cid}")
    for cid in expected - set(got):
        errs.append(f"missing decision for {cid}")
    pages = {p.page_id for p in req.pages}
    for d in resp.decisions:
        for pid in set(d.page_ids) - pages:
            errs.append(f"decision {d.candidate_id}: unknown page_id {pid}")
    return errs


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

Confidence = Literal["low", "medium", "high"]
Verification = Literal["verified"]


class Evidence(Strict):
    evidence_id: str
    paper_id: str
    source_kind: Literal["paper_json"] = "paper_json"
    value_path: str
    provenance_path: str
    source_locations: list[SourceLocation]
    source_value: str
    summary: str                          # deterministic: "<label> — <source_value>"
    stance: Stance
    artifact_sha256: str


class Condition(Strict):
    text: str
    evidence_ids: list[str]
    hypothesis: bool


class ReviewSource(Strict):
    paper_id: str
    page: int
    key_or_path: str
    sha256: str


class AgreementCounts(Strict):
    supports: int = 0
    qualifies: int = 0
    contradicts: int = 0
    mixed: int = 0
    unknown: int = 0


class Agreement(Strict):
    scope: Literal["thread"] = "thread"
    denominator: int
    paper_ids: list[str]
    counts: AgreementCounts


class CrossPaperFinding(Strict):
    finding_id: str
    thread_id: str
    refines: list[str]
    finding_kind: FindingKind
    statement: str
    conditions: list[Condition]
    evidence: list[Evidence]
    agreement: Agreement
    confidence: Confidence
    verification: Verification = "verified"
    review_sources: list[ReviewSource]
    review_notes: str | None = None


class Observation(Strict):
    observation_id: str
    thread_id: str
    refines: list[str]
    statement: str
    conditions: list[Condition]
    evidence: list[Evidence]
    verification: Verification = "verified"
    review_sources: list[ReviewSource]
    review_notes: str | None = None


class TensionSide(Strict):
    assertion: str
    evidence: list[Evidence]


class Explanation(Strict):
    text: str
    evidence_ids: list[str]
    hypothesis: bool


class Tension(Strict):
    tension_id: str
    thread_id: str
    refines: list[str]
    statement: str
    sides: list[TensionSide]
    candidate_explanations: list[Explanation]
    distinct_papers: int
    verification: Verification = "verified"
    review_sources: list[ReviewSource]
    review_notes: str | None = None


DiagnosticReason = Literal[
    "invalid_citation", "no_valid_evidence", "unresolved_location",
    "unverifiable_location", "verification_skipped", "insufficient_pages",
    "rejected_on_review", "review_invalid", "insufficient_for_adjudication",
    "budget_skipped", "insufficient_evidence", "comparability_issue",
    "draft_invalid", "failed",
]


class CandidateDiagnostic(Strict):
    candidate_id: str
    kind: Literal["finding", "observation", "tension", "thread"]
    thread_id: str
    payload_snapshot: dict = Field(default_factory=dict)
    reason: DiagnosticReason
    detail: str = ""


class Coverage(Strict):
    papers: dict[str, int]
    threads: dict[str, int]
    citations: dict[str, int]
    candidates: dict[str, object]
    review_pages: dict[str, int]
    calls: dict[str, int]


class RunInfo(Strict):
    provider: str | None
    draft_model: str | None
    review_model: str | None
    prompt_version: str
    schema_version: str = OUTPUT_SCHEMA_VERSION
    budgets: dict[str, int]
    verify_mode: Literal["pages", "none"]
    token_counter: str
    started: str
    ended: str | None = None


class CrossPaperReasoning(Strict):
    schema_version: Literal["cross_paper_v1"] = OUTPUT_SCHEMA_VERSION
    topic: str
    landscape_ref: dict[str, str]
    run: RunInfo
    findings: list[CrossPaperFinding]
    observations: list[Observation]
    tensions: list[Tension]
    diagnostics: list[CandidateDiagnostic]
    coverage: Coverage
    usage: dict[str, dict[str, int]]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="Print the JSON Schemas of the input, draft, "
                             "review, and output contracts")
    parser.add_argument("--landscape", default=None,
                        help="Validate this landscape JSON file")
    args = parser.parse_args()
    if args.landscape:
        try:
            land = load_landscape(Path(args.landscape))
        except (LandscapeValidationError, ValidationError, json.JSONDecodeError) as e:
            sys.exit(str(e))
        count = (len(land.items) if isinstance(land, LegacyLandscape) else
                 sum(len(getattr(land, name)) for name in
                     ("aggregated_findings", "recurring_limitations",
                      "common_assumptions", "underexplored_regimes")))
        print(f"OK: {len(land.paper_ids)} papers, {len(land.groups)} groups, "
              f"{count} items, {len(land.contradictions)} "
              f"contradictions, {len(land.relationships)} relationships")
    if args.check or not args.landscape:
        out = {name: m.model_json_schema() for name, m in [
            ("Landscape", Landscape), ("LegacyLandscape", LegacyLandscape),
            ("EvidenceBundle", EvidenceBundle),
            ("ThreadReasoningDraft", ThreadReasoningDraft),
            ("SupportReviewRequest", SupportReviewRequest),
            ("SupportReviewResponse", SupportReviewResponse),
            ("CrossPaperReasoning", CrossPaperReasoning)]}
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
