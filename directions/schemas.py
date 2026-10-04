"""Generation contracts: model-owned proposals, code-owned evidence and lineage."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opportunities.schemas import Opportunity
from reasoning.schemas import ReviewSource

Text = Annotated[str, Field(min_length=1)]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class GroundedRationale(Strict):
    statement: Text
    evidence_ids: list[Text] = Field(min_length=1)
    page_ids: list[Text] = Field(min_length=1)


class Hypothesis(Strict):
    hypothesis_id: Text
    condition: Text
    intervention: Text
    expected_effect: Text
    mechanism: Text
    assumptions: list[Text] = Field(min_length=1)
    falsification_criterion: Text
    evidence_ids: list[Text] = Field(min_length=1)


class Experiment(Strict):
    experiment_id: Text
    stage: Literal['initial', 'followup']
    hypothesis_ids: list[Text] = Field(min_length=1)
    depends_on: list[Text]
    objective: Text
    setup: Text
    intervention: Text
    baselines: list[Text] = Field(min_length=1)
    metrics: list[Text] = Field(min_length=1)
    controls: list[Text] = Field(min_length=1)
    resource_requirements: Text
    cost_rationale: Text
    informative_outcomes: Text


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
    what_would_falsify_it: Text

    @model_validator(mode='after')
    def check_experiment_graph(self):
        hids = [h.hypothesis_id for h in self.hypotheses]
        eids = [e.experiment_id for e in self.experiments]
        if len(set(hids)) != len(hids) or len(set(eids)) != len(eids):
            raise ValueError('hypothesis and experiment IDs must be unique within the direction')
        if self.experiments[0].stage != 'initial' or self.experiments[0].depends_on:
            raise ValueError('first experiment must be the independent initial test')
        seen, tested = set(), set()
        for i, experiment in enumerate(self.experiments):
            if (len(set(experiment.hypothesis_ids)) != len(experiment.hypothesis_ids)
                    or not set(experiment.hypothesis_ids) <= set(hids)):
                raise ValueError('experiment references unknown or duplicate hypotheses')
            if i and (experiment.stage != 'followup' or not experiment.depends_on):
                raise ValueError('later experiments must be followups with explicit dependencies')
            if (len(set(experiment.depends_on)) != len(experiment.depends_on)
                    or not set(experiment.depends_on) <= seen):
                raise ValueError('experiment dependencies must reference earlier experiments only')
            seen.add(experiment.experiment_id)
            tested.update(experiment.hypothesis_ids)
        if tested != set(hids):
            raise ValueError('each hypothesis needs at least one experiment')
        return self


class GenerationResponse(Strict):
    direction: DirectionDraft | None
    abstention_reason: Text | None

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.direction is None) == (self.abstention_reason is None):
            raise ValueError('return a direction or a reason to abstain, exclusively')
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
    hypothesis_status: Literal['proposed_untested'] = 'proposed_untested'
    evidence_validation: Literal['references_and_artifacts_checked'] = 'references_and_artifacts_checked'
    scientific_review: Literal['not_assessed'] = 'not_assessed'
    literature_novelty: Literal['not_assessed'] = 'not_assessed'


class Diagnostic(Strict):
    opportunity_id: str
    reason: Literal['no_opportunities', 'invalid_source', 'missing_artifact',
                    'stale_evidence', 'missing_pages', 'page_budget', 'input_budget',
                    'call_budget', 'invalid_generation', 'abstained', 'provider_error', 'call_failed']
    detail: str


class GenerationResult(Strict):
    schema_version: Literal['directions_v1'] = 'directions_v1'
    topic: str
    landscape_ref: dict[str, str]
    reasoning_ref: dict[str, str]
    opportunities_ref: dict[str, str]
    directions: list[Direction]
    diagnostics: list[Diagnostic]
    coverage: dict[str, int]
    calls: dict[str, int]
    usage: dict[str, dict[str, int]]
    run: dict
