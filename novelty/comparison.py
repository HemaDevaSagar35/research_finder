"""Original-evidence comparison of all shortlisted target/paper pairs (section 12)."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import os

from pydantic import BaseModel, Field
from directions.schemas import GenerationResult
from llm_client import errors, usage
from novelty import comparison_prompts as prompts
from novelty.comparison_schemas import (CandidateComparison, ComparisonDraft, ComparisonResponse,
    ComparisonReview, NoveltyComparisonResult, PaperComparison, ReviewFormatRepair, ComparisonReport, Passage)
from novelty.comparison_workflow import EvidenceBuilder, eligible, assemble, accepted_targets, project, proposal_requirements
from novelty.comparison_repair import InterpretationPatch, apply_interpretation_patch, affected_pair_ids, review_addresses
from novelty.pipeline import ModelOutputError, NoveltySearcher, Settings as SearchSettings, validate_signature
from novelty.comparison_evidence import passages_for_pages, table_context_refs, candidate_view, validate_comparison, validate_report
from novelty.schemas import NoveltySearchResult
from opportunities.miner import digest
from reasoning.budget import TOKEN_COUNTER
from reasoning.schemas import ReviewSource


class Settings(BaseModel):
    concurrency: int = Field(default=100, ge=1, le=100)
    max_calls: int | None = Field(default=None, ge=0)
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



class NoveltyComparator:
    """One run per instance. Reuses existing evidence/call infrastructure, no search."""
    def __init__(self, store, chat=None, *, review_chat=None, provider=None, model=None,
                 review_model=None, settings=None):
        self.settings = settings or Settings()
        self.io = NoveltySearcher(store, None, chat, review_chat=review_chat, provider=provider,
            model=model, review_model=review_model, settings=SearchSettings(**self.settings.model_dump(), max_queries=20))
        self.used = False

    async def _reviewed(self, payload, targets, passages, reviews, evidence_history, requests, *, seed=None, prior_draft=None, initial_corrections=None):
        builder=EvidenceBuilder(self.io,prompts)
        evidence=seed if seed is not None else await builder.initial(payload,targets,passages,evidence_history)
        previous,corrections=prior_draft,initial_corrections
        for round in range(2):
            ids,claims=eligible(evidence)
            if not ids:return None
            body={'payload':payload,'eligible_target_ids':sorted(ids),
                'reviewed_evidence':{'claims':[c.model_dump() for c in claims],
                    'relationships':[r.model_dump() for r in evidence.record.relationships if r.target_id in ids]}}
            affected = affected_pair_ids(previous, ComparisonReport.model_validate(corrections)) if previous is not None else set()
            if previous is not None:body.update(comparison=previous.model_dump(),corrections=corrections,affected_target_ids=sorted(affected))
            request=body
            for attempt in range(2):
                try:
                    sparse = previous is not None
                    response,_=await self.io._call(('patch_comparison' if sparse else 'compare_prior_work') if attempt==0 else 'repair_comparison',
                        prompts.PATCH_INTERPRETATION if sparse else prompts.COMPARE,
                        InterpretationPatch if sparse else ComparisonResponse,request)
                    if sparse:
                        response=apply_interpretation_patch(previous,response,affected)
                    if response.abstention_reason:raise ValueError('comparison_abstained: '+response.abstention_reason)
                    if response.evidence_requests:
                        for r in response.evidence_requests:
                            if not set(r.target_ids)<=ids or not set(r.passage_ids)<={p.passage_id for p in passages}:
                                raise ValueError('invalid evidence reopen references')
                        draft=None
                    else:
                        draft=assemble(response,evidence,targets)
                        validate_comparison(draft,{**payload,'targets':[t.model_dump() for t in targets if t.target_id in ids]})
                    break
                except ModelOutputError as exc:detail,raw=str(exc),exc.raw
                except ValueError as exc:
                    if str(exc).startswith('comparison_abstained:'):raise
                    detail,raw=str(exc),response.model_dump()
                if attempt:raise ValueError('invalid_comparison_after_repair: '+detail)
                request={**body,'invalid_output':raw,'validation_errors':detail}
            if draft is None:
                requests.extend(response.evidence_requests)
                if round: return None
                evidence=await builder.reopen(payload,targets,passages,evidence_history,
                    {'evidence_requests':[r.model_dump() for r in response.evidence_requests]})
                continue
            report=error=None;model=self.io.review_model or self.io.model;repairs=[]
            base={'payload':payload,'eligible_target_ids':sorted(ids),
                'withheld_target_ids':sorted({t.target_id for t in targets}-ids),
                'relationships':[r.model_dump() for r in evidence.record.relationships if r.target_id in ids],
                'comparison':draft.model_dump(),'valid_review_fields':review_addresses(draft)};request=base
            try:
                for attempt in range(2):
                    try:
                        report,model=await self.io._call('review_comparison' if attempt==0 else 'repair_comparison_review',
                            prompts.REVIEW,ComparisonReport,request)
                        validate_report(report,draft,passages)
                        break
                    except ModelOutputError as exc:detail,raw=str(exc),exc.raw
                    except ValueError as exc:detail,raw=str(exc),report.model_dump()
                    report=None
                    if attempt:raise ValueError('invalid_comparison_review_after_repair: '+detail)
                    repairs.append(ReviewFormatRepair(invalid_output=raw,validation_error=detail,model=model))
                    request={**base,'invalid_review':raw,'validation_errors':detail}
            except Exception as exc:
                error=errors.describe(exc)
                raise
            finally:
                reviews.append(ComparisonReview(round=round,evidence_version=evidence.version,draft=draft.model_copy(deep=True),
                    format_repairs=repairs,model=model,report=report,error=error))
            if report.decision=='pass' or round==1 or report.decision=='abstain':
                return project(draft,accepted_targets(report,draft,evidence))
            previous,corrections=draft,report.model_dump()
            claim_issues = [i for i in report.issues if i.field_path.startswith('/claims/')]
            if claim_issues:
                corrections['disputed_claim_ids'] = sorted({draft.claims[int(i.field_path.split('/')[2])].claim_id for i in claim_issues})
                evidence=await builder.reopen(payload,targets,passages,evidence_history,corrections)
                previous=corrections=None  # regenerate from changed evidence, no stale interpretation
        return None

    async def _paper(self, pid, targets, expected_hash, source_context):
        reviews, refs, missing, passages, evidence_history, requests = [], [], [], [], [], []
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
            passages = passages_for_pages([{'page_id':f'{pid}#p{p.page}', 'sha256':p.sha256, 'text':p.text} for p in loaded])
            if not passages:
                raise ValueError('no_original_prior_text: ' + pid)
            prior_page_ids={p.page_id for p in passages}
            source_pages=[p for p in source_context['pages'] if p['page_id'] not in prior_page_ids]
            payload={'targets':[t.model_dump() for t in targets], 'prior_paper_id':pid,
                'proposal_requirements':{t.target_id:proposal_requirements(t) for t in targets},
                'prior_artifact_sha256':paper.sha256,
                'paper_navigation':{'title':paper.paper.get('title'), 'available_pages':[p.page for p in loaded],
                    'structured_referenced_pages':sorted(referenced)},
                'candidate_source_passages':[p.model_dump() for p in passages_for_pages(source_pages)],
                'prior_passages':[p.model_dump() for p in passages],
                'table_context_refs':table_context_refs(passages),
                'missing_referenced_pages':missing,'evidence_scope':'available_extracted_pages'}
            draft = await self._reviewed(payload,targets,passages,reviews,evidence_history,requests)
            status='complete' if draft and len(draft.pairs)==len(targets) else 'partial' if draft else 'unresolved'
            diagnostic=None if status=='complete' else 'Some target comparisons remain unresolved; inspect per-target outcomes and versioned evidence/reviews.'
            return PaperComparison(paper_id=pid, target_ids=[t.target_id for t in targets], targets=targets, passages=passages, status=status,
                expected_artifact_sha256=expected_hash, context_pages=refs, missing_referenced_pages=missing,
                comparison=draft, reviews=reviews,evidence_reviews=evidence_history,evidence_requests=requests,diagnostic=diagnostic)
        except Exception as exc:
            retained=None
            if reviews and evidence_history and reviews[-1].evidence_version==evidence_history[-1].version:
                retained=project(reviews[-1].draft,accepted_targets(reviews[-1].report,reviews[-1].draft,evidence_history[-1]))
            return PaperComparison(paper_id=pid, target_ids=[t.target_id for t in targets], targets=targets, passages=passages,
                status='partial' if retained else 'unresolved' if reviews or evidence_history else 'failed', expected_artifact_sha256=expected_hash,
                context_pages=refs, missing_referenced_pages=missing, comparison=retained,
                reviews=reviews,evidence_reviews=evidence_history,evidence_requests=requests, diagnostic=errors.describe(exc))

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
                return PaperComparison(paper_id=pid, target_ids=[t.target_id for t in grouped[pid]], targets=grouped[pid], passages=[],
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
                'review_prompt':prompts.REVIEW_VERSION, 'evidence_prompt':prompts.EVIDENCE_VERSION, 'token_counter':TOKEN_COUNTER,
                'selected_papers':sorted(paper_ids) if paper_ids is not None else None,
                'stopped_for_provider_error':self.io.fatal,
                'repair_policy':{'evidence_corrections_per_phase':3,'comparison_corrections':1,'repeated_record_stop':True},
                'scope':'pairwise_overlap_on_available_extracted_evidence'})
