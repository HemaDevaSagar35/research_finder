"""Retrieval preparation contracts; no novelty verdict is produced here."""
from typing import Literal

from pydantic import Field, model_validator

from directions.schemas import Strict, Text, SourceSpan, CorrectnessResponse
from research.retrieval import RetrievalResult
from reasoning.schemas import ReviewSource


class Facet(Strict):
    text: Text
    basis: Literal['source_fact', 'candidate_proposal', 'unknown'] = Field(description='Split mixed statements: candidate_proposal is an untested proposed relationship; an unestablished claim about the original implementation is unknown, even inside a proposed mechanism.')
    proposal_paths: list[Text] = Field(description='Exact JSON Pointers into candidate proposal. Unknowns also need provenance: cite e.g. /uncertainties/0, /risks/0 or the field containing the unestablished assertion.')
    evidence_ids: list[Text]
    source_spans: list[SourceSpan]

    @model_validator(mode='after')
    def provenance(self):
        if self.basis == 'source_fact' and (not self.evidence_ids or not self.source_spans):
            raise ValueError('source facts need evidence IDs and original-page quotations')
        if self.basis == 'candidate_proposal' and not self.proposal_paths:
            raise ValueError('proposals need candidate field pointers')
        if not (self.proposal_paths or self.evidence_ids or self.source_spans):
            raise ValueError('every facet needs provenance')
        return self


FACETS = ('problem', 'intervention', 'decision_signal', 'mechanism', 'regime', 'comparison', 'expected_effect')


class TargetSignature(Strict):
    target_id: Text
    level: Literal['direction', 'hypothesis']
    problem: list[Facet] = Field(min_length=1)
    intervention: list[Facet] = Field(min_length=1)
    decision_signal: list[Facet]
    mechanism: list[Facet]
    regime: list[Facet]
    comparison: list[Facet]
    expected_effect: list[Facet] = Field(min_length=1)
    queries: list[Text] = Field(min_length=1, max_length=20)


class SignatureDraft(Strict):
    targets: list[TargetSignature] = Field(min_length=1)


class SignatureResponse(Strict):
    signature: SignatureDraft | None
    abstention_reason: Text | None

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.signature is None) == (self.abstention_reason is None):
            raise ValueError('return signature or abstention exclusively')
        return self


class SignatureReview(Strict):
    round: Literal[0, 1]
    signature: SignatureDraft
    model: str | None
    report: CorrectnessResponse | None
    error: str | None

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('review needs report or error exclusively')
        return self


class RankedMatch(Strict):
    paper_id: Text
    why_relevant: Text


class RankingResponse(Strict):
    papers: list[RankedMatch]


class TargetRetrieval(Strict):
    target_id: Text
    status: Literal['complete', 'partial', 'failed']
    retrieval: RetrievalResult | None
    ranked: list[RankedMatch]
    paper_artifact_hashes: dict[str, str]
    unavailable_papers: list[str]
    diagnostic: str | None
    novelty: Literal['not_assessed'] = 'not_assessed'

    @model_validator(mode='after')
    def valid_ranking(self):
        ids = [p.paper_id for p in self.ranked]
        available = {p.paper_id for p in self.retrieval.papers} if self.retrieval else set()
        if len(ids) != len(set(ids)) or not set(ids) <= available:
            raise ValueError('ranking must contain unique retrieved papers')
        if not set(ids) <= set(self.paper_artifact_hashes):
            raise ValueError('ranked papers need loaded artifact hashes')
        if self.retrieval and (self.retrieval.filters.types or self.retrieval.filters.year is not None):
            raise ValueError('novelty retrieval must not inherit corpus restrictions')
        if self.status == 'complete' and (self.diagnostic or self.unavailable_papers or self.retrieval is None):
            raise ValueError('complete retrieval cannot hide errors')
        if self.status != 'complete' and not self.diagnostic:
            raise ValueError('incomplete retrieval needs a diagnostic')
        return self


class CandidateSearch(Strict):
    direction_id: Text
    candidate_sha256: Text
    context_pages: list[ReviewSource]
    signature: SignatureDraft | None
    reviews: list[SignatureReview]
    searches: list[TargetRetrieval]
    diagnostic: str | None

    @model_validator(mode='after')
    def reviewed_handoff(self):
        if self.signature is not None:
            if not self.reviews:
                raise ValueError('signature requires review')
            last = self.reviews[-1]
            if last.report is None or last.report.decision != 'pass' or last.signature != self.signature:
                raise ValueError('signature must equal latest passing review')
            ids = [s.target_id for s in self.signature.targets]
            if len(ids) != len(set(ids)):
                raise ValueError('duplicate signature target IDs')
            if sorted(s.target_id for s in self.searches) != sorted(ids):
                raise ValueError('every signature target needs a retrieval outcome')
            by_id = {t.target_id: t for t in self.signature.targets}
            for search in self.searches:
                if search.retrieval and search.retrieval.queries != by_id[search.target_id].queries:
                    raise ValueError('retrieval queries must equal the reviewed target queries')
        elif self.searches:
            raise ValueError('unreviewed signatures cannot search')
        elif not self.diagnostic:
            raise ValueError('withheld signature requires a diagnostic')
        return self


class NoveltySearchResult(Strict):
    schema_version: Literal['novelty_search_v1'] = 'novelty_search_v1'
    directions_ref: dict[str, str]
    candidates: list[CandidateSearch]
    calls: dict[str, int]
    usage: dict[str, dict[str, int]]
    run: dict
    novelty: Literal['not_assessed'] = 'not_assessed'
