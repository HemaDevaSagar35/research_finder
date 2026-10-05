"""Immutable inputs, reviewed scientific judgments and code-owned routing."""
from typing import Literal

from pydantic import Field, model_validator
from directions.schemas import Strict, Text
from novelty.assessment_schemas import NoveltyAssessmentResult, FormatRepair
from opportunities.miner import digest

CRITERIA = ('gap_support', 'meaningful_distinction', 'combination_justification',
            'mechanism_plausibility', 'testability', 'falsifiability',
            'negative_result_value', 'research_contribution')
Criterion = Literal['gap_support', 'meaningful_distinction', 'combination_justification',
                    'mechanism_plausibility', 'testability', 'falsifiability',
                    'negative_result_value', 'research_contribution']
Action = Literal['KEEP', 'REFINE', 'DOWNRANK', 'DISCARD']


class Finding(Strict):
    criterion: Criterion
    assessment: Literal['adequate', 'concern', 'uncertain', 'not_applicable']
    basis: Literal['source_fact', 'scientific_inference', 'proposal_analysis']
    reasoning: Text
    passage_ids: list[Text]
    proposal_paths: list[Text]


class TargetCritique(Strict):
    target_id: Text
    action: Action
    rationale: Text
    findings: list[Finding] = Field(min_length=8, max_length=8)
    learning_if_negative: Text
    recommended_next_step: Text


class RevisionRequest(Strict):
    target_id: Text
    field_path: Text
    required_change: Text
    reason: Text


class UpstreamRequest(Strict):
    stage: Literal['direction_correctness', 'novelty_comparison', 'novelty_assessment']
    target_id: Text
    passage_ids: list[Text]
    reason: Text
    basis: Literal['source_grounding', 'proposal_consistency'] = 'source_grounding'
    field_paths: list[Text] = Field(default_factory=list)

    @model_validator(mode='after')
    def located(self):
        if self.basis == 'source_grounding' and not self.passage_ids:
            raise ValueError('source-grounding request requires source references')
        if self.basis == 'proposal_consistency' and (self.stage != 'direction_correctness' or not self.field_paths):
            raise ValueError('proposal consistency requires located direction-correctness request')
        return self


class CritiqueDraft(Strict):
    targets: list[TargetCritique] = Field(min_length=1)
    revisions: list[RevisionRequest]
    upstream_requests: list[UpstreamRequest]


class ReviewIssue(Strict):
    field_path: Text
    explanation: Text
    required_change: Text
    passage_ids: list[Text]


class ReviewCheck(Strict):
    target_id: Text
    reasoning: Text
    defects: list[Text]


class TestLinkCheck(Strict):
    hypothesis_id: Text
    experiment_id: Text
    prediction: Text
    equality_case: Text
    decision: Literal['consistent', 'contradiction', 'uncertain']
    reasoning: Text


class ReviewReport(Strict):
    decision: Literal['pass', 'revise', 'abstain']
    summary: Text
    issues: list[ReviewIssue]
    target_checks: list[ReviewCheck]
    test_link_checks: list[TestLinkCheck] = Field(default_factory=list)
    upstream_requests: list[UpstreamRequest]

    @model_validator(mode='after')
    def consistent(self):
        defects = self.issues or self.upstream_requests or any(c.defects for c in self.target_checks) or any(c.decision != 'consistent' for c in self.test_link_checks)
        if self.decision == 'pass' and defects:
            raise ValueError('passing review cannot hide defects or upstream requests')
        if self.decision == 'revise' and not defects:
            raise ValueError('revision needs actionable feedback')
        return self


class MergeMember(Strict):
    direction_id: Text
    hypothesis_ids: list[Text] = Field(min_length=1)


class MergePlan(Strict):
    action: Literal['MERGE'] = 'MERGE'
    members: list[MergeMember] = Field(min_length=2)
    combined_question: Text
    rationale: Text
    preserved_distinctions: Text
    passage_ids: list[Text] = Field(min_length=1)


class PortfolioDraft(Strict):
    considered_direction_ids: list[Text]
    merges: list[MergePlan]
    rationale: Text


class ReviewRecord(Strict):
    round: int = Field(ge=0, le=1)
    draft: CritiqueDraft | PortfolioDraft
    report: ReviewReport | None
    error: Text | None
    model: str | None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('review requires a report or error, exclusively')
        return self


