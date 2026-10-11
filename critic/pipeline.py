"""Async transport-independent critique with a fresh independent audit."""
from llm_client.progress import gather as progress_gather
import asyncio
from datetime import datetime, timezone
from critic import prompts
from critic.evidence import (packet_for, upstream_route, validate_draft, validate_review,
    portfolio_packet, validate_portfolio, normalize_review_paths)
from critic.schemas import (CritiqueDraft, ReviewReport, ReviewRecord, CandidateCritique,
    PortfolioDraft, PortfolioCritique, CriticResult)
from novelty.assessment_evidence import model_packet, restore_response_sources, map_source_refs, source_aliases
from novelty.assessment_schemas import FormatRepair, NoveltyAssessmentResult
from novelty.comparison import Settings
from novelty.pipeline import NoveltySearcher, Settings as CallSettings, ModelOutputError
from opportunities.miner import digest
from llm_client import errors, usage


def transport(packet):
    """Lossless grouping: repeated claims and support pages are sent once."""
    packed = model_packet(packet)
    for item in packed.get('candidates', [packed]):
        if 'candidate' in item:
            def paths(node, path=''):
                if isinstance(node, dict):
                    return [p for key, value in node.items() for p in paths(value, path + '/' + key)]
                if isinstance(node, list):
                    return [path] + [p for i, value in enumerate(node) for p in paths(value, path + '/' + str(i))]
                return [path]
            item['valid_proposal_paths'] = paths(item['candidate'])
        if 'support_context' in item:
            item['support_context']['pages'] = [dict(page_id=p['page_id'], paper_id=p['paper_id'],
                text_location='source_passages[' + p['page_id'] + ']') for p in item['support_context']['pages']]
        claims = {}
        for e in item.get('evidence', []):
            claims.setdefault(e['paper_id'], {})
            for c in e['claims']:
                prior = claims[e['paper_id']].get(c['claim_id'])
                if prior is not None and prior != c:
                    raise ValueError('conflicting repeated accepted claim')
                claims[e['paper_id']][c['claim_id']] = c
            e['claim_ids'] = [c['claim_id'] for c in e.pop('claims')]
        item['accepted_claims_by_paper'] = claims
    return packed


def preserve_unaffected(draft, previous, report):
    if not isinstance(draft, CritiqueDraft):
        return
    affected = {c.target_id for c in report.target_checks if c.defects}
    for issue in report.issues:
        parts = issue.field_path.split('/')
        if len(parts) > 2 and parts[1] == 'targets' and parts[2].isdigit():
            affected.add(previous.targets[int(parts[2])].target_id)
        elif len(parts) > 2 and parts[1] == 'revisions' and parts[2].isdigit():
            affected.add(previous.revisions[int(parts[2])].target_id)
        else:
            affected.update(t.target_id for t in previous.targets)
    old = {t.target_id: t for t in previous.targets}
    if any(t != old[t.target_id] for t in draft.targets if t.target_id not in affected):
        raise ValueError('revision changed an unaffected target')
    if [r for r in draft.revisions if r.target_id not in affected] != [r for r in previous.revisions if r.target_id not in affected]:
        raise ValueError('revision changed unaffected revision requests')


