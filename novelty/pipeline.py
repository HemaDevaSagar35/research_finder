"""Reviewed signatures and full-index retrieval; see docs/novelty_search.md."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from directions.judge import validate_report
from directions.schemas import CorrectnessResponse, GenerationResult
from extraction.research_extract import _parse_json
from llm_client import AsyncLLMClient, errors, usage
from opportunities.miner import digest
from reasoning.budget import AttemptBudget, estimate_tokens, TOKEN_COUNTER
from reasoning.evidence import PaperStore, candidates, paper_card
from reasoning.schemas import ReviewSource
from research.query_planner import QueryPlan
from research.retrieval import MultiQueryRetriever, RetrievalFilters
from novelty import prompts
from novelty.schemas import (FACETS, CandidateSearch, NoveltySearchResult,
    RankingResponse, SignatureResponse, SignatureReview, TargetRetrieval)


class ModelOutputError(ValueError):
    def __init__(self, detail, raw):
        super().__init__(detail)
        self.raw = raw


class Settings(BaseModel):
    concurrency: int = Field(default=100, ge=1, le=100)
    max_calls: int | None = Field(default=None, ge=0)
    max_queries: int = Field(default=4, ge=1, le=20)
    record_k: int = Field(default=50, ge=1)
    candidate_k: int = Field(default=100, ge=1)
    rerank_k: int = Field(default=20, ge=1)
    retrieval_timeout: float = Field(default=120, gt=0, allow_inf_nan=False)
    max_input_tokens: int = Field(default_factory=lambda: int(os.getenv('NOVELTY_MAX_INPUT_TOKENS', '500000')), ge=1)
    max_output_tokens: int = Field(default_factory=lambda: int(os.getenv('NOVELTY_MAX_OUTPUT_TOKENS', '500000')), ge=1)


def pointer(document, path):
    if not path.startswith('/'):
        raise ValueError('expected JSON Pointer into candidate proposal')
    node = document
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(node, list) and part.isdigit():
            node = node[int(part)]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise ValueError('invalid candidate pointer')
    return node


def normalize_proposal_paths(signature):
    """Accept the unambiguous payload /candidate/ wrapper; store proposal-relative paths.

    Only syntax changes, before structural validation and independent review.
    No generated claims, source references, queries or basis labels are edited.
    """
    signature = signature.model_copy(deep=True)
    for target in signature.targets:
        for key in FACETS:
            for facet in getattr(target, key):
                facet.proposal_paths = [p[len('/candidate'):] if p.startswith('/candidate/') else p
                                        for p in facet.proposal_paths]
    return signature


def validate_signature(signature, direction, payload, max_queries):
    expected = {direction.direction_id: 'direction',
                **{h.hypothesis_id: 'hypothesis' for h in direction.proposal.hypotheses}}
    actual = {t.target_id: t.level for t in signature.targets}
    if len(actual) != len(signature.targets) or actual != expected:
        raise ValueError('signature must cover the direction and every hypothesis exactly once')
    evidence = {e.evidence_id: e for e in direction.opportunity.evidence}
    pages = {p['page_id']: p for p in payload['pages']}
    quote_errors = []
    for target in signature.targets:
        queries = [' '.join(q.split()).casefold() for q in target.queries]
        if len(queries) > max_queries or len(set(queries)) != len(queries):
            raise ValueError('queries must be unique and within allowance')
        for key in FACETS:
            for facet in getattr(target, key):
                for path in facet.proposal_paths:
                    pointer(direction.proposal.model_dump(), path)
                if not set(facet.evidence_ids) <= set(evidence):
                    raise ValueError('unknown evidence ID in signature')
                for span in facet.source_spans:
                    page = pages.get(span.page_id)
                    if page is None or ' '.join(span.quote.split()) not in ' '.join(page['text'].split()):
                        quote_errors.append(f'{target.target_id}/{key}: quote absent from {span.page_id}: {span.quote!r}')
                if facet.basis == 'source_fact':
                    quoted_papers = {pages[s.page_id]['paper_id'] for s in facet.source_spans if s.page_id in pages}
                    if not {evidence[e].paper_id for e in facet.evidence_ids} <= quoted_papers:
                        quote_errors.append(f'{target.target_id}/{key}: source fact quotes must cover cited evidence papers')
    if quote_errors:
        raise ValueError('; '.join(quote_errors))


class NoveltySearcher:
    """One run per instance. Caller owns injected retriever, store and chat clients."""
    def __init__(self, store: PaperStore, retriever: MultiQueryRetriever, chat=None, *,
                 review_chat=None, provider=None, model=None, review_model=None,
                 settings=None, corpus_id='configured-index', retrieval_mode='caller_configured'):
        self.store, self.retriever, self.chat = store, retriever, chat
        self.review_chat = review_chat
        self.provider = provider or os.getenv('NOVELTY_PROVIDER') or os.getenv('PROVIDER')
        self.model = model or os.getenv('NOVELTY_MODEL')
        self.review_model = review_model or os.getenv('NOVELTY_REVIEW_MODEL')
        self.settings = settings or Settings()
        self.corpus_id, self.retrieval_mode = corpus_id, retrieval_mode
        self.sem = asyncio.Semaphore(self.settings.concurrency)
        self.budget = AttemptBudget(self.settings.max_calls)
        self.locks, self.client, self.fatal, self.used = {}, None, None, False

    async def _resource(self, kind, *args):
        async with self.locks.setdefault((kind, *args), asyncio.Lock()):
            return await asyncio.to_thread(getattr(self.store, kind), *args)

    async def _call(self, task, system, schema, body):
        messages = [{'role': 'system', 'content': system + '\nReturn a JSON object matching the supplied schema.'}, {'role': 'user', 'content': json.dumps(
            {'task': task, 'schema': schema.model_json_schema(), **body},
            ensure_ascii=False, separators=(',', ':'))}]
        # Estimate actual message text, not a second JSON encoding that adds
        # escapes to quotes/newlines. Allow 32 tokens per message for framing;
        # the provider's own context limit remains independent of this estimate.
        input_tokens = sum(estimate_tokens(m['content']) + 32 for m in messages)
        if input_tokens > self.settings.max_input_tokens:
            raise ValueError('input_budget: complete context exceeds allowance; no silent omission')
        async with self.sem:
            if self.fatal:
                raise RuntimeError('provider_error: stopped after provider rejection')
            if not await self.budget.reserve(task):
                raise RuntimeError('call_budget: attempt allowance exhausted')
            try:
                if self.chat is None:
                    self.client = AsyncLLMClient(self.provider, concurrency=self.settings.concurrency, max_retries=0)
                    self.provider = self.client.provider
                    self.model = self.model or self.client.default_model
                    self.chat = self.client.chat_result
                review = task in ('review_signature', 'review_comparison', 'repair_comparison_review', 'review_comparison_evidence', 'repair_evidence_review', 'review_novelty_assessment', 'repair_review_novelty_assessment',
                    'review_research_critique', 'repair_review_research_critique',
                    'review_critic_portfolio', 'repair_review_critic_portfolio',
                    'review_revision_input', 'repair_review_revision_input',
                    'review_direction_revision', 'repair_review_direction_revision',
                    'review_novelty_reuse', 'repair_review_novelty_reuse')
                call = (self.review_chat or self.chat) if review else self.chat
                model = (self.review_model or self.model) if review else self.model
                result = await call(messages=messages, model=model,
                    max_tokens=self.settings.max_output_tokens, response_format={'type': 'json_object'})
                try:
                    if result.truncated:
                        raise ValueError('truncated model response')
                    parsed = schema.model_validate(_parse_json(result.text))
                except (ValueError, TypeError) as exc:
                    raise ModelOutputError(str(exc), result.text) from exc
                return parsed, result.model
            except Exception as exc:
                if errors.is_fatal(exc):
                    self.fatal = errors.describe(exc)
                raise

    async def _context(self, direction, extra_pages):
        op = direction.opportunity
        ids = sorted(direction.paper_artifact_hashes)
        if any(Path(pid).name != pid or pid in ('.', '..') for pid in ids):
            raise ValueError('unsafe paper ID')
        if not set(op.paper_ids) <= set(ids):
            raise ValueError('opportunity paper missing from artifact manifest')
        if direction.opportunity_id != op.opportunity_id:
            raise ValueError('opportunity snapshot mismatch')
        prior = {(p.paper_id, p.page): p.sha256 for p in direction.context_pages}
        if len(prior) != len(direction.context_pages) or not prior:
            raise ValueError('missing or duplicate context pages')
        if any(prior.get((p.paper_id, p.page)) != p.sha256 for p in op.review_sources):
            raise ValueError('direction drops reviewed opportunity context')
        if any(pid not in ids for pid, _ in prior):
            raise ValueError('context page outside artifact manifest')
        loaded = await asyncio.gather(*(self._resource('get', pid) for pid in ids))
        papers = dict(zip(ids, loaded))
        for pid, paper in papers.items():
            if paper.status != 'loaded' or paper.sha256 != direction.paper_artifact_hashes[pid]:
                raise ValueError(f'missing_or_changed_paper: {pid}')
        rows = {pid: {r.value_path: r for r in candidates(p.paper, pid)} for pid, p in papers.items()}
        for ev in op.evidence:
            row = rows.get(ev.paper_id, {}).get(ev.value_path)
            if (row is None or ev.artifact_sha256 != papers[ev.paper_id].sha256 or
                not ev.source_value or not row.source_value.startswith(ev.source_value) or
                row.provenance_path != ev.provenance_path or
                row.source_locations != [s.model_dump() for s in ev.source_locations]):
                raise ValueError('evidence no longer matches original artifact')
            if any((ev.paper_id, s.page) not in prior for s in ev.source_locations):
                raise ValueError('cited evidence page missing from context')
        keys = set(prior)
        # Extra available pages may clarify source details; absent pages fail explicitly.
        for pid, numbers in extra_pages.items():
            if pid not in ids:
                continue  # Extras for other directions in this same run.
            keys.update((pid, n) for n in numbers)
        keys = sorted(keys)
        pages = await asyncio.gather(*(self._resource('page', *key) for key in keys))
        for key, page in zip(keys, pages):
            if page is None or (key in prior and page.sha256 != prior[key]):
                raise ValueError(f'missing_or_changed_page: {key}')
        payload = {'candidate': direction.proposal.model_dump(), 'direction_id': direction.direction_id,
            'opportunity': op.model_dump(),
            'paper_artifacts': [{'paper_id': pid, 'sha256': papers[pid].sha256, 'paper': papers[pid].paper} for pid in ids],
            'pages': [{'page_id': f'{p.paper_id}#p{p.page}', 'paper_id': p.paper_id, 'text': p.text} for p in pages],
            'max_queries': self.settings.max_queries}
        refs = [ReviewSource(paper_id=p.paper_id, page=p.page, key_or_path=p.key_or_path, sha256=p.sha256) for p in pages]
        return payload, refs

    async def _signature(self, direction, payload, reviews, previous=None):
        async def create(task, body):
            # One format/reference repair per creation or substantive revision.
            # Provider failures, abstentions and input/call budgets are not repaired.
            request = body
            for attempt in range(2):
                try:
                    response, _ = await self._call(task if attempt == 0 else 'repair_signature',
                        prompts.SIGNATURE, SignatureResponse, request)
                except ModelOutputError as exc:
                    detail, raw = str(exc), exc.raw
                else:
                    if response.signature is None:
                        return response
                    normalized = normalize_proposal_paths(response.signature)
                    try:
                        validate_signature(normalized, direction, payload, self.settings.max_queries)
                    except (ValueError, KeyError, IndexError) as exc:
                        detail, raw = str(exc), response.model_dump()
                    else:
                        return response.model_copy(update={'signature': normalized})
                if attempt == 1:
                    raise ValueError('invalid_signature_after_repair: ' + detail)
                request = {**body, 'invalid_output': raw, 'validation_errors': detail,
                    'repair_instruction': 'Correct schema, candidate pointers, evidence references and quotations. Preserve all targets, predictions and uncertainty. Quotes must be contiguous verbatim original-page text, not paraphrases. Return a complete signature or abstain.'}
            raise AssertionError('unreachable')

        if previous is None:
            response = await create('create_signature', {'payload': payload})
        else:
            response = await create('revise_signature', {'payload': payload,
                'signature': previous.signature.model_dump(), 'corrections': previous.report.model_dump()})
        for round in range(2):
            if response.signature is None:
                raise ValueError('signature_abstained: ' + response.abstention_reason)
            signature = normalize_proposal_paths(response.signature)
            validate_signature(signature, direction, payload, self.settings.max_queries)
            report, error, model = None, None, self.review_model or self.model
            try:
                proposed, model = await self._call('review_signature', prompts.REVIEW, CorrectnessResponse,
                    {'payload': payload, 'signature': signature.model_dump()})
                validate_report(proposed, signature, payload)
                report = proposed
            except Exception as exc:
                error = errors.describe(exc)
                raise
            finally:
                reviews.append(SignatureReview(round=round, signature=signature.model_copy(deep=True),
                    model=model, report=report, error=error))
            if report.decision == 'pass':
                return signature
            if report.decision == 'abstain' or round == 1:
                raise ValueError('signature_review_unresolved: ' + report.summary)
            response = await create('revise_signature',
                {'payload': payload, 'signature': signature.model_dump(), 'corrections': report.model_dump()})
        raise AssertionError('unreachable')

    async def _search(self, target):
        retrieval, ranked, hashes, unavailable = None, [], {}, []
        try:
            retrieval = await self.retriever.retrieve(QueryPlan(queries=target.queries),
                record_k=self.settings.record_k, paper_k=self.settings.candidate_k,
                filters=RetrievalFilters(), timeout=self.settings.retrieval_timeout)
            ids = [p.paper_id for p in retrieval.papers]
            if any(Path(pid).name != pid or pid in ('.', '..') for pid in ids):
                raise ValueError('unsafe retrieved paper ID')
            papers = await asyncio.gather(*(self._resource('get', pid) for pid in ids))
            cards = []
            for hit, paper in zip(retrieval.papers, papers):
                if paper.status != 'loaded':
                    unavailable.append(hit.paper_id)
                    continue
                hashes[hit.paper_id] = paper.sha256
                cards.append({'paper': paper_card(hit.paper_id, paper.paper), 'matches': hit.model_dump()})
            if cards:
                keep = min(self.settings.rerank_k, len(cards))
                response, _ = await self._call('rerank', prompts.RANK, RankingResponse,
                    {'target': target.model_dump(), 'candidates': cards, 'keep': keep})
                chosen = [p.paper_id for p in response.papers]
                if len(chosen) != keep or len(set(chosen)) != keep or not set(chosen) <= set(hashes):
                    raise ValueError('reranker must return exactly keep unique loaded paper IDs')
                ranked = response.papers
            return TargetRetrieval(target_id=target.target_id, status='partial' if unavailable else 'complete',
                retrieval=retrieval, ranked=ranked, paper_artifact_hashes=hashes, unavailable_papers=unavailable,
                diagnostic='Some retrieved paper artifacts are unavailable; ranking is incomplete.' if unavailable else None)
        except Exception as exc:
            return TargetRetrieval(target_id=target.target_id, status='failed', retrieval=retrieval,
                ranked=[], paper_artifact_hashes=hashes, unavailable_papers=unavailable, diagnostic=errors.describe(exc))

    async def run(self, generation: GenerationResult, *, extra_pages=None, signature_recovery=None):
        if self.used:
            raise ValueError('create a new novelty searcher per run')
        self.used = True
        generation = GenerationResult.model_validate(generation.model_dump())
        ids = [d.direction_id for d in generation.directions]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate direction IDs')
        recoveries = {}
        if signature_recovery is not None:
            signature_recovery = NoveltySearchResult.model_validate(signature_recovery.model_dump())
            if signature_recovery.directions_ref.get('sha256') != digest(generation.model_dump()):
                raise ValueError('signature recovery generation changed')
            recoveries = {c.direction_id: c for c in signature_recovery.candidates}
            if set(recoveries) != set(ids):
                raise ValueError('signature recovery candidate scope changed')
            for c in recoveries.values():
                if c.signature is not None or not c.reviews or c.reviews[-1].report is None or c.reviews[-1].report.decision != 'revise':
                    raise ValueError('signature recovery requires a withheld signature with revision feedback')
        extras = extra_pages or {}
        known = {pid for d in generation.directions for pid in d.paper_artifact_hashes}
        if not set(extras) <= known or any(type(n) is not int or n < 1 for ns in extras.values() for n in ns):
            raise ValueError('extra pages require known source paper IDs and positive page numbers')
        started = datetime.now(timezone.utc).isoformat()
        before = {k: vars(v).copy() for k, v in usage.snapshot().items()}

        async def process(direction):
            reviews, refs = [], []
            try:
                payload, refs = await self._context(direction, extras)
                previous = recoveries.get(direction.direction_id)
                if previous is not None and (previous.context_pages != refs or previous.candidate_sha256 != digest(direction.model_dump())):
                    raise ValueError('signature recovery candidate or source context changed')
                signature = await self._signature(direction, payload, reviews,
                    previous.reviews[-1] if previous else None)
                searches = await asyncio.gather(*(self._search(t) for t in signature.targets))
                return CandidateSearch(direction_id=direction.direction_id, candidate_sha256=digest(direction.model_dump()),
                    context_pages=refs, signature=signature, reviews=reviews, searches=searches, diagnostic=None)
            except Exception as exc:
                return CandidateSearch(direction_id=direction.direction_id, candidate_sha256=digest(direction.model_dump()),
                    context_pages=refs, signature=None, reviews=reviews, searches=[], diagnostic=errors.describe(exc))

        try:
            results = await asyncio.gather(*(process(d) for d in generation.directions))
        finally:
            if self.client:
                await self.client.raw.close()
        after = {k: vars(v) for k, v in usage.snapshot().items()}
        return NoveltySearchResult(directions_ref={'schema_version': generation.schema_version,
            'sha256': digest(generation.model_dump())}, candidates=results, calls=self.budget.snapshot(),
            usage={k: {m: n-before.get(k, {}).get(m, 0) for m, n in v.items()} for k, v in after.items()},
            run={'started': started, 'ended': datetime.now(timezone.utc).isoformat(),
                 'provider': self.provider, 'model': self.model, 'review_model': self.review_model or self.model,
                 'signature_prompt': prompts.SIGNATURE_VERSION, 'review_prompt': prompts.REVIEW_VERSION,
                 'rank_prompt': prompts.RANK_VERSION, 'settings': self.settings.model_dump(),
                 'corpus_id': self.corpus_id, 'corpus_scope': 'full_configured_index',
                 'retrieval_mode': self.retrieval_mode,
                 'token_counter': TOKEN_COUNTER, 'stopped_for_provider_error': self.fatal,
                 'signature_recovery_parent_sha256': digest(signature_recovery.model_dump()) if signature_recovery else None})
