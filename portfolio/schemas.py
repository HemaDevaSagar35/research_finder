"""Sections 16–17: source-bound candidate reports and final selection."""
from typing import Literal
from pydantic import Field, model_validator
from directions.schemas import Strict, Text, Hypothesis, Experiment, GroundedRationale
from reasoning.schemas import Evidence, ReviewSource
from opportunities.schemas import PaperAssessment
from novelty.assessment_schemas import TargetAssessment, TargetCoverage
from critic.schemas import CriticResult, TargetCritique, MergePlan
from novelty.comparison_schemas import PairComparison
from novelty.comparison_records import PriorClaim, Relationship
from critic.refinement_loop import RefinementResult
from opportunities.miner import digest
from portfolio.references import MetadataSnapshot, PaperReference


class SelectionSettings(Strict):
    min_directions: int = Field(default=3, ge=0)
    max_directions: int = Field(default=5, ge=1)

    @model_validator(mode='after')
    def bounds(self):
        if self.min_directions > self.max_directions:
            raise ValueError('min_directions cannot exceed max_directions')
        return self


class FinalHypothesis(Strict):
    proposal: Hypothesis
    novelty: TargetAssessment
    coverage: TargetCoverage
    critique: TargetCritique


class PriorWork(Strict):
    paper_id: Text
    target_id: Text
    comparison: PairComparison
    relationship: Relationship
    claims: list[PriorClaim]
    evidence_scope: Literal['available_extracted_pages']
    missing_referenced_pages: list[int]


class FinalCandidate(Strict):
    candidate_id: Text
    candidate_sha256: Text
    title: Text
    research_direction: Text
    proposed_mechanism: Text
    scope: Text
    assumptions: list[Text]
    why_this_direction_exists: list[GroundedRationale]
    unresolved_gap: Text
    supporting_evidence: list[Evidence]
    evidence_paper_roles: list[PaperAssessment]
    evidence_pages: list[ReviewSource]
    possible_hypotheses: list[FinalHypothesis]
    suggested_initial_experiments: list[Experiment]
    closest_prior_work: list[PriorWork]
    novelty_assessment: TargetAssessment
    novelty_coverage: list[TargetCoverage]
    risks: list[Text]
    uncertainties: list[Text]
    what_would_falsify_it: Text
    recommended_next_step: Text
    recommended_next_experiment_id: Text
    critique: TargetCritique
    scientific_validation: Literal['model_reviewed_not_empirically_validated'] = 'model_reviewed_not_empirically_validated'
    literature_wide_novelty: Literal['unverified'] = 'unverified'


class RankingEntry(Strict):
    candidate_id: Text
    rank: int = Field(ge=1)
    action: Literal['KEEP', 'DOWNRANK']
    concern_fraction: float = Field(ge=0, le=1)
    uncertain_fraction: float = Field(ge=0, le=1)
    novelty_priority: int = Field(ge=0, le=3)
    rationale: Text


class CandidateDisposition(Strict):
    candidate_id: Text
    disposition: Literal['selected', 'reserve', 'discarded', 'pending']
    route: dict


class FinalPortfolio(Strict):
    schema_version: Literal['final_portfolio_v1', 'final_portfolio_v2'] = 'final_portfolio_v2'
    metadata_inputs: list[MetadataSnapshot] = Field(default_factory=list)
    metadata_inputs_sha256: str | None = None
    references: list[PaperReference] = Field(default_factory=list)
    source: CriticResult | RefinementResult
    source_sha256: Text
    settings: SelectionSettings
    topic: str
    candidates: list[FinalCandidate]
    ranking: list[RankingEntry]
    dispositions: list[CandidateDisposition]
    pending_merges: list[MergePlan]
    status: Literal['ready', 'partial', 'blocked', 'empty']
    selection_shortfall: int = Field(ge=0)
    diagnostics: list[str]
    counts: dict[str, int]
    ranking_policy: Literal['reviewed_signals_v1'] = 'reviewed_signals_v1'
    unassessed_ranking_dimensions: list[str]
    literature_wide_novelty: Literal['unverified'] = 'unverified'

    @model_validator(mode='after')
    def exact_projection(self):
        from portfolio.pipeline import project
        if self.source_sha256 != digest(self.source.model_dump()):
            raise ValueError('portfolio source hash mismatch')
        expected = project(self.source, self.settings)
        actual = self.model_dump(exclude={'source', 'source_sha256', 'settings', 'schema_version', 'metadata_inputs', 'metadata_inputs_sha256', 'references'})
        if actual != expected:
            raise ValueError('portfolio differs from reviewed source or selection policy')
        if self.schema_version == 'final_portfolio_v2':
            from portfolio.references import bibliography
            from portfolio.pipeline import current_critic
            if self.metadata_inputs_sha256 != digest([r.model_dump() for r in self.metadata_inputs]):
                raise ValueError('bibliography metadata snapshot hash mismatch')
            if self.references != bibliography(current_critic(self.source), self.metadata_inputs):
                raise ValueError('bibliography differs from saved metadata and evidence')
        elif self.metadata_inputs or self.references or self.metadata_inputs_sha256 is not None:
            raise ValueError('bibliography requires final_portfolio_v2')
        return self
