"""Reviewed joint novelty assessment; prior evidence is never silently rewritten."""
from __future__ import annotations

from llm_client.progress import gather as progress_gather
import asyncio
from pydantic import Field
from datetime import datetime, timezone

from llm_client import errors, usage
from novelty import assessment_prompts as prompts
from novelty.assessment_schemas import (AssessmentInputs, Invalidations, AssessmentDraft,
    AssessmentReport, AssessmentReview, CandidateAssessment, FormatRepair,
    NoveltyAssessmentResult, TargetAssessment, DirectionSynthesis)
from novelty.assessment_evidence import build_packet, hypothesis_packet, hypothesis_draft, locked_targets, model_packet, restore_response_sources, validate_draft, validate_report
from novelty.comparison import Settings
from novelty.pipeline import ModelOutputError, NoveltySearcher, Settings as CallSettings
from opportunities.miner import digest
from reasoning.budget import TOKEN_COUNTER


class AssessmentSettings(Settings):
    comparison_coverage_threshold: float = Field(default=0.8, gt=0, le=1)
    individual_assessments: bool = True


class NoveltyAssessor:
    """One run per instance. Optional independent hypotheses precede joint synthesis."""
    def __init__(self, chat=None, *, review_chat=None, provider=None, model=None,
                 review_model=None, settings=None):
        self.settings = settings or Settings()
        self.io = NoveltySearcher(None, None, chat, review_chat=review_chat, provider=provider,
            model=model, review_model=review_model, settings=CallSettings(**self.settings.model_dump(exclude={"comparison_coverage_threshold", "individual_assessments"})))
        self.used = False

    async def _validated_call(self, task, system, schema, body, validator, repairs):
        transport = {**body, 'packet': model_packet(body['packet'])}
        if body['packet'].get('assessment_contract'):
            transport['available_evidence_references']=[dict(paper_id=e['paper_id'],target_id=e['target_id'],claim_ids=[c['claim_id'] for c in e['claims']]) for e in body['packet']['evidence']]
            transport['valid_scientific_edit_paths']=['/'+k for k in ('research_direction','proposed_mechanism','scope','assumptions','what_would_falsify_it')]+['/hypotheses/'+str(i)+'/'+k for i,_ in enumerate(body['packet']['candidate']['hypotheses']) for k in ('condition','intervention','expected_effect','mechanism','assumptions','falsification_criterion')]
        request = transport
        for attempt in range(2):
            response = None
            try:
                response, model = await self.io._call(task if attempt == 0 else 'repair_' + task,
                    system, schema, request)
                response = restore_response_sources(response, body['packet'])
                validator(response, body['packet'])
                return response, model
            except ModelOutputError as exc:
                raw, detail = exc.raw, str(exc)
            except ValueError as exc:
                if response is None:
                    raise
                raw, detail = response.model_dump(), str(exc)
            repairs.append(FormatRepair(task=task, invalid_output=raw, validation_error=detail))
            if attempt:
                raise ValueError('invalid_' + task + '_after_repair: ' + detail)
            request = {**transport, 'invalid_output': raw, 'validation_errors': detail}

    async def _candidate(self, packet, previous=None, observations=None, previous_review=None, hypothesis_results=None):
        reviews, repairs, assessment, diagnostic = [], [], None, None
        isolated = packet.get('assessment_contract') == 'isolated_v1'
        single = isolated and len(packet['coverage']) == 1 and packet['coverage'][0]['level'] == 'hypothesis'
        schema = TargetAssessment if single else DirectionSynthesis if isolated else AssessmentDraft
        prompt = prompts.HYPOTHESIS if single else prompts.SYNTHESIS if isolated else prompts.ASSESS
        def compose(value):
            if single: return hypothesis_draft(value, packet)
            if isolated: return AssessmentDraft(targets=[value.direction, *[TargetAssessment.model_validate(t) for t in packet['locked_hypotheses']]], refinement=value.refinement)
            return value
        def validate(value, source): validate_draft(compose(value), source)
        try:
            if not packet['evidence']:
                raise ValueError('No accepted prior-work evidence; all original targets remain unresolved.')
            body = {'packet': packet}
            if hypothesis_results is not None:
                body['reviewed_hypotheses'] = [dict(target_id=h.coverage[0].target_id, assessment=h.assessment.model_dump() if h.assessment else None, diagnostic=h.diagnostic) for h in hypothesis_results]
                body['synthesis_instruction'] = 'Synthesize the direction using these independently reviewed hypothesis results and original evidence. Preserve gaps, dependencies, and counterevidence; never equate comparison coverage with novelty.'
            if previous is not None:
                body.update(previous_assessment=previous.targets[0].model_dump() if single else previous.model_dump(), review_observations=observations,
                    correction_policy='Verify the reported concerns against the original sources. Correct only assessment-owned prose; do not change the candidate or prior evidence. Preserve explicit unspecified qualifiers in both reasoning and meaningful differences. Every output receives a fresh independent review.')
            if previous_review is not None:
                body['corrections']=previous_review.model_dump()
            if previous is None and observations:
                body['review_observations']=observations
                body['correction_policy']='Verify these source-scope concerns against the packet and retain all material qualifications in the assessment prose.'
            for round in range(2):
                draft, _ = await self._validated_call('assess_novelty' if round == 0 and previous is None else 'revise_novelty_assessment',
                    prompt, schema, body, validate, repairs)
                draft = compose(draft)
                report = error = None
                model = self.io.review_model or self.io.model
                try:
                    # Fresh independent conversation; no author corrections/history.
                    report, model = await self._validated_call('review_novelty_assessment', prompts.HYPOTHESIS_REVIEW if single else prompts.REVIEW.replace('Remove only hypotheses already studied.', 'Under isolated_v1, unresolved hypotheses may be deferred by removal from the retained subset.') + (' Review the exact retained subset. Locked hypothesis judgments cannot be edited here; flag source defects against their owner. The following lifecycle rules take precedence over generic retention guidance. In particular a defer plan preserves all IDs without approving unresolved hypotheses; do not require removals for defer. '+prompts.LIFECYCLE if isolated else ''),
                        AssessmentReport, {'packet': packet, 'assessment': draft.targets[0].model_dump() if single else draft.model_dump()}, validate_report, repairs)
                except Exception as exc:
                    error = errors.describe(exc)
                    raise
                finally:
                    reviews.append(AssessmentReview(round=round, draft=draft, model=model, report=report, error=error))
                if report.reassessment_requests:
                    diagnostic = 'Accepted upstream evidence needs section-12 reassessment; synthesis withheld.'
                    break
                if report.decision == 'pass':
                    assessment = draft
                    break
                diagnostic = 'Independent assessment review: ' + report.decision + ': ' + report.summary
                if report.decision == 'abstain' or round == 1:
                    break
                body = {'packet': packet, 'previous_assessment': draft.targets[0].model_dump() if single else draft.model_dump(), 'corrections': report.model_dump()}
        except Exception as exc:
            diagnostic = errors.describe(exc)
        return CandidateAssessment(direction_id=packet['direction_id'], candidate_sha256=packet['candidate_sha256'],
            coverage=packet['coverage'], assessment=assessment, reviews=reviews,
            format_repairs=repairs, diagnostic=None if assessment else diagnostic)

    async def run(self, generation, search, comparisons, *, invalidations: Invalidations | None = None,
                  recovery: NoveltyAssessmentResult | None = None, previous: NoveltyAssessmentResult | None = None, observations: dict[str,list[str]] | None = None, guidance: dict[str,list[str]] | None = None):
        if self.used:
            raise ValueError('create a new novelty assessor per run')
        self.used = True
        policy = dict(assessment_contract='isolated_v1' if self.settings.individual_assessments else None, comparison_coverage_threshold=self.settings.comparison_coverage_threshold, individual_assessments=self.settings.individual_assessments) if isinstance(self.settings, AssessmentSettings) else {}
        inputs = AssessmentInputs.model_validate(dict(**policy, generation=generation.model_dump(), search=search.model_dump(),
            comparisons=comparisons.model_dump(), invalidations=invalidations.model_dump() if invalidations else None))
        recovered = {}
        if recovery is not None:
            recovery = NoveltyAssessmentResult.model_validate(recovery.model_dump())
            current_ids={d.direction_id for d in inputs.generation.directions}
            def same_packet(did):
                before=build_packet(recovery.inputs,did);after=build_packet(inputs,did)
                return {k:v for k,v in before.items() if k!='retrieval_context'} == {k:v for k,v in after.items() if k!='retrieval_context'}
            recovered = {c.direction_id:c for c in recovery.candidates
                         if c.direction_id in current_ids and c.assessment is not None and (not inputs.individual_assessments or all(h.assessment is not None for h in c.hypothesis_assessments or [])) and recovery.inputs.assessment_contract == inputs.assessment_contract and recovery.inputs.comparison_coverage_threshold == inputs.comparison_coverage_threshold and recovery.inputs.individual_assessments == inputs.individual_assessments and same_packet(c.direction_id)}
        prior={}
        prior_drafts={}
        prior_reports={}
        if previous is not None or observations is not None:
            if previous is None or not observations:
                raise ValueError('synthesis reopening needs both a prior assessment and observations')
            previous=NoveltyAssessmentResult.model_validate(previous.model_dump())
            if previous.inputs!=inputs: raise ValueError('synthesis reopening inputs changed')
            prior={c.direction_id:c for c in previous.candidates}
            if not set(observations)<=set(prior) or any(not rows or any(not isinstance(x,str) or not x.strip() for x in rows) for rows in observations.values()):
                raise ValueError('invalid synthesis observation scope')
            for did in observations:
                candidate=prior[did]
                if candidate.assessment is not None:
                    prior_drafts[did]=candidate.assessment
                    continue
                last=candidate.reviews[-1] if candidate.reviews else None
                if last is None or last.report is None or last.report.decision!='revise' or any(r.report and r.report.reassessment_requests for r in candidate.reviews):
                    raise ValueError('synthesis reopening requires a published assessment or a reviewed draft with synthesis-owned revisions')
                prior_drafts[did]=last.draft
                prior_reports[did]=last.report
        if guidance and (not set(guidance)<={d.direction_id for d in inputs.generation.directions} or any(not isinstance(rows,list) or not rows or any(not isinstance(x,str) or not x.strip() for x in rows) for rows in guidance.values())):
            raise ValueError('invalid synthesis guidance scope')
        packets = [build_packet(inputs, d.direction_id) for d in inputs.generation.directions]
        started = datetime.now(timezone.utc).isoformat()
        before = {k: vars(v).copy() for k, v in usage.snapshot().items()}
        try:
            async def process(packet):
                did=packet['direction_id']
                if did in recovered and did not in (guidance or {}): return recovered[did]
                if previous is not None and did not in observations: return prior[did]
                hypotheses = None
                if inputs.individual_assessments:
                    saved = next((c for c in recovery.candidates if c.direction_id == did), None) if recovery else None
                    async def one(h):
                        scoped = hypothesis_packet(packet, h['hypothesis_id'])
                        old = next((x for x in saved.hypothesis_assessments or [] if x.coverage[0].target_id == h['hypothesis_id']), None) if saved else None
                        if old and old.assessment and old.candidate_sha256 == packet['candidate_sha256']:
                            original = hypothesis_packet(build_packet(recovery.inputs, did), h['hypothesis_id'])
                            if all(original[k] == scoped[k] for k in ('candidate','coverage','evidence','source_passages')):
                                return old
                        if old and old.reviews and old.reviews[-1].report and old.reviews[-1].report.decision=='revise' and not any(r.report and r.report.reassessment_requests for r in old.reviews):
                            return await self._candidate(scoped,previous=old.reviews[-1].draft,previous_review=old.reviews[-1].report)
                        return await self._candidate(scoped)
                    hypotheses = await progress_gather(*(one(h) for h in packet['candidate']['hypotheses']), label='hypothesis_assessments')
                    packet['locked_hypotheses'] = [t.model_dump() for t in locked_targets(packet, hypotheses)]
                saved = next((c for c in recovery.candidates if c.direction_id==did),None) if recovery else None
                if saved and saved.assessment is None and saved.reviews and saved.reviews[-1].report and saved.reviews[-1].report.decision=='revise' and not any(r.report and r.report.reassessment_requests for r in saved.reviews):
                    prior_drafts.setdefault(did,saved.reviews[-1].draft)
                    prior_reports.setdefault(did,saved.reviews[-1].report)
                result = await self._candidate(packet,prior_drafts.get(did),
                    observations.get(did) if observations else (guidance or {}).get(did), prior_reports.get(did), hypotheses)
                result.hypothesis_assessments = hypotheses
                if not inputs.assessment_contract and any(r.report and r.report.reassessment_requests for h in hypotheses or [] for r in h.reviews):
                    result.assessment = None
                    result.diagnostic = 'Hypothesis review requires upstream evidence reassessment; direction synthesis withheld.'
                if inputs.assessment_contract and result.assessment and result.assessment.refinement.action in ('retain','narrow') and not result.assessment.refinement.scientific_edits:
                    result.selection_scope = result.assessment.refinement.retained_hypothesis_ids
                return result
            results = await progress_gather(*(process(p) for p in packets), label="assessments")
        finally:
            if self.io.client:
                await self.io.client.raw.close()
        after = {k: vars(v) for k, v in usage.snapshot().items()}
        return NoveltyAssessmentResult(inputs=inputs, inputs_sha256=digest(inputs.model_dump()), candidates=results,
            calls=self.io.budget.snapshot(),
            usage={k: {m: n-before.get(k, {}).get(m, 0) for m, n in v.items()} for k, v in after.items()},
            run=dict(started=started, ended=datetime.now(timezone.utc).isoformat(), provider=self.io.provider,
                model=self.io.model, review_model=self.io.review_model or self.io.model,
                settings=self.settings.model_dump(), assessment_prompt=prompts.VERSION,
                review_prompt=prompts.REVIEW_VERSION, source_transport='page_grouped_source_map_v2', token_counter=TOKEN_COUNTER,
                reassessment_parent_sha256=digest(previous.model_dump()) if previous else None, review_observations=observations, generation_guidance=guidance,
                stopped_for_provider_error=self.io.fatal, scope='saved_retrieval_and_available_extracted_evidence'))
