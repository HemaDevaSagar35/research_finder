"""Reviewed joint novelty assessment; prior evidence is never silently rewritten."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from llm_client import errors, usage
from novelty import assessment_prompts as prompts
from novelty.assessment_schemas import (AssessmentInputs, Invalidations, AssessmentDraft,
    AssessmentReport, AssessmentReview, CandidateAssessment, FormatRepair,
    NoveltyAssessmentResult)
from novelty.assessment_evidence import build_packet, model_packet, restore_response_sources, validate_draft, validate_report
from novelty.comparison import Settings
from novelty.pipeline import ModelOutputError, NoveltySearcher, Settings as CallSettings
from opportunities.miner import digest
from reasoning.budget import TOKEN_COUNTER


class NoveltyAssessor:
    """One run per instance. Joint targets within a direction; concurrent directions."""
    def __init__(self, chat=None, *, review_chat=None, provider=None, model=None,
                 review_model=None, settings=None):
        self.settings = settings or Settings()
        self.io = NoveltySearcher(None, None, chat, review_chat=review_chat, provider=provider,
            model=model, review_model=review_model, settings=CallSettings(**self.settings.model_dump()))
        self.used = False

    async def _validated_call(self, task, system, schema, body, validator, repairs):
        transport = {**body, 'packet': model_packet(body['packet'])}
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

    async def _candidate(self, packet):
        reviews, repairs, assessment, diagnostic = [], [], None, None
        try:
            if not packet['evidence']:
                raise ValueError('No accepted prior-work evidence; all original targets remain unresolved.')
            body = {'packet': packet}
            for round in range(2):
                draft, _ = await self._validated_call('assess_novelty' if round == 0 else 'revise_novelty_assessment',
                    prompts.ASSESS, AssessmentDraft, body, validate_draft, repairs)
                report = error = None
                model = self.io.review_model or self.io.model
                try:
                    # Fresh independent conversation; no author corrections/history.
                    report, model = await self._validated_call('review_novelty_assessment', prompts.REVIEW,
                        AssessmentReport, {'packet': packet, 'assessment': draft.model_dump()}, validate_report, repairs)
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
                body = {'packet': packet, 'previous_assessment': draft.model_dump(), 'corrections': report.model_dump()}
        except Exception as exc:
            diagnostic = errors.describe(exc)
        return CandidateAssessment(direction_id=packet['direction_id'], candidate_sha256=packet['candidate_sha256'],
            coverage=packet['coverage'], assessment=assessment, reviews=reviews,
            format_repairs=repairs, diagnostic=None if assessment else diagnostic)

    async def run(self, generation, search, comparisons, *, invalidations: Invalidations | None = None):
        if self.used:
            raise ValueError('create a new novelty assessor per run')
        self.used = True
        inputs = AssessmentInputs.model_validate(dict(generation=generation.model_dump(), search=search.model_dump(),
            comparisons=comparisons.model_dump(), invalidations=invalidations.model_dump() if invalidations else None))
        packets = [build_packet(inputs, d.direction_id) for d in inputs.generation.directions]
        started = datetime.now(timezone.utc).isoformat()
        before = {k: vars(v).copy() for k, v in usage.snapshot().items()}
        try:
            results = await asyncio.gather(*(self._candidate(p) for p in packets))
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
                review_prompt=prompts.REVIEW_VERSION, source_transport='page_grouped_source_aliases_v1', token_counter=TOKEN_COUNTER,
                stopped_for_provider_error=self.io.fatal, scope='saved_retrieval_and_available_extracted_evidence'))
