"""Bounded, independently auditable repairs before scientific refinement."""
from pydantic import Field, model_validator
from directions.schemas import Strict
from novelty.assessment import NoveltyAssessor, AssessmentSettings
from novelty.assessment_schemas import Invalidations, Invalidation
from novelty.reassessment import InterpretationConcern, InterpretationReassessor, ReassessmentResult
from opportunities.miner import digest
from critic.schemas import CriticResult


class RepairRound(Strict):
    parent_sha256: str
    evidence_repairs: list[ReassessmentResult]
    failures: list[str]
    critic: CriticResult


def source_concerns(current):
    rows=[]
    guidance={}
    for candidate in current.novelty.candidates:
        records=[candidate, *(candidate.hypothesis_assessments or [])]
        for record in records:
            for review in record.reviews[-1:]:
                if review.report:
                    for q in review.report.reassessment_requests:
                        passages=list(q.passage_ids)
                        if not passages and q.claim_ids:
                            comparison=next(c for c in current.novelty.inputs.comparisons.candidates if c.direction_id==candidate.direction_id)
                            paper=next((p for p in comparison.papers if p.paper_id==q.paper_id),None)
                            if paper and paper.evidence_reviews:
                                evidence=paper.evidence_reviews[-1].record
                                passages=sorted({p for claim in evidence.claims if claim.claim_id in q.claim_ids for p in claim.passage_ids})
                        rows.append(dict(direction_id=candidate.direction_id,paper_id=q.paper_id,target_ids=q.target_ids,passage_ids=passages,reason=q.reason))
        if candidate.assessment is None and candidate.reviews and candidate.reviews[-1].report:
            report=candidate.reviews[-1].report
            if report.decision=='revise' and not report.reassessment_requests:
                guidance.setdefault(candidate.direction_id,[]).append(report.model_dump_json())
    for candidate in current.candidates:
        for review in candidate.reviews[-1:]:
            for q in [*review.draft.upstream_requests, *(review.report.upstream_requests if review.report else [])]:
                if q.stage=='novelty_assessment':
                    guidance.setdefault(candidate.direction_id,[]).append(q.reason)
                elif q.stage=='novelty_comparison':
                    papers={p.split('#')[0] for p in q.passage_ids if '#' in p and not p.startswith('support:')}
                    for pid in papers:
                        rows.append(dict(direction_id=candidate.direction_id,paper_id=pid,target_ids=[q.target_id],passage_ids=[p for p in q.passage_ids if p.startswith(pid+'#')],reason=q.reason))
    grouped={}
    for row in rows:
        key=(row['direction_id'],row['paper_id'])
        target=grouped.setdefault(key,dict(direction_id=key[0],paper_id=key[1],target_ids=[],passage_ids=[],reason=''))
        target['target_ids']=sorted(set(target['target_ids'])|set(row['target_ids']))
        target['passage_ids']=sorted(set(target['passage_ids'])|set(row['passage_ids']))
        target['reason']+='\n'+row['reason']
    return [InterpretationConcern(**r) for r in grouped.values() if r['passage_ids']],guidance


async def repair_round(core,current):
    from critic.refinement_loop import BorrowedIO
    from critic.pipeline import ResearchCritic
    concerns,guidance=source_concerns(current)
    novelty=current.novelty
    comparisons=novelty.inputs.comparisons
    repairs,failures=[],[]
    invalid=list(novelty.inputs.invalidations.entries) if novelty.inputs.invalidations else []
    for concern in concerns:
        try:
            worker=InterpretationReassessor(core.io.store,comparisons,[concern.model_dump()],settings=core.settings)
            worker.io=BorrowedIO(core.io)
            result=await worker.reassess(novelty.inputs.generation,novelty.inputs.search)
            repairs.append(result)
            comparisons=result.merged
            invalid=[e for e in invalid if not ((e.direction_id,e.paper_id)==(concern.direction_id,concern.paper_id) and e.target_ids and set(e.target_ids)<=set(concern.target_ids) and not e.claim_ids)]
        except Exception as exc:
            failures.append(f'{concern.direction_id}/{concern.paper_id}: {exc}')
            invalid.append(Invalidation(direction_id=concern.direction_id,paper_id=concern.paper_id,target_ids=concern.target_ids,reason=concern.reason))
    invalidations=Invalidations(comparison_sha256=digest(comparisons.model_dump()),entries=invalid) if invalid else None
    assessor=NoveltyAssessor(settings=AssessmentSettings(**core.settings.model_dump(),comparison_coverage_threshold=novelty.inputs.comparison_coverage_threshold or 1.0))
    assessor.io=BorrowedIO(core.io)
    assessment=await assessor.run(novelty.inputs.generation,novelty.inputs.search,comparisons,invalidations=invalidations,recovery=novelty,guidance=guidance or None)
    critic=ResearchCritic(core.io.store,settings=core.settings)
    critic.io=BorrowedIO(core.io)
    reviewed=await critic.run(assessment,recovery=current)
    return RepairRound(parent_sha256=digest(current.model_dump()),evidence_repairs=repairs,failures=failures,critic=reviewed)


def validate_rounds(original,rounds):
    current=original
    for record in rounds:
        if record.parent_sha256!=digest(current.model_dump()): raise ValueError('repair round parent mismatch')
        old=current.novelty.inputs;new=record.critic.novelty.inputs
        if old.generation!=new.generation or old.search!=new.search: raise ValueError('repair dispatcher changed scientific proposal or retrieval')
        comparisons=old.comparisons
        for repair in record.evidence_repairs:
            if repair.parent!=comparisons: raise ValueError('evidence repair chain mismatch')
            comparisons=repair.merged
        if comparisons!=new.comparisons: raise ValueError('unreviewed evidence change in dispatcher')
        current=record.critic
    return current
