"""Bounded critic → revision → novelty revalidation → fresh critic cycle."""
from llm_client.progress import gather as progress_gather
import asyncio
from datetime import datetime, timezone
from typing import Literal
from pydantic import Field, model_validator
from directions.schemas import Strict, Text, GenerationResult
from directions.judge import validate_report as validate_correctness_report
from directions.revision_schemas import DirectionRevision
from directions.revision import revise_direction, revised_generation, confirm_requests
from directions.subset import SubsetRevision, revise_subset
from novelty.revalidation import ReuseCertificate, assess_reuse, rebind_reused_inputs
from novelty.assessment import NoveltyAssessor, AssessmentSettings
from novelty.comparison import NoveltyComparator
from novelty.pipeline import NoveltySearcher
from novelty.schemas import NoveltySearchResult
from novelty.comparison_schemas import NoveltyComparisonResult
from critic.pipeline import ResearchCritic
from critic.schemas import CriticResult, UpstreamRequest
from critic.evidence import validate_request
from opportunities.miner import digest
from llm_client import usage
from critic.repair_routes import RepairRound, repair_round, validate_rounds


class Cycle(Strict):
    input_critic_sha256: Text
    revisions: list[DirectionRevision | SubsetRevision]
    certificates: list[ReuseCertificate]
    generation: GenerationResult | None
    critic: CriticResult | None
    fresh_search: NoveltySearchResult | None = None
    fresh_comparisons: NoveltyComparisonResult | None = None
    diagnostic: str | None


class RefinementResult(Strict):
    schema_version: Literal['refinement_loop_v1'] = 'refinement_loop_v1'
    original: CriticResult
    original_sha256: Text
    observations: dict[str,list[UpstreamRequest]] = Field(default_factory=dict)
    repair_rounds: list[RepairRound] | None = Field(default=None, max_length=2, exclude_if=lambda v: v is None)
    cycles: list[Cycle] = Field(max_length=2)
    status: Literal['ready', 'needs_revision', 'blocked']
    diagnostic: str | None
    calls: dict[str, int | None]
    usage: dict
    run: dict

    @model_validator(mode='after')
    def lineage(self):
        if self.original_sha256!=digest(self.original.model_dump()): raise ValueError('loop parent hash mismatch')
        validate_observations(self.original,self.observations)
        current=validate_rounds(self.original,self.repair_rounds or [])
        for index,cycle in enumerate(self.cycles):
            if index and self.cycles[index-1].critic is None: raise ValueError("unfinished cycle must be last")
            if cycle.input_critic_sha256!=digest(current.model_dump()): raise ValueError('cycle parent hash mismatch')
            old={d.direction_id:d for d in current.novelty.inputs.generation.directions}
            if len({r.original.direction_id for r in cycle.revisions})!=len(cycle.revisions): raise ValueError('duplicate revisions')
            for r in cycle.revisions:
                if r.original!=old.get(r.original.direction_id): raise ValueError('revision parent differs from current direction')
                candidate=next(c for c in current.candidates if c.direction_id==r.original.direction_id)
                if isinstance(r,SubsetRevision):
                    assessment=next(a for a in current.novelty.candidates if a.direction_id==r.original.direction_id)
                    retained=candidate.handoff().get('retained_hypothesis_ids') if candidate.critique else (assessment.selection_scope or (assessment.assessment.refinement.retained_hypothesis_ids if assessment.assessment and assessment.assessment.refinement.action=='narrow' else None))
                    if retained!=r.retained_hypothesis_ids: raise ValueError('subset differs from independently reviewed retained scope')
                    continue
                if r.accepted_refinements!=requests_for(candidate,r.original,current.novelty)[1]: raise ValueError('unpublished refinements cannot authorize edits')
                requests=requests_for(candidate,r.original,current.novelty)[0]
                if index==0: requests.extend(q.model_dump() for q in self.observations.get(r.original.direction_id,[]))
                if not requests and not r.accepted_refinements: raise ValueError('revision has no authorized request')
                if r.attempts: confirm_requests(r.preflight,requests)
                if r.preflight:
                    support=candidate.packet['support_context'] if candidate.packet else r.support_context
                    if support is None: raise ValueError('revision requires the original source packet')
                    payload={'pages':support['pages']}
                    validate_correctness_report(r.preflight,r.original.proposal,payload)
                    for attempt in r.attempts:
                        if attempt.review: validate_correctness_report(attempt.review,attempt.proposal,payload)
            if cycle.generation is not None and cycle.generation!=revised_generation(current.novelty.inputs.generation,cycle.revisions):
                raise ValueError('generation differs from reviewed revision records')
            if cycle.critic:
                if all(r.proposal is None for r in cycle.revisions): raise ValueError('unreviewed revision cannot advance')
                if cycle.generation!=cycle.critic.novelty.inputs.generation: raise ValueError('critic did not receive revised generation')
                s,c=rebind_reused_inputs(current.novelty,cycle.generation,cycle.certificates,cycle.fresh_search,cycle.fresh_comparisons)
                if s!=cycle.critic.novelty.inputs.search or c!=cycle.critic.novelty.inputs.comparisons:
                    raise ValueError('novelty work differs from certified parent or fresh run')
                current=cycle.critic
            elif not cycle.diagnostic: raise ValueError('unfinished cycle needs diagnostic')
        if self.status!='ready' and not self.diagnostic: raise ValueError('unfinished loop needs diagnostic')
        if self.status=='ready' and self.observations:
            completed={r.original.direction_id for r in self.cycles[0].revisions if r.proposal is not None} if self.cycles else set()
            if not set(self.observations)<=completed: raise ValueError('ready loop skipped manual observations')
        if self.status=='ready' and (self.diagnostic or not ready(current) or any(c.critic is None for c in self.cycles)):
            raise ValueError('ready loop cannot have unresolved routes')
        return self

    def handoff(self):
        current=next((c.critic for c in reversed(self.cycles) if c.critic),self.repair_rounds[-1].critic if self.repair_rounds else self.original)
        handoff=current.handoff()
        if self.status=='blocked':
            affected={r.original.direction_id for r in self.cycles[-1].revisions} if self.cycles and self.cycles[-1].critic is None else set(self.observations)
            for candidate in handoff['candidates']:
                if candidate['direction_id'] in affected:
                    candidate['previous_scientific_action']=candidate.get('scientific_action')
                    candidate.update(next_stage='resolve_upstream',scientific_action=None,refinement_diagnostic=self.diagnostic)
        return dict(status=self.status,diagnostic=self.diagnostic,**handoff)


