"""Generation contracts: model-owned proposals, code-owned evidence and lineage."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opportunities.schemas import Opportunity
from reasoning.schemas import ReviewSource

Text = Annotated[str, Field(min_length=1)]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class GroundedRationale(Strict):
    statement: Text = Field(description='Grounded motivation; distinguish author statements from inferred coverage gaps and unknowns.')
    evidence_ids: list[Text] = Field(min_length=1)
    page_ids: list[Text] = Field(min_length=1)


class Hypothesis(Strict):
    hypothesis_id: Text
    condition: Text
    intervention: Text
    expected_effect: Text
    mechanism: Text
    assumptions: list[Text] = Field(min_length=1)
    falsification_criterion: Text = Field(description='Evidence contrary to this hypothesis’s expected effect under its stated conditions; use the same success criterion.')
    evidence_ids: list[Text] = Field(min_length=1)


class Experiment(Strict):
    """Suggested informative test, not an execution protocol."""
    experiment_id: Text
    stage: Literal['initial', 'followup']
    hypothesis_ids: list[Text] = Field(min_length=1)
    objective: Text
    comparison: Text = Field(description='High-level comparison that addresses the linked hypotheses.')
    observations: list[Text] = Field(min_length=1, description='What to examine or measure, without a measurement protocol.')
    why_this_test: Text = Field(description='Why this is an informative, economical first test or useful followup; no resource estimates.')
    informative_outcomes: Text = Field(description='Interpret outcomes relative to the identified linked hypothesis, not the paper conclusion; state partial or inconclusive coverage honestly.')


class DirectionDraft(Strict):
    title: Text
    research_direction: Text
    rationale: list[GroundedRationale] = Field(min_length=1)
    proposed_mechanism: Text
    scope: Text
    assumptions: list[Text] = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(min_length=1)
    experiments: list[Experiment] = Field(min_length=1)
    risks: list[Text] = Field(min_length=1)
    uncertainties: list[Text] = Field(min_length=1)
    what_would_falsify_it: Text = Field(description='What would undermine the proposed mechanism or benefit. A negative answer can still resolve a useful research question; do not call the question itself falsified.')

    @model_validator(mode='after')
    def check_suggested_tests(self):
        hids = [h.hypothesis_id for h in self.hypotheses]
        eids = [e.experiment_id for e in self.experiments]
        if len(set(hids)) != len(hids) or len(set(eids)) != len(eids):
            raise ValueError('hypothesis and experiment IDs must be unique within the direction')
        if self.experiments[0].stage != 'initial':
            raise ValueError('first suggested test must be initial')
        tested = set()
        for i, experiment in enumerate(self.experiments):
            if (len(set(experiment.hypothesis_ids)) != len(experiment.hypothesis_ids)
                    or not set(experiment.hypothesis_ids) <= set(hids)):
                raise ValueError('experiment references unknown or duplicate hypotheses')
            if i and experiment.stage != 'followup':
                raise ValueError('later suggested tests must be followups')
            tested.update(experiment.hypothesis_ids)
        if tested != set(hids):
            raise ValueError('each hypothesis needs at least one suggested test')
        return self


class GenerationResponse(Strict):
    direction: DirectionDraft | None
    abstention_reason: Text | None

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.direction is None) == (self.abstention_reason is None):
            raise ValueError('return a direction or a reason to abstain, exclusively')
        return self


class SourceSpan(Strict):
    page_id: Text
    quote: Text


class CorrectnessIssue(Strict):
    category: Literal['grounding', 'attribution', 'consistency', 'scope']
    field_path: Text = Field(description='JSON Pointer into the candidate, e.g. /hypotheses/0/expected_effect.')
    explanation: Text
    required_change: Text
    source_spans: list[SourceSpan]

    @model_validator(mode='after')
    def grounded_issue(self):
        if self.category in ('grounding', 'attribution') and not self.source_spans:
            raise ValueError('grounding/attribution issues require original-page support')
        return self


class CorrectnessResponse(Strict):
    decision: Literal['pass', 'revise', 'abstain']
    summary: Text
    issues: list[CorrectnessIssue]

    @model_validator(mode='after')
    def decision_agrees(self):
        if self.decision == 'pass' and self.issues:
            raise ValueError('pass cannot carry unresolved issues')
        if self.decision == 'revise' and not self.issues:
            raise ValueError('revise requires actionable issues')
        return self


class ReviewRecord(Strict):
    review_id: Text
    opportunity_id: Text
    round: Literal[0, 1]
    proposal: DirectionDraft
    prompt_version: Text
    model: str | None
    report: CorrectnessResponse | None
    error: str | None

    @model_validator(mode='after')
    def completed_attempt(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('review record needs a report or an error, exclusively')
        return self


class Direction(Strict):
    direction_id: str
    opportunity_id: str
    proposal: DirectionDraft
    # Immutable-at-generation upstream snapshot keeps exact gap, roles and evidence.
    opportunity: Opportunity
    paper_artifact_hashes: dict[str, str]
    context_pages: list[ReviewSource]
    recommended_next_experiment_id: str
    correctness_review_id: Text
    correctness_review: Literal['passed'] = 'passed'
    hypothesis_status: Literal['proposed_untested'] = 'proposed_untested'
    evidence_validation: Literal['references_and_artifacts_checked'] = 'references_and_artifacts_checked'
    scientific_review: Literal['not_assessed'] = 'not_assessed'
    literature_novelty: Literal['not_assessed'] = 'not_assessed'


class Diagnostic(Strict):
    opportunity_id: str
    reason: Literal['no_opportunities', 'invalid_source', 'missing_artifact',
                    'stale_evidence', 'missing_pages', 'page_budget', 'input_budget',
                    'call_budget', 'invalid_generation', 'invalid_review', 'review_abstained',
                    'correctness_unresolved', 'abstained', 'provider_error', 'call_failed']
    detail: str


class GenerationResult(Strict):
    schema_version: Literal['directions_v3'] = 'directions_v3'
    topic: str
    landscape_ref: dict[str, str]
    reasoning_ref: dict[str, str]
    opportunities_ref: dict[str, str]
    directions: list[Direction]
    reviews: list[ReviewRecord]
    diagnostics: list[Diagnostic]
    coverage: dict[str, int]
    calls: dict[str, int | None]
    usage: dict[str, dict[str, int]]
    run: dict


    @model_validator(mode='after')
    def only_reviewed_proposals_advance(self):
        by_id = {r.review_id: r for r in self.reviews}
        if len(by_id) != len(self.reviews):
            raise ValueError('duplicate correctness review IDs')
        for d in self.directions:
            review = by_id.get(d.correctness_review_id)
            if (review is None or review.opportunity_id != d.opportunity_id
                    or review.report is None or review.report.decision != 'pass'
                    or review.proposal != d.proposal):
                raise ValueError('direction must match its exact independently passed proposal')
            if self.run.get('correctness_contract') == 'test_links_v1' or d.direction_id in self.run.get('revision_direction_ids', []):
                from directions.test_links import LinkedCorrectnessResponse, validate_links
                linked = LinkedCorrectnessResponse.model_validate({**self.run.get('test_link_reviews', {}), **self.run.get('revision_test_link_reviews', {})}.get(review.review_id, {}))
                validate_links(linked.test_link_checks, d.proposal)
                if linked.basic() != review.report:
                    raise ValueError('saved link audit differs from accepted correctness review')
            if any(r.opportunity_id == d.opportunity_id and r.round > review.round for r in self.reviews):
                raise ValueError('direction cannot bypass its latest review')
        return self
