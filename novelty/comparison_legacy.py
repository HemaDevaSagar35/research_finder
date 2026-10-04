"""Frozen v1 artifact reader for historical audits; not a production comparison path.

Old results are never silently converted to v2 or treated as newly reviewed.
"""
from typing import Literal
from pydantic import Field, model_validator
from directions.schemas import Strict, Text, SourceSpan, CorrectnessResponse
from reasoning.schemas import ReviewSource
from novelty.schemas import SignatureDraft

DIMENSIONS = ('problem', 'method', 'mechanism', 'signal', 'regime', 'evaluation', 'scientific_question', 'hypothesis')
Overlap = Literal['SAME', 'VERY_CLOSE', 'PARTIAL_OVERLAP', 'ADJACENT', 'DIFFERENT']


class DimensionComparison(Strict):
    dimension: Literal['problem', 'method', 'mechanism', 'signal', 'regime', 'evaluation', 'scientific_question', 'hypothesis']
    candidate_statement: Text
    candidate_paths: list[Text] = Field(min_length=1, description='JSON Pointers into the supplied target signature, e.g. /intervention/0/text.')
    prior_statement: Text
    relation: Literal['SAME', 'CLOSE', 'PARTIAL', 'DIFFERENT', 'UNKNOWN', 'NOT_APPLICABLE']
    source_spans: list[SourceSpan]
    rationale: Text

    @model_validator(mode='after')
    def evidence(self):
        if self.relation not in ('UNKNOWN', 'NOT_APPLICABLE') and not self.source_spans:
            raise ValueError('determinate dimension comparisons require original prior-paper quotes')
        return self


class PairComparison(Strict):
    target_id: Text
    level: Literal['direction', 'hypothesis']
    dimensions: list[DimensionComparison] = Field(min_length=8, max_length=8)
    classification: Overlap | None = Field(description='Null when supplied evidence is insufficient even for scoped overlap classification.')
    rationale: Text
    hypothesis_tested: Literal['tested', 'discussed_only', 'not_established', 'not_applicable']
    hypothesis_evidence: list[SourceSpan]
    remaining_uncertainties: list[Text]

    @model_validator(mode='after')
    def dimensions_and_claim(self):
        if sorted(d.dimension for d in self.dimensions) != sorted(DIMENSIONS):
            raise ValueError('all eight dimensions must appear exactly once')
        if self.hypothesis_tested in ('tested', 'discussed_only') and not self.hypothesis_evidence:
            raise ValueError('tested/discussed hypotheses require prior-paper source quotes')
        if self.level == 'hypothesis' and self.hypothesis_tested == 'not_applicable':
            raise ValueError('hypothesis-level comparison must assess whether it was tested')
        if self.classification == 'SAME':
            if self.level == 'hypothesis' and self.hypothesis_tested != 'tested':
                raise ValueError('SAME hypothesis requires evidence the relationship was tested')
            if any(d.relation not in ('SAME', 'NOT_APPLICABLE') for d in self.dimensions):
                raise ValueError('SAME requires all applicable dimensions to match')
        if self.classification is not None and not any(d.source_spans for d in self.dimensions):
            raise ValueError('overlap classification requires original-paper evidence')
        if (self.classification is None or any(d.relation == 'UNKNOWN' for d in self.dimensions)) and not self.remaining_uncertainties:
            raise ValueError('unknown comparison dimensions must remain explicit')
        return self


class ComparisonDraft(Strict):
    pairs: list[PairComparison] = Field(min_length=1)

    @model_validator(mode='after')
    def unique_targets(self):
        if len({p.target_id for p in self.pairs}) != len(self.pairs):
            raise ValueError('duplicate comparison targets')
        return self


class ComparisonResponse(Strict):
    comparison: ComparisonDraft | None
    abstention_reason: Text | None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.comparison is None) == (self.abstention_reason is None):
            raise ValueError('return comparison or abstention exclusively')
        return self


class ReviewFormatRepair(Strict):
    invalid_output: str | dict
    validation_error: Text
    model: str | None


class ComparisonReview(Strict):
    round: Literal[0, 1]
    draft: ComparisonDraft
    format_repairs: list[ReviewFormatRepair] = Field(default_factory=list, max_length=1)
    model: str | None
    report: CorrectnessResponse | None
    error: str | None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('review needs report or error exclusively')
        return self