def ready(result):
    routes=result.handoff()['candidates']
    return bool(routes) and all(r['next_stage'] in ('ranking','stop') for r in routes)


def validate_observations(original,observations):
    candidates={c.direction_id:c for c in original.candidates}
    for did,requests in observations.items():
        candidate=candidates.get(did)
        if candidate is None or candidate.packet is None or not requests:
            raise ValueError('observation requires an existing source-backed candidate')
        for request in requests:
            if request.stage!='direction_correctness' or not request.field_paths:
                raise ValueError('manual observation must locate a candidate correctness concern')
            validate_request(request,candidate.packet)


def requests_for(candidate, original=None, novelty=None):
    requests=[]
    for r in candidate.reviews:
        requests.extend(q.model_dump() for q in r.draft.upstream_requests)
        if r.report: requests.extend(q.model_dump() for q in r.report.upstream_requests)
    # Only published scientific refinements have authority to change a proposal.
    refinements=[r.model_dump() for r in candidate.critique.revisions] if candidate.critique else []
    if original is not None and candidate.packet and candidate.packet.get('selection_scope') is not None:
        def translate(path):
            parts=path.split('/')
            if len(parts)>2 and parts[1] in ('hypotheses','experiments') and parts[2].isdigit():
                key=parts[1];field='hypothesis_id' if key=='hypotheses' else 'experiment_id'
                item=candidate.packet['candidate'][key][int(parts[2])]
                parts[2]=str(next(i for i,x in enumerate(getattr(original.proposal,key)) if getattr(x,field)==item[field]))
            return '/'.join(parts)
        for q in requests: q['field_paths']=[translate(p) for p in q.get('field_paths',[])]
        for r in refinements: r['field_path']=translate(r['field_path'])
    if novelty is not None and candidate.blocked_at == 'upstream':
        assessment=next(a for a in novelty.candidates if a.direction_id==candidate.direction_id)
        if assessment.assessment:
            for edit in assessment.assessment.refinement.scientific_edits:
                refinements.append(dict(field_path=edit.field_path,reason=edit.reason,required_change='Apply the independently reviewed novelty refinement: '+str(edit.proposed_value)))
    dedup={digest(q):q for q in requests}
    return list(dedup.values()),refinements