class ResearchCritic:
    """One instance per request; caller owns injected store and chat clients."""
    def __init__(self, store, chat=None, *, review_chat=None, provider=None, model=None,
                 review_model=None, settings=None):
        self.settings = settings or Settings()
        self.io = NoveltySearcher(store, None, chat, review_chat=review_chat, provider=provider,
            model=model, review_model=review_model, settings=CallSettings(**self.settings.model_dump()))
        self.used = False
        self.normalizations = []

    async def _call(self, task, system, schema, body, validate, repairs):
        aliases = source_aliases(body['packet'])
        request = {**map_source_refs(body, aliases), 'packet': transport(body['packet'])}
        for attempt in range(2):
            response = None
            try:
                response, model = await self.io._call(task if attempt == 0 else 'repair_' + task, system, schema, request)
                response = restore_response_sources(response, body['packet'])
                if schema is ReviewReport and 'candidate' in body['packet']:
                    response, changes = normalize_review_paths(response, CritiqueDraft.model_validate(body['draft']))
                    self.normalizations.extend(dict(task=task, direction_id=body['packet']['direction_id'], **c) for c in changes)
                validate(response)
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
            request = {**request, 'invalid_output': map_source_refs(raw, aliases), 'validation_errors': detail}

    async def _reviewed(self, packet, portfolio=False, previous=None):
        reviews, repairs, accepted, diagnostic = [], [], None, None
        schema = PortfolioDraft if portfolio else CritiqueDraft
        validate = validate_portfolio if portfolio else validate_draft
        task = 'critic_portfolio' if portfolio else 'research_critique'
        body = {'packet': packet}
        if previous is not None:
            body.update(previous_draft=previous.draft.model_dump(),corrections=previous.report.model_dump())
        try:
            for round in range(2):
                def check(draft):
                    validate(draft, packet)
                    if not round and previous is not None:
                        preserve_unaffected(draft,previous.draft,previous.report)
                    if round:
                        preserve_unaffected(draft, reviews[-1].draft, reviews[-1].report)
                scope_instruction = (' Scope override: output targets and target_checks ONLY for these IDs: ' + str(packet['review_target_ids']) + '. Other hypotheses and direction text are context only. Keep original proposal field paths. Test-link checks cover only these hypotheses.' if packet.get('review_target_ids') else '')
                draft, _ = await self._call(task if not round and previous is None else 'revise_' + task,
                    (prompts.PORTFOLIO if portfolio else prompts.CRITIQUE) + scope_instruction, schema, body, check, repairs)
                report = error = None
                model = self.io.review_model or self.io.model
                try:
                    report, model = await self._call('review_' + task,
                        (prompts.PORTFOLIO_REVIEW if portfolio else prompts.REVIEW) + scope_instruction + ' ReviewIssue.field_path must point into the submitted critique draft, never into your own test_link_checks. A defective proposal/test requires an upstream direction_correctness request with its original proposal field_paths.', ReviewReport,
                        {'packet': packet, 'draft': draft.model_dump()},
                        lambda r: validate_review(r, draft, packet, portfolio), repairs)
                except Exception as exc:
                    error = errors.describe(exc)
                    raise
                finally:
                    reviews.append(ReviewRecord(round=round, draft=draft, report=report, error=error, model=model))
                if (not portfolio and draft.upstream_requests) or report.upstream_requests:
                    diagnostic = 'Source defect requires upstream reassessment.'
                    break
                if report.decision == 'pass':
                    accepted = draft
                    break
                diagnostic = 'Independent critic review: ' + report.decision + ': ' + report.summary
                if report.decision == 'abstain':
                    break
                body = {'packet': packet, 'previous_draft': draft.model_dump(), 'corrections': report.model_dump()}
        except Exception as exc:
            diagnostic = errors.describe(exc)
        return accepted, reviews, repairs, None if accepted else diagnostic

    async def _candidate(self, novelty, direction, previous=None):
        blocked = upstream_route(novelty, direction)
        base = dict(direction_id=direction.direction_id, candidate_sha256=digest(direction.model_dump()),
                    packet=None, packet_sha256=None, critique=None, reviews=[], format_repairs=[])
        if blocked:
            return CandidateCritique(**base, diagnostic=blocked, blocked_at='upstream')
        try:
            support, _ = await self.io._context(direction, {})
            packet = packet_for(novelty, direction, {k: support[k] for k in ('paper_artifacts', 'pages')})
        except Exception as exc:
            return CandidateCritique(**base, diagnostic=errors.describe(exc), blocked_at='source_loading')
        seed=None
        if previous and previous.packet==packet and previous.reviews:
            last=previous.reviews[-1]
            if last.report and last.report.decision=='revise' and not any(r.draft.upstream_requests or (r.report and r.report.upstream_requests) for r in previous.reviews): seed=last
        critique, reviews, repairs, diagnostic = await self._reviewed(packet,previous=seed)
        return CandidateCritique(direction_id=direction.direction_id, candidate_sha256=base['candidate_sha256'],
            packet=packet, packet_sha256=digest(packet), critique=critique, reviews=reviews,
            format_repairs=repairs, diagnostic=diagnostic, blocked_at=None if critique else 'critique')

    async def run(self, novelty, *, recovery=None):
        with usage.scoped() as totals:
            result = await self._run(novelty, recovery)
            result.usage = {k: vars(v).copy() for k, v in totals.items()}
            return result

    async def _run(self, novelty, recovery=None):
        if self.used:
            raise ValueError('create a new critic per run')
        self.used = True
        novelty = NoveltyAssessmentResult.model_validate(novelty.model_dump())
        started = datetime.now(timezone.utc).isoformat()
        try:
            if recovery: recovery=CriticResult.model_validate(recovery.model_dump())
            async def one(d):
                old=next((c for c in recovery.candidates if c.direction_id==d.direction_id),None) if recovery else None
                if old and old.critique and old.packet and not upstream_route(novelty,d):
                    new_packet=packet_for(novelty,d,old.packet['support_context'])
                    if {k:v for k,v in old.packet.items() if k!='retrieval_context'}=={k:v for k,v in new_packet.items() if k!='retrieval_context'}:
                        return old.model_copy(update={'packet':new_packet,'packet_sha256':digest(new_packet)})
                return await self._candidate(novelty,d,old)
            results = await progress_gather(*(one(d) for d in novelty.inputs.generation.directions), label="critiques")
            packet = portfolio_packet(results)
            if len(packet['eligible_direction_ids']) < 2:
                assessment = PortfolioDraft(considered_direction_ids=packet['eligible_direction_ids'], merges=[],
                    rationale='Fewer than two eligible directions; no merge is possible.')
                reviews, repairs, diagnostic = [], [], None
            else:
                assessment, reviews, repairs, diagnostic = await self._reviewed(packet, True)
            portfolio = PortfolioCritique(packet=packet, packet_sha256=digest(packet), assessment=assessment,
                reviews=reviews, format_repairs=repairs, diagnostic=diagnostic)
        finally:
            if self.io.client:
                await self.io.client.raw.close()
        return CriticResult(novelty=novelty, novelty_sha256=digest(novelty.model_dump()), candidates=results,
            portfolio=portfolio, calls=self.io.budget.snapshot(), usage={},
            run=dict(started=started, ended=datetime.now(timezone.utc).isoformat(), provider=self.io.provider,
                model=self.io.model, review_model=self.io.review_model or self.io.model,
                settings=self.settings.model_dump(), prompt_version=prompts.VERSION,
                review_prompt_version=prompts.REVIEW_VERSION, review_path_normalizations=self.normalizations, source_transport='lossless_grouped_claims_and_page_maps_v2',
                scientific_validation='model_reviewed_not_empirically_validated'))