class PaperComparison(Strict):
    paper_id: Text
    target_ids: list[Text] = Field(min_length=1)
    status: Literal['complete', 'unresolved', 'failed', 'skipped']
    expected_artifact_sha256: Text
    context_pages: list[ReviewSource]
    missing_referenced_pages: list[int]
    evidence_scope: Literal['available_extracted_pages'] = 'available_extracted_pages'
    comparison: ComparisonDraft | None
    reviews: list[ComparisonReview]
    diagnostic: str | None

    @model_validator(mode='after')
    def reviewed(self):
        page_keys = [(p.paper_id, p.page) for p in self.context_pages]
        if len(page_keys) != len(set(page_keys)) or any(p.paper_id != self.paper_id for p in self.context_pages):
            raise ValueError('prior-page manifest must contain unique pages of this paper')
        if set(self.missing_referenced_pages) & {p.page for p in self.context_pages}:
            raise ValueError('a page cannot be both loaded and missing')
        if len(set(self.target_ids)) != len(self.target_ids):
            raise ValueError('duplicate requested target IDs')
        if self.comparison is not None:
            if self.status != 'complete' or self.diagnostic or not self.context_pages or not self.reviews:
                raise ValueError('published comparison requires completed evidence-backed review')
            last = self.reviews[-1]
            if last.report is None or last.report.decision != 'pass' or last.draft != self.comparison:
                raise ValueError('comparison must equal latest independently passing review')
            page_ids = {f'{p.paper_id}#p{p.page}' for p in self.context_pages}
            for pair in self.comparison.pairs:
                spans = pair.hypothesis_evidence + [s for d in pair.dimensions for s in d.source_spans]
                if any(s.page_id not in page_ids for s in spans):
                    raise ValueError('published quotes require prior-paper page-manifest entries')
            if sorted(p.target_id for p in self.comparison.pairs) != sorted(self.target_ids):
                raise ValueError('comparison must cover every requested target exactly once')
        elif self.status == 'complete' or not self.diagnostic:
            raise ValueError('withheld comparison needs incomplete status and diagnostic')
        return self


class CandidateComparison(Strict):
    direction_id: Text
    candidate_sha256: Text
    signature: SignatureDraft | None
    shortlist: dict[str, list[str]]
    retrieval_status: dict[str, Literal['complete', 'partial', 'failed']]
    papers: list[PaperComparison]
    diagnostic: str | None

    @model_validator(mode='after')
    def coverage(self):
        if self.signature is None:
            if self.papers or self.shortlist or self.retrieval_status or not self.diagnostic:
                raise ValueError('withheld signature cannot produce comparisons')
            return self
        targets = {t.target_id: t.level for t in self.signature.targets}
        if set(targets) != set(self.shortlist) or set(targets) != set(self.retrieval_status):
            raise ValueError('coverage must retain every signature target')
        if any(len(v) != len(set(v)) for v in self.shortlist.values()):
            raise ValueError('duplicate shortlist paper IDs')
        expected = {(tid, pid) for tid, ids in self.shortlist.items() for pid in ids}
        actual = [(tid, p.paper_id) for p in self.papers for tid in p.target_ids]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError('every shortlisted pair needs a result or explicit diagnostic')
        if len({p.paper_id for p in self.papers}) != len(self.papers):
            raise ValueError('duplicate paper batches')
        for paper in self.papers:
            if paper.comparison and any(p.level != targets[p.target_id] for p in paper.comparison.pairs):
                raise ValueError('comparison target level changed')
        return self


class NoveltyComparisonResult(Strict):
    schema_version: Literal['novelty_comparison_v1'] = 'novelty_comparison_v1'
    directions_ref: dict[str, str]
    search_ref: dict[str, str]
    candidates: list[CandidateComparison]
    calls: dict[str, int]
    usage: dict[str, dict[str, int]]
    run: dict
    literature_novelty: Literal['not_assessed'] = 'not_assessed'

    @model_validator(mode='after')
    def unique(self):
        if len({c.direction_id for c in self.candidates}) != len(self.candidates):
            raise ValueError('duplicate comparison candidates')
        return self
