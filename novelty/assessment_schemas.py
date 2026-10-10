"""Sections 13–14: immutable evidence, explicit coverage, reviewed refinement plans."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from directions.schemas import Strict, Text, GenerationResult
from novelty.schemas import NoveltySearchResult
from novelty.comparison_schemas import NoveltyComparisonResult
from opportunities.miner import digest


class Invalidation(Strict):
    direction_id: Text
    paper_id: Text
    target_ids: list[Text] = Field(default_factory=list, description='Empty means all targets, subject to claim_ids if supplied.')
    claim_ids: list[Text] = Field(default_factory=list, description='Empty means invalidate every selected pair; otherwise invalidate dependents of these claims.')
    reason: Text


class Invalidations(Strict):
    schema_version: Literal['novelty_invalidations_v1'] = 'novelty_invalidations_v1'
    comparison_sha256: Text
    entries: list[Invalidation]


class AssessmentInputs(Strict):
    comparison_coverage_threshold: float | None = Field(default=None, gt=0, le=1, exclude_if=lambda v: v is None)
    individual_assessments: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    generation: GenerationResult
    search: NoveltySearchResult
    comparisons: NoveltyComparisonResult
    invalidations: Invalidations | None = None

    @model_validator(mode='after')
    def handoff(self):
        gen, search, comp = self.generation, self.search, self.comparisons
        ref = {'schema_version': gen.schema_version, 'sha256': digest(gen.model_dump())}
        if search.directions_ref != ref or comp.directions_ref != ref:
            raise ValueError('generation lineage mismatch')
        if comp.search_ref != {'schema_version': search.schema_version, 'sha256': digest(search.model_dump())}:
            raise ValueError('comparison search lineage mismatch')
        directions = {d.direction_id: d for d in gen.directions}
        if (len(directions) != len(gen.directions)
                or sorted(directions) != sorted(c.direction_id for c in search.candidates)
                or sorted(directions) != sorted(c.direction_id for c in comp.candidates)):
            raise ValueError('every direction needs exactly one search and comparison record')
        for c in comp.candidates:
            s = next(s for s in search.candidates if s.direction_id == c.direction_id)
            d = directions[c.direction_id]
            if c.candidate_sha256 != s.candidate_sha256 or c.candidate_sha256 != digest(d.model_dump()):
                raise ValueError('candidate snapshot mismatch')
            if c.signature != s.signature:
                raise ValueError('reviewed signature mismatch')
            expected = {d.direction_id: 'direction', **{h.hypothesis_id: 'hypothesis' for h in d.proposal.hypotheses}}
            if s.signature and {t.target_id: t.level for t in s.signature.targets} != expected:
                raise ValueError('signature must preserve every original target')
            if c.shortlist != {r.target_id: [p.paper_id for p in r.ranked] for r in s.searches}:
                raise ValueError('shortlist changed after retrieval')
            if c.retrieval_status != {r.target_id: r.status for r in s.searches}:
                raise ValueError('retrieval status mismatch')
            for p in c.papers:
                hashes = {r.paper_artifact_hashes[p.paper_id] for r in s.searches if p.paper_id in [x.paper_id for x in r.ranked]}
                if hashes != {p.expected_artifact_sha256}:
                    raise ValueError('prior artifact hash mismatch')
        if self.invalidations:
            if self.invalidations.comparison_sha256 != digest(comp.model_dump()):
                raise ValueError('invalidation references a different comparison artifact')
            for entry in self.invalidations.entries:
                c = next((c for c in comp.candidates if c.direction_id == entry.direction_id), None)
                p = next((p for p in c.papers if p.paper_id == entry.paper_id), None) if c else None
                if p is None or not set(entry.target_ids) <= set(p.target_ids):
                    raise ValueError('invalidation names unknown paper/target')
                claims = {x.claim_id for x in p.evidence_reviews[-1].record.claims} if p.evidence_reviews else set()
                if not set(entry.claim_ids) <= claims:
                    raise ValueError('invalidation names unknown final claim')
        return self


class CoverageRow(Strict):
    paper_id: Text
    status: Literal['reviewed', 'withheld', 'skipped', 'failed', 'invalidated']
    reasons: list[Text]
    missing_referenced_pages: list[int]


class TargetCoverage(Strict):
    target_id: Text
    level: Literal['direction', 'hypothesis']
    retrieval_status: Literal['complete', 'partial', 'failed', 'not_run']
    papers: list[CoverageRow]
    blockers: list[Text]
    complete: bool


class EvidenceRef(Strict):
    paper_id: Text
    target_id: Text
    claim_ids: list[Text] = Field(min_length=1)


class TargetAssessment(Strict):
    target_id: Text
    finding: Literal['ALREADY_STUDIED', 'PARTIAL_OVERLAP', 'COMPONENTS_KNOWN', 'LOW_PRIOR_OVERLAP', 'UNRESOLVED']
    reasoning: Text
    evidence: list[EvidenceRef]
    meaningful_difference: Text | None = Field(description='A scientific distinction, not merely renamed components or a new model/dataset. Null if unresolved or no remaining distinction.')


class ScientificEdit(Strict):
    field_path: Text = Field(description='JSON Pointer to an existing scientific proposal field; IDs, evidence and rationale cannot be edited.')
    proposed_value: Text | list[Text]
    reason: Text


class Refinement(Strict):
    action: Literal['retain', 'narrow', 'reframe', 'reject', 'defer']
    rationale: Text
    retained_hypothesis_ids: list[Text]
    removed_hypothesis_ids: list[Text]
    scientific_edits: list[ScientificEdit]


class AssessmentDraft(Strict):
    targets: list[TargetAssessment] = Field(min_length=1)
    refinement: Refinement


class ReassessmentRequest(Strict):
    paper_id: Text
    target_ids: list[Text] = Field(min_length=1)
    claim_ids: list[Text]
    passage_ids: list[Text]
    reason: Text


class SynthesisScopeCheck(Strict):
    target_id: Text
    decision: Literal['pass', 'revise', 'abstain']
    summary: Text
    scope_defects: list[Text] = Field(description='Every unsupported/ambiguous model, comparator, dataset, regime or result attribution in synthesis prose, even if it would not change the overlap finding. Empty only if none.')

    @model_validator(mode='after')
    def no_hidden_defects(self):
        if self.scope_defects and self.decision == 'pass':
            raise ValueError('scope defects require revision or abstention')
        return self


class AssessmentReport(Strict):
    decision: Literal['pass', 'revise', 'abstain']
    summary: Text
    issues: list[Text]
    reassessment_requests: list[ReassessmentRequest]
    scope_checks: list[SynthesisScopeCheck] = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self):
        if self.decision == 'pass' and (self.issues or any(c.decision != 'pass' for c in self.scope_checks)):
            raise ValueError('passing review cannot hide unresolved issues')
        if self.decision == 'revise' and not (self.issues or self.reassessment_requests or any(c.decision != 'pass' for c in self.scope_checks)):
            raise ValueError('revision needs actionable feedback')
        return self


class AssessmentReview(Strict):
    round: Literal[0, 1]
    draft: AssessmentDraft
    model: str | None
    report: AssessmentReport | None
    error: str | None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('review needs report or error exclusively')
        return self


class FormatRepair(Strict):
    task: Text
    invalid_output: str | dict
    validation_error: Text


class CandidateAssessment(Strict):
    direction_id: Text
    candidate_sha256: Text
    coverage: list[TargetCoverage]
    assessment: AssessmentDraft | None
    reviews: list[AssessmentReview]
    format_repairs: list[FormatRepair]
    diagnostic: str | None

    hypothesis_assessments: list[CandidateAssessment] | None = Field(default=None, exclude_if=lambda v: v is None)

    def outcomes(self):
        """Code-owned interpretation; unreviewed drafts can never publish findings."""
        accepted = {t.target_id: t for h in self.hypothesis_assessments or [] if h.assessment for t in h.assessment.targets}
        if self.assessment:
            accepted.update({t.target_id: t for t in self.assessment.targets})
        return [dict(target_id=c.target_id, level=c.level, coverage_complete=c.complete,
            finding=accepted[c.target_id].finding if c.target_id in accepted else 'UNRESOLVED',
            novelty_status=('overlap_found' if accepted[c.target_id].finding == 'ALREADY_STUDIED'
                else 'low_overlap_in_searched_evidence' if accepted[c.target_id].finding == 'LOW_PRIOR_OVERLAP'
                else 'unresolved' if not c.complete or accepted[c.target_id].finding == 'UNRESOLVED'
                else 'partial_overlap') if c.target_id in accepted else 'unresolved',
            literature_wide_novelty='unverified') for c in self.coverage]

    def refinement_handoff(self, direction):
        if not self.assessment:
            return None
        r = self.assessment.refinement
        changed = set()
        for edit in r.scientific_edits:
            if edit.field_path.startswith('/hypotheses/'):
                changed.add(direction.proposal.hypotheses[int(edit.field_path.split('/')[2])].hypothesis_id)
            else:
                changed.update(r.retained_hypothesis_ids)
        # Removing hypotheses changes direction scope, but not unchanged survivors' identity.
        recheck = [] if r.action == 'reject' else ([direction.direction_id] if r.scientific_edits or r.removed_hypothesis_ids else []) + sorted(changed)
        return dict(original_candidate_sha256=self.candidate_sha256, proposal_applied=False,
            unresolved_target_ids=[o['target_id'] for o in self.outcomes() if o['novelty_status'] == 'unresolved'],
            scientific_quality='not_assessed',
            requires_novelty_recheck_target_ids=recheck,
            next_stage='direction_revision_then_signature_search_comparison' if r.scientific_edits else
                'narrow_direction_and_recheck_scope' if r.removed_hypothesis_ids and r.retained_hypothesis_ids else
                'stop' if r.action == 'reject' else 'resolve_coverage' if r.action == 'defer' else 'research_critic',
            retained_experiment_links={e.experiment_id: [h for h in e.hypothesis_ids if h in r.retained_hypothesis_ids]
                for e in direction.proposal.experiments if set(e.hypothesis_ids) & set(r.retained_hypothesis_ids)})


class NoveltyAssessmentResult(Strict):
    schema_version: Literal['novelty_assessment_v1'] = 'novelty_assessment_v1'
    inputs: AssessmentInputs
    inputs_sha256: Text
    candidates: list[CandidateAssessment]
    calls: dict[str, int | None]
    usage: dict[str, dict[str, int]]
    run: dict
    literature_wide_novelty: Literal['unverified'] = 'unverified'
    scientific_quality: Literal['not_assessed'] = 'not_assessed'

    @model_validator(mode='after')
    def reviewed_handoff(self):
        from novelty.assessment_evidence import build_packet, hypothesis_packet, validate_draft, validate_report
        if self.inputs_sha256 != digest(self.inputs.model_dump()):
            raise ValueError('assessment input hash mismatch')
        if sorted(c.direction_id for c in self.candidates) != sorted(d.direction_id for d in self.inputs.generation.directions):
            raise ValueError('assessment must preserve every direction')
        for c in self.candidates:
            packet = build_packet(self.inputs, c.direction_id)
            if c.candidate_sha256 != packet['candidate_sha256'] or [v.model_dump() for v in c.coverage] != packet['coverage']:
                raise ValueError('coverage or candidate differs from immutable inputs')
            if self.inputs.individual_assessments:
                expected = {h.hypothesis_id for d in self.inputs.generation.directions if d.direction_id == c.direction_id for h in d.proposal.hypotheses}
                children = c.hypothesis_assessments or []
                if len(children) != len(expected) or any(len(x.coverage) != 1 for x in children) or {x.coverage[0].target_id for x in children} != expected:
                    raise ValueError('individual assessment must preserve every hypothesis')
                for child in children:
                    scoped = hypothesis_packet(packet, child.coverage[0].target_id)
                    if child.direction_id != c.direction_id or child.hypothesis_assessments is not None or child.candidate_sha256 != c.candidate_sha256 or [v.model_dump() for v in child.coverage] != scoped['coverage']:
                        raise ValueError('hypothesis coverage differs from immutable inputs')
                    if len(child.reviews) > 2 or [r.round for r in child.reviews] != list(range(len(child.reviews))):
                        raise ValueError('invalid hypothesis review history')
                    for r in child.reviews:
                        validate_draft(r.draft, scoped)
                        if r.report: validate_report(r.report, scoped)
                    if child.assessment is not None:
                        if not child.reviews or not child.reviews[-1].report or child.reviews[-1].report.decision != 'pass' or child.reviews[-1].draft != child.assessment or any(r.report and r.report.reassessment_requests for r in child.reviews):
                            raise ValueError('hypothesis assessment requires independent acceptance')
                        validate_draft(child.assessment, scoped)
                    elif not child.diagnostic:
                        raise ValueError('withheld hypothesis requires a diagnostic')
            if len(c.reviews) > 2 or [r.round for r in c.reviews] != list(range(len(c.reviews))):
                raise ValueError('invalid assessment review history')
            for r in c.reviews:
                validate_draft(r.draft, packet)
                if r.report:
                    validate_report(r.report, packet)
            if c.assessment is not None:
                if any(r.report and r.report.reassessment_requests for h in c.hypothesis_assessments or [] for r in h.reviews):
                    raise ValueError('hypothesis source reassessment must be resolved before publication')
                if not c.reviews or c.reviews[-1].report is None or c.reviews[-1].report.decision != 'pass' or c.reviews[-1].draft != c.assessment:
                    raise ValueError('assessment must equal the latest independently passing draft')
                if any(r.report and r.report.reassessment_requests for r in c.reviews):
                    raise ValueError('source reassessment must be resolved upstream before publication')
                validate_draft(c.assessment, packet)
            elif not c.diagnostic:
                raise ValueError('withheld assessment requires a diagnostic')
        return self