class CandidateCritique(Strict):
    direction_id: Text
    candidate_sha256: Text
    packet: dict | None
    packet_sha256: Text | None
    critique: CritiqueDraft | None
    reviews: list[ReviewRecord]
    format_repairs: list[FormatRepair]
    diagnostic: str | None
    blocked_at: Literal['upstream', 'source_loading', 'critique'] | None

    def handoff(self):
        if self.critique is None:
            requests = [r.model_dump() for v in self.reviews
                        for r in [*v.draft.upstream_requests, *(v.report.upstream_requests if v.report else [])]]
            return dict(next_stage='resolve_upstream' if self.blocked_at == 'upstream' or requests else 'retry_critic',
                        scientific_action=None, diagnostic=self.diagnostic, upstream_requests=requests)
        draft = self.critique
        direction = next(t for t in draft.targets if t.target_id == self.direction_id)
        removed = [t.target_id for t in draft.targets if t.target_id != self.direction_id and t.action == 'DISCARD']
        kept = [t.target_id for t in draft.targets if t.target_id != self.direction_id and t.action != 'DISCARD']
        changed = {r.target_id for r in draft.revisions}
        for r in draft.revisions:
            if r.field_path.startswith('/experiments/'):
                experiment = self.packet['candidate']['experiments'][int(r.field_path.split('/')[2])]
                changed.update(experiment['hypothesis_ids'])
        if self.direction_id in changed:
            changed.update(kept)
        recheck = sorted({self.direction_id, *changed} if changed or removed else set())
        return dict(scientific_action=direction.action, retained_hypothesis_ids=kept,
                    removed_hypothesis_ids=removed, proposal_applied=False,
                    retained_experiment_links={e['experiment_id']: [h for h in e['hypothesis_ids'] if h in kept]
                        for e in self.packet['candidate']['experiments'] if set(e['hypothesis_ids']) & set(kept)},
                    requires_novelty_recheck_target_ids=[] if direction.action == 'DISCARD' else recheck,
                    next_stage='stop' if direction.action == 'DISCARD' else
                        'direction_revision_then_correctness_and_novelty' if changed else
                        'narrow_direction_and_recheck_scope' if removed else 'ranking',
                    scientific_validation='model_reviewed_not_empirically_validated')


class PortfolioCritique(Strict):
    packet: dict
    packet_sha256: Text
    assessment: PortfolioDraft | None
    reviews: list[ReviewRecord]
    format_repairs: list[FormatRepair]
    diagnostic: str | None


class CriticResult(Strict):
    schema_version: Literal['research_critic_v1', 'research_critic_v2'] = 'research_critic_v2'
    novelty: NoveltyAssessmentResult
    novelty_sha256: Text
    candidates: list[CandidateCritique]
    portfolio: PortfolioCritique
    calls: dict[str, int]
    usage: dict
    run: dict

    @model_validator(mode='after')
    def integrity(self):
        from critic.evidence import validate_result
        if self.novelty_sha256 != digest(self.novelty.model_dump()):
            raise ValueError('novelty lineage mismatch')
        validate_result(self)
        return self

    def handoff(self):
        merged = {m.direction_id for plan in self.portfolio.assessment.merges for m in plan.members} if self.portfolio.assessment else set()
        candidates = []
        for c in self.candidates:
            route = c.handoff()
            if c.blocked_at == 'upstream':
                direction = next(d for d in self.novelty.inputs.generation.directions if d.direction_id == c.direction_id)
                assessment = next(a for a in self.novelty.candidates if a.direction_id == c.direction_id)
                upstream = assessment.refinement_handoff(direction)
                if upstream:
                    route.update(next_stage=upstream['next_stage'], upstream_handoff=upstream)
            if c.direction_id in merged:
                route.update(next_stage='merge_revision_then_correctness_and_novelty',
                             requires_novelty_recheck_target_ids=[t.target_id for t in c.critique.targets])
            elif route['next_stage'] == 'ranking' and self.portfolio.assessment is None:
                route['next_stage'] = 'complete_portfolio_review'
            candidates.append(dict(direction_id=c.direction_id, **route))
        return dict(candidates=candidates, portfolio_complete=self.portfolio.assessment is not None,
                    literature_wide_novelty='unverified')