class BorrowedIO:
    """Stage borrows shared calls/resources; only the outer loop closes its client."""
    client=None
    def __init__(self,io): self.shared=io
    def __getattr__(self,name): return getattr(self.shared,name)


class RefinementLoop:
    def __init__(self,store,chat=None,*,review_chat=None,provider=None,model=None,review_model=None,
                 settings=None,retriever=None, max_cycles=2, checkpoint=None, search_settings=None):
        if max_cycles not in (1,2): raise ValueError('max_cycles must be one or two')
        self.core=ResearchCritic(store,chat,review_chat=review_chat,provider=provider,model=model,
            review_model=review_model,settings=settings)
        self.io=self.core.io
        self.retriever=retriever
        self.search_settings=search_settings
        self.max_cycles=max_cycles
        self.checkpoint=checkpoint
        self.used=False

    async def save(self,stage,value):
        if self.checkpoint: await self.checkpoint(stage,value)

    async def _refresh(self,parent,generation,certificates):
        if self.retriever is None:
            raise ValueError('fresh_novelty_required: scientific scope/applicability changed; configure a retriever')
        ids={c.direction_id for c in certificates if c.decision=='recheck'}
        data=generation.model_dump()
        data['directions']=[d for d in data['directions'] if d['direction_id'] in ids]
        subset=GenerationResult.model_validate(data)
        searcher=NoveltySearcher(self.io.store,self.retriever,provider=self.io.provider,model=self.io.model,review_model=self.io.review_model,
            corpus_id=str(getattr(self.retriever,'path','configured-index')),retrieval_mode='caller_configured')
        # Share the actual call budget, concurrency limit and fatal-provider state.
        searcher._call=self.io._call
        searcher.settings=self.search_settings or self.io.settings
        searcher.budget=self.io.budget
        search=await searcher.run(subset)
        await self.save('fresh_search',search)
        comparator=NoveltyComparator(self.io.store,settings=self.core.settings)
        comparator.io=BorrowedIO(self.io)
        comp=await comparator.run(subset,search)
        await self.save('fresh_comparisons',comp)
        rebound=rebind_reused_inputs(parent,generation,certificates,search,comp)
        return *rebound,search,comp

    async def run(self,original,*,observations=None):
        if self.used: raise ValueError('create a new refinement loop per request')
        self.used=True
        original=CriticResult.model_validate(original.model_dump())
        observations={did:[UpstreamRequest.model_validate(r) for r in rows] for did,rows in (observations or {}).items()}
        validate_observations(original,observations)
        current=original
        cycles=[]
        repairs=[]
        diagnostic=None
        started=datetime.now(timezone.utc).isoformat()
        with usage.scoped() as totals:
            try:
                if current.novelty.inputs.assessment_contract == 'isolated_v1':
                    for index in range(self.max_cycles):
                        if ready(current): break
                        record=await repair_round(self.core,current)
                        repairs.append(record)
                        current=record.critic
                        await self.save(f'repair_{index}',record)
                for index in range(self.max_cycles):
                    if ready(current) and not (index==0 and observations): break
                    selected=[]
                    subsets=[]
                    blockers=[]
                    for candidate in current.candidates:
                        assessment=next(a for a in current.novelty.candidates if a.direction_id==candidate.direction_id)
                        direction=next(d for d in current.novelty.inputs.generation.directions if d.direction_id==candidate.direction_id)
                        if candidate.critique and candidate.handoff()['next_stage']=='narrow_direction_and_recheck_scope':
                            subsets.append((direction,candidate.handoff()['retained_hypothesis_ids']))
                            continue
                        if candidate.blocked_at=='source_loading' and assessment.selection_scope and 'retained_scope_requires_refinement' in str(candidate.diagnostic):
                            subsets.append((direction,assessment.selection_scope))
                            continue
                        if candidate.blocked_at=='upstream' and assessment.assessment and assessment.assessment.refinement.action=='narrow' and assessment.assessment.refinement.removed_hypothesis_ids:
                            subsets.append((direction,assessment.assessment.refinement.retained_hypothesis_ids))
                            continue
                        if candidate.blocked_at=='upstream':
                            assessment=next(a for a in current.novelty.candidates if a.direction_id==candidate.direction_id)
                            if assessment.assessment is None:
                                requests=[q for r in assessment.reviews if r.report for q in r.report.reassessment_requests]
                                owner='upstream_evidence_reassessment_required' if requests else 'novelty_assessment_review_unresolved'
                                blockers.append(owner+': '+candidate.direction_id+': '+str(assessment.diagnostic))
                                continue
                        direction=next(d for d in current.novelty.inputs.generation.directions if d.direction_id==candidate.direction_id)
                        requests,refinements=requests_for(candidate,direction,current.novelty)
                        if index==0: requests.extend(r.model_dump() for r in observations.get(candidate.direction_id,[]))
                        if any(q['stage']!='direction_correctness' for q in requests):
                            blockers.append('upstream_evidence_reassessment_required: '+candidate.direction_id)
                            continue
                        if requests or refinements:
                            direction=next(d for d in current.novelty.inputs.generation.directions if d.direction_id==candidate.direction_id)
                            selected.append((direction,requests,refinements))
                    if not selected and not subsets:
                        raise ValueError('; '.join(blockers) or 'no_revision_route: pending merge, execution failure or upstream work needs its own handler')
                    revisions=await progress_gather(*[revise_direction(self.io,*args) for args in selected], *[revise_subset(self.io,*args) for args in subsets], label="revisions")
                    cycle=Cycle(input_critic_sha256=digest(current.model_dump()),revisions=revisions,
                        certificates=[],generation=None,critic=None,diagnostic='Revision in progress.')
                    cycles.append(cycle)
                    await self.save(f'cycle_{index}_revisions',cycle)
                    if all(r.proposal is None for r in revisions):
                        cycle.diagnostic='Unresolved candidate correctness: '+'; '.join(r.diagnostic for r in revisions if r.diagnostic)
                        diagnostic=cycle.diagnostic
                        break
                    generation=revised_generation(current.novelty.inputs.generation,revisions)
                    cycle.generation=generation
                    await self.save(f'cycle_{index}_generation',generation)
                    certificates=await progress_gather(*(assess_reuse(self.core,current,generation,r) for r in revisions if r.proposal is not None), label="reuse_checks")
                    cycle.certificates=certificates
                    await self.save(f'cycle_{index}_applicability',cycle)
                    if all(c.decision=='reuse' for c in certificates):
                        search,comparisons=rebind_reused_inputs(current.novelty,generation,certificates)
                    else:
                        search,comparisons,cycle.fresh_search,cycle.fresh_comparisons=await self._refresh(current.novelty,generation,certificates)
                    assessor=NoveltyAssessor(settings=AssessmentSettings(**self.core.settings.model_dump(),comparison_coverage_threshold=current.novelty.inputs.comparison_coverage_threshold or 1.0) if current.novelty.inputs.assessment_contract else self.core.settings)
                    assessor.io=BorrowedIO(self.io)
                    assessment=await assessor.run(generation,search,comparisons,recovery=current.novelty)
                    await self.save(f'cycle_{index}_assessment',assessment)
                    critic=ResearchCritic(self.io.store,settings=self.core.settings)
                    critic.io=BorrowedIO(self.io)
                    reviewed=await critic.run(assessment,recovery=current)
                    for model,counts in reviewed.usage.items():
                        totals[model].add(usage.Usage(**counts))
                    cycle.critic=reviewed
                    cycle.diagnostic=None
                    await self.save(f'cycle_{index}_critic',reviewed)
                    current=reviewed
                status='blocked' if diagnostic else 'ready' if ready(current) else 'needs_revision'
                if status=='needs_revision': diagnostic='Bounded revision cycle completed; final handoff records remaining scientific work.'
            except Exception as exc:
                status,diagnostic='blocked',str(exc)
                if cycles and cycles[-1].critic is None: cycles[-1].diagnostic=diagnostic
            finally:
                if self.io.client: await self.io.client.raw.close()
        return RefinementResult(original=original,original_sha256=digest(original.model_dump()),observations=observations,cycles=cycles,repair_rounds=repairs or None,
            status=status,diagnostic=diagnostic,calls=self.io.budget.snapshot(),usage={k:vars(v).copy() for k,v in totals.items()},
            run=dict(started=started,ended=datetime.now(timezone.utc).isoformat(),max_cycles=self.max_cycles,
                settings=self.io.settings.model_dump(),scope='saved_corpus_with_explicit_revision_lineage'))
