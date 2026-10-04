"""Original-evidence comparison of all shortlisted target/paper pairs (section 12)."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os

from pydantic import BaseModel, Field
from directions.judge import validate_report
from directions.schemas import CorrectnessResponse, GenerationResult
from llm_client import errors, usage
from novelty import comparison_prompts as prompts
from novelty.comparison_schemas import (CandidateComparison, ComparisonDraft, ComparisonResponse,
    ComparisonReview, NoveltyComparisonResult, PaperComparison)
from novelty.pipeline import ModelOutputError, NoveltySearcher, Settings as SearchSettings, pointer, validate_signature
from novelty.schemas import FACETS, NoveltySearchResult
from opportunities.miner import digest
from reasoning.budget import TOKEN_COUNTER
from reasoning.schemas import ReviewSource


class Settings(BaseModel):
    concurrency: int = Field(default=4, ge=1, le=32)
    max_calls: int = Field(default=500, ge=0)
    max_input_tokens: int = Field(default_factory=lambda: int(os.getenv('NOVELTY_MAX_INPUT_TOKENS', '500000')), ge=1)
    max_output_tokens: int = Field(default_factory=lambda: int(os.getenv('NOVELTY_MAX_OUTPUT_TOKENS', '500000')), ge=1)


def page_numbers(root, pid, paper):
    """All locally extracted numeric markdown pages plus structured source references.

    Referenced pages can also be fetched by PaperStore's configured S3 fallback.
    No inferred full-PDF page count or appendix-completeness requirement.
    """
    local = {int(p.stem) for p in (Path(root)/pid).glob('*.md') if p.stem.isdigit() and int(p.stem) > 0}
    referenced = set()
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('source_locations', 'supporting_source_locations') and isinstance(value, list):
                    referenced.update(x['page'] for x in value if isinstance(x, dict) and type(x.get('page')) is int and x['page'] > 0)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(paper)
    return sorted(local | referenced), referenced


def validate_comparison(draft, payload):
    targets = {t['target_id']: t for t in payload['targets']}
    pages = {p['page_id']: p['text'] for p in payload['prior_pages']}
    if {p.target_id for p in draft.pairs} != set(targets):
        raise ValueError('comparison must cover exactly the requested targets')
    def quote(span):
        if span.page_id not in pages or ' '.join(span.quote.split()) not in ' '.join(pages[span.page_id].split()):
            raise ValueError('comparison quote missing or belongs to a different paper')
    for pair in draft.pairs:
        target = targets[pair.target_id]
        if pair.level != target['level']:
            raise ValueError('comparison target level changed')
        for dim in pair.dimensions:
            for path in dim.candidate_paths:
                if path.split('/')[1:2] not in [[key] for key in FACETS]:
                    raise ValueError('candidate pointers must reference semantic signature facets')
                pointer(target, path)
            for span in dim.source_spans:
                quote(span)
        for span in pair.hypothesis_evidence:
            quote(span)


class NoveltyComparator:
    """One run per instance. Reuses existing evidence/call infrastructure, no search."""
    def __init__(self, store, chat=None, *, review_chat=None, provider=None, model=None,
                 review_model=None, settings=None):
        self.settings = settings or Settings()
        self.io = NoveltySearcher(store, None, chat, review_chat=review_chat, provider=provider,
            model=model, review_model=review_model, settings=SearchSettings(**self.settings.model_dump(), max_queries=20))
        self.used = False

    async def _reviewed(self, payload, reviews):
        async def create(task, body):
            request = body
            for attempt in range(2):
                try:
                    response, _ = await self.io._call(task if attempt == 0 else 'repair_comparison',
                        prompts.COMPARE, ComparisonResponse, request)
                except ModelOutputError as exc:
                    detail, raw = str(exc), exc.raw
                else:
                    if response.comparison is None:
                        raise ValueError('comparison_abstained: ' + response.abstention_reason)
                    try:
                        validate_comparison(response.comparison, payload)
                    except (ValueError, KeyError, IndexError) as exc:
                        detail, raw = str(exc), response.model_dump()
                    else:
                        return response.comparison
                if attempt:
                    raise ValueError('invalid_comparison_after_repair: ' + detail)
                request = {**body, 'invalid_output': raw, 'validation_errors': detail,
                    'repair_instruction': 'Return complete corrected JSON with exact target IDs, target-relative pointers and verbatim prior-page quotes, or abstain. Do not invent evidence.'}
        draft = await create('compare_prior_work', {'payload': payload})
        for round in range(2):
            report, error, model = None, None, self.io.review_model or self.io.model
            try:
                report, model = await self.io._call('review_comparison', prompts.REVIEW, CorrectnessResponse,
                    {'payload': payload, 'comparison': draft.model_dump()})
                validate_report(report, draft, {'pages': payload['source_context']['pages'] + payload['prior_pages']})
            except Exception as exc:
                report, error = None, errors.describe(exc)
                raise
            finally:
                reviews.append(ComparisonReview(round=round, draft=draft.model_copy(deep=True),
                    model=model, report=report, error=error))
            if report.decision == 'pass':
                return draft
            if report.decision == 'abstain' or round == 1:
                raise ValueError('comparison_review_unresolved: ' + report.summary)
            draft = await create('revise_comparison', {'payload': payload,
                'comparison': draft.model_dump(), 'corrections': report.model_dump()})

    async def _paper(self, pid, targets, expected_hash, source_context):
        reviews, refs, missing = [], [], []
        try:
            if Path(pid).name != pid or pid in ('.', '..'):
                raise ValueError('unsafe prior paper ID')
            paper = await self.io._resource('get', pid)
            if paper.status != 'loaded' or paper.sha256 != expected_hash:
                raise ValueError('missing_or_changed_prior_paper: ' + pid)
            numbers, referenced = await asyncio.to_thread(page_numbers, self.io.store.root, pid, paper.paper)
            pages = await asyncio.gather(*(self.io._resource('page', pid, n) for n in numbers))
            missing = [n for n, p in zip(numbers, pages) if p is None and n in referenced]
            if any(p is None and n not in referenced for n, p in zip(numbers, pages)):
                raise ValueError('extracted page disappeared during loading')
            loaded = [p for p in pages if p is not None]
            if not loaded:
                raise ValueError('no_original_prior_pages: ' + pid)
            refs = [ReviewSource(paper_id=p.paper_id, page=p.page, key_or_path=p.key_or_path, sha256=p.sha256) for p in loaded]
            payload = {'source_context': source_context,
                'targets': [t.model_dump() for t in targets], 'prior_paper_id': pid,
                'prior_paper': paper.paper, 'prior_artifact_sha256': paper.sha256,
                'prior_pages': [{'page_id': f'{pid}#p{p.page}', 'text': p.text} for p in loaded],
                'missing_referenced_pages': missing, 'evidence_scope': 'available_extracted_pages'}
            draft = await self._reviewed(payload, reviews)
            return PaperComparison(paper_id=pid, target_ids=[t.target_id for t in targets], status='complete',
                expected_artifact_sha256=expected_hash, context_pages=refs, missing_referenced_pages=missing,
                comparison=draft, reviews=reviews, diagnostic=None)
        except Exception as exc:
            return PaperComparison(paper_id=pid, target_ids=[t.target_id for t in targets],
                status='unresolved' if reviews else 'failed', expected_artifact_sha256=expected_hash,
                context_pages=refs, missing_referenced_pages=missing, comparison=None,
                reviews=reviews, diagnostic=errors.describe(exc))

    async def run(self, generation: GenerationResult, search: NoveltySearchResult, *, paper_ids=None):
        if self.used:
            raise ValueError('create a new novelty comparator per run')
        self.used = True
        generation = GenerationResult.model_validate(generation.model_dump())
        search = NoveltySearchResult.model_validate(search.model_dump())
        expected_ref = {'schema_version': generation.schema_version, 'sha256': digest(generation.model_dump())}
        if search.directions_ref != expected_ref:
            raise ValueError('search references a different generation artifact')
        directions = {d.direction_id: d for d in generation.directions}
        if len(directions) != len(generation.directions) or sorted(directions) != sorted(c.direction_id for c in search.candidates):
            raise ValueError('search must cover each direction exactly once')
        # Validate every batch's immutable identity before scheduling any API work.
        for c in search.candidates:
            if c.candidate_sha256 != digest(directions[c.direction_id].model_dump()):
                raise ValueError('candidate snapshot changed since search')
            hashes = {}
            for retrieval in c.searches:
                for match in retrieval.ranked:
                    value = retrieval.paper_artifact_hashes[match.paper_id]
                    if match.paper_id in hashes and hashes[match.paper_id] != value:
                        raise ValueError('conflicting prior artifact hashes')
                    hashes[match.paper_id] = value
        known = {p.paper_id for c in search.candidates for s in c.searches for p in s.ranked}
        if paper_ids is not None and (not paper_ids or not set(paper_ids) <= known):
            raise ValueError('selected papers must be a nonempty subset of the saved shortlists')
        started = datetime.now(timezone.utc).isoformat()
        before = {k: vars(v).copy() for k, v in usage.snapshot().items()}

        async def process(candidate):
            direction = directions[candidate.direction_id]
            if candidate.candidate_sha256 != digest(direction.model_dump()):
                raise ValueError('candidate snapshot changed since search')
            if candidate.signature is None:
                return CandidateComparison(direction_id=direction.direction_id, candidate_sha256=candidate.candidate_sha256,
                    signature=None, shortlist={}, retrieval_status={}, papers=[], diagnostic=candidate.diagnostic)
            signature = candidate.signature
            by_target = {t.target_id: t for t in signature.targets}
            shortlist = {s.target_id: [p.paper_id for p in s.ranked] for s in candidate.searches}
            grouped, hashes = {}, {}
            for s in candidate.searches:
                for p in s.ranked:
                    h = s.paper_artifact_hashes[p.paper_id]
                    if p.paper_id in hashes and hashes[p.paper_id] != h:
                        raise ValueError('conflicting prior artifact hashes')
                    hashes[p.paper_id] = h
                    grouped.setdefault(p.paper_id, []).append(by_target[s.target_id])
            def withheld(pid, status, why):
                return PaperComparison(paper_id=pid, target_ids=[t.target_id for t in grouped[pid]],
                    status=status, expected_artifact_sha256=hashes[pid], context_pages=[],
                    missing_referenced_pages=[], comparison=None, reviews=[], diagnostic=why)
            try:
                extras = {}
                for ref in candidate.context_pages:
                    extras.setdefault(ref.paper_id, []).append(ref.page)
                payload, refs = await self.io._context(direction, extras)
                actual = {(r.paper_id, r.page): r.sha256 for r in refs}
                prior = {(r.paper_id, r.page): r.sha256 for r in candidate.context_pages}
                if len(prior) != len(candidate.context_pages) or actual != prior:
                    raise ValueError('signature context pages changed or omitted')
                validate_signature(signature, direction, payload, 20)
            except Exception as exc:
                diagnostic = errors.describe(exc)
                results = [withheld(pid, 'failed', diagnostic) for pid in sorted(grouped)]
            else:
                async def one(pid):
                    if paper_ids is not None and pid not in paper_ids:
                        return withheld(pid, 'skipped', 'Outside explicitly selected comparison scope; not assessed.')
                    return await self._paper(pid, grouped[pid], hashes[pid], payload)
                results = await asyncio.gather(*(one(pid) for pid in sorted(grouped)))
                diagnostic = None
            return CandidateComparison(direction_id=direction.direction_id, candidate_sha256=candidate.candidate_sha256,
                signature=signature, shortlist=shortlist, retrieval_status={s.target_id:s.status for s in candidate.searches},
                papers=results, diagnostic=diagnostic)
        try:
            results = await asyncio.gather(*(process(c) for c in search.candidates))
        finally:
            if self.io.client:
                await self.io.client.raw.close()
        after = {k: vars(v) for k, v in usage.snapshot().items()}
        return NoveltyComparisonResult(directions_ref=expected_ref,
            search_ref={'schema_version': search.schema_version, 'sha256': digest(search.model_dump())},
            candidates=results, calls=self.io.budget.snapshot(),
            usage={k: {m:n-before.get(k, {}).get(m, 0) for m,n in v.items()} for k,v in after.items()},
            run={'started':started, 'ended':datetime.now(timezone.utc).isoformat(),
                'provider':self.io.provider, 'model':self.io.model, 'review_model':self.io.review_model or self.io.model,
                'settings':self.settings.model_dump(), 'comparison_prompt':prompts.VERSION,
                'review_prompt':prompts.REVIEW_VERSION, 'token_counter':TOKEN_COUNTER,
                'selected_papers':sorted(paper_ids) if paper_ids is not None else None,
                'stopped_for_provider_error':self.io.fatal,
                'scope':'pairwise_overlap_on_available_extracted_evidence'})
