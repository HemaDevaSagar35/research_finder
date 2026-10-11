"""Source-reviewed reopening of an accepted interpretation, preserving other work."""
from pydantic import Field, model_validator
from directions.schemas import Strict, Text
from directions.revision import checked_call
from novelty.comparison import NoveltyComparator
from novelty.comparison_repair import EvidencePatch, apply_evidence_patch, attach_review_citations
from novelty.comparison_workflow import EvidenceBuilder, eligible, validate_record, scope_citation_gaps
from novelty.comparison_schemas import NoveltyComparisonResult, ComparisonReport
from novelty.comparison_records import EvidenceResponse
from novelty import comparison_prompts as prompts
from opportunities.miner import digest


class InterpretationConcern(Strict):
    direction_id: Text
    paper_id: Text
    target_ids: list[Text] = Field(min_length=1)
    passage_ids: list[Text] = Field(min_length=1)
    reason: Text


def merge_reassessment(parent, fresh, concerns):
    """Only named papers/targets may change; claim text stays exact; audited citations may be added."""
    if parent.directions_ref!=fresh.directions_ref or parent.search_ref!=fresh.search_ref:
        raise ValueError('reassessment input lineage changed')
    scope={(c.direction_id,c.paper_id):c for c in concerns}
    if len(scope)!=len(concerns): raise ValueError('duplicate paper concerns')
    data=parent.model_dump()
    found=set()
    for candidate in data['candidates']:
        old=next(c for c in parent.candidates if c.direction_id==candidate['direction_id'])
        new=next(c for c in fresh.candidates if c.direction_id==candidate['direction_id'])
        for i,paper in enumerate(old.papers):
            concern=scope.get((old.direction_id,paper.paper_id))
            if concern is None: continue
            found.add((old.direction_id,paper.paper_id))
            replacement=next(p for p in new.papers if p.paper_id==paper.paper_id)
            if replacement.status!='complete': raise ValueError('reassessment not independently complete: '+str(replacement.diagnostic))
            if (replacement.context_pages!=paper.context_pages or replacement.passages!=paper.passages
                    or replacement.expected_artifact_sha256!=paper.expected_artifact_sha256):
                raise ValueError('original source changed during reassessment')
            before=paper.evidence_reviews[-1].record;after=replacement.evidence_reviews[-1].record
            expected=before
            for review in replacement.evidence_reviews[:-1]:
                expected=attach_review_citations(expected,review.report,replacement.passages)
            if expected.claims!=after.claims: raise ValueError('interpretation repair changed source claims beyond audited citation attachment')
            affected=set(concern.target_ids)
            if before.relationships==after.relationships and replacement.comparison.pairs==paper.comparison.pairs:
                raise ValueError('reassessment made no correction to the disputed interpretation')
            if not affected<=set(paper.target_ids) or len(affected)!=len(concern.target_ids): raise ValueError('unknown or duplicate repair target')
            if [r for r in before.relationships if r.target_id not in affected]!=[r for r in after.relationships if r.target_id not in affected]:
                raise ValueError('repair changed unaffected relationships')
            if [p for p in paper.comparison.pairs if p.target_id not in affected]!=[p for p in replacement.comparison.pairs if p.target_id not in affected]:
                raise ValueError('repair changed unaffected comparisons')
            candidate['papers'][i]=replacement.model_dump()
    if found!=set(scope): raise ValueError('concern refers to an unknown paper')
    data['run']={**data['run'],'reassessment_parent_sha256':digest(parent.model_dump()),
        'reassessment_fresh_sha256':digest(fresh.model_dump()),'interpretation_concerns':[c.model_dump() for c in concerns]}
    return NoveltyComparisonResult.model_validate(data)


class ReassessmentResult(Strict):
    parent: NoveltyComparisonResult
    parent_sha256: Text
    concerns: list[InterpretationConcern]
    fresh: NoveltyComparisonResult
    merged: NoveltyComparisonResult

    @model_validator(mode='after')
    def lineage(self):
        if self.parent_sha256!=digest(self.parent.model_dump()): raise ValueError('parent comparison hash mismatch')
        if self.merged!=merge_reassessment(self.parent,self.fresh,self.concerns): raise ValueError('reassessment merge differs from reviewed results')
        return self


class InterpretationReassessor(NoveltyComparator):
    """Patch only authorized relationships/pairs; freshly audit the whole paper."""
    def __init__(self,store,parent,concerns,*args,**kwargs):
        super().__init__(store,*args,**kwargs)
        self.parent=NoveltyComparisonResult.model_validate(parent.model_dump())
        self.concerns=[InterpretationConcern.model_validate(c) for c in concerns]
        self.repairs={}
        for c in self.concerns:
            candidate=next(x for x in self.parent.candidates if x.direction_id==c.direction_id)
            paper=next(p for p in candidate.papers if p.paper_id==c.paper_id)
            if paper.status!='complete' or not set(c.target_ids)<=set(paper.target_ids) or not set(c.passage_ids)<={p.passage_id for p in paper.passages}:
                raise ValueError('repair needs accepted targets and their original passages')
            key=(c.paper_id,tuple(paper.target_ids))
            if key in self.repairs: raise ValueError('ambiguous or duplicate paper repair')
            self.repairs[key]=(paper,c)

    async def _reviewed(self,payload,targets,passages,reviews,evidence_history,requests):
        old,concern=self.repairs[(payload['prior_paper_id'],tuple(t.target_id for t in targets))]
        if passages!=old.passages: raise ValueError('saved and loaded source passages differ')
        builder=EvidenceBuilder(self.io,prompts)
        record=old.evidence_reviews[-1].record
        scope=dict(claim_ids=[],target_ids=concern.target_ids,allow_new_claims=False)
        current=record
        for attempt in range(3):
            previous=evidence_history[-1] if evidence_history else None
            if previous and scope_citation_gaps(previous.report,current):
                # Scope evidence found by a fresh audit must be attached and
                # independently rechecked; it does not require rewording claims.
                current=attach_review_citations(current,previous.report,passages)
                response=EvidenceResponse(record=current,abstention_reason=None,
                    revision_notes=['Attached original-page citations named by the preceding scope audit; claim text is unchanged.'])
            else:
                body=dict(payload=payload,record=current.model_dump(),repair_scope=scope,
                    corrections=dict(reason=concern.reason,passage_ids=concern.passage_ids,
                        last_review=evidence_history[-1].report.model_dump() if evidence_history else None))
                def check(patch):
                    changed=apply_evidence_patch(current,patch,scope).record
                    validate_record(changed,targets,passages,require_alignment=True,require_context=True)
                patch=await checked_call(self.io,'patch_comparison_evidence',prompts.PATCH_EVIDENCE,EvidencePatch,body,check)
                response=apply_evidence_patch(current,patch,scope)
            audit=await builder.audit(payload,response,passages,evidence_history,'comparison_reopen')
            if eligible(audit)[0]==set(old.target_ids): break
            current=response.record
        if eligible(audit)[0]!=set(old.target_ids): raise ValueError('fresh full-paper evidence audit remains unresolved')
        issues=[dict(category='grounding',field_path=f'/pairs/{i}/rationale',explanation=concern.reason,
            required_change='Correct this target throughout its rationale and dimensions using the freshly reviewed relationship; preserve unrelated targets.',
            passage_ids=concern.passage_ids) for i,p in enumerate(old.comparison.pairs) if p.target_id in concern.target_ids]
        corrections=ComparisonReport(decision='revise',summary=concern.reason,issues=issues).model_dump()
        return await super()._reviewed(payload,targets,passages,reviews,evidence_history,requests,
            seed=audit,prior_draft=old.comparison,initial_corrections=corrections)

    async def reassess(self,generation,search):
        fresh=await self.run(generation,search,paper_ids={c.paper_id for c in self.concerns})
        self.fresh_result=fresh
        merged=merge_reassessment(self.parent,fresh,self.concerns)
        return ReassessmentResult(parent=self.parent,parent_sha256=digest(self.parent.model_dump()),
            concerns=self.concerns,fresh=fresh,merged=merged)


def merge_recovery(parent, fresh):
    """Retain completed papers; propagate fresh substantive review objections."""
    if parent.directions_ref!=fresh.directions_ref or parent.search_ref!=fresh.search_ref:
        raise ValueError('recovery input lineage changed')
    data=parent.model_dump()
    for candidate in data['candidates']:
        old=next(c for c in parent.candidates if c.direction_id==candidate['direction_id'])
        new=next(c for c in fresh.candidates if c.direction_id==candidate['direction_id'])
        for i,paper in enumerate(old.papers):
            if paper.status=='complete': continue
            replacement=next(p for p in new.papers if p.paper_id==paper.paper_id)
            accepted=lambda p:{x.target_id for x in p.comparison.pairs} if p.comparison else set()
            if replacement.status in ('skipped','failed'): continue
            substantive_review=any(r.report is not None for r in replacement.evidence_reviews+replacement.reviews)
            if not accepted(paper)<=accepted(replacement) and not substantive_review: continue
            if paper.targets!=replacement.targets or paper.expected_artifact_sha256!=replacement.expected_artifact_sha256:
                raise ValueError('recovery changed candidate or source identity')
            if paper.passages and (paper.passages!=replacement.passages or paper.context_pages!=replacement.context_pages):
                raise ValueError('original source changed during recovery')
            candidate['papers'][i]=replacement.model_dump()
    data['run']={**data['run'],'recovery_parent_sha256':digest(parent.model_dump()),
        'recovery_fresh_sha256':digest(fresh.model_dump())}
    return NoveltyComparisonResult.model_validate(data)


class ComparisonRecoveryResult(Strict):
    parent: NoveltyComparisonResult
    fresh: NoveltyComparisonResult
    merged: NoveltyComparisonResult

    @model_validator(mode='after')
    def lineage(self):
        if self.merged!=merge_recovery(self.parent,self.fresh):
            raise ValueError('recovery merge differs from reviewed results')
        return self


class IncompleteComparisonRecovery(NoveltyComparator):
    """Resume only incomplete papers using their existing evidence and feedback."""
    def __init__(self,store,parent,*args,concerns=(),**kwargs):
        super().__init__(store,*args,**kwargs)
        self.parent=NoveltyComparisonResult.model_validate(parent.model_dump())
        self.pending={(p.paper_id,tuple(p.target_ids)):p for c in parent.candidates
            for p in c.papers if p.status!='complete'}
        self.concerns={}
        for raw in concerns:
            c=InterpretationConcern.model_validate(raw)
            candidate=next((x for x in self.parent.candidates if x.direction_id==c.direction_id),None)
            paper=next((p for p in candidate.papers if p.paper_id==c.paper_id),None) if candidate else None
            if (paper is None or paper.status=='complete' or not set(c.target_ids)<=set(paper.target_ids)
                    or not set(c.passage_ids)<={p.passage_id for p in paper.passages}):
                raise ValueError('recovery concern must name incomplete targets and their original passages')
            key=(paper.paper_id,tuple(paper.target_ids))
            self.concerns.setdefault(key,[]).append(c.model_dump())

    async def _reviewed(self,payload,targets,passages,reviews,evidence_history,requests):
        old=self.pending.get((payload['prior_paper_id'],tuple(t.target_id for t in targets)))
        if old is None: raise ValueError('paper is outside incomplete recovery scope')
        if old.passages and old.passages!=passages: raise ValueError('saved and loaded source passages differ')
        payload={**payload,'recovery_feedback':dict(diagnostic=old.diagnostic,
            comparison_reviews=[r.report.model_dump() for r in old.reviews if r.report],
            source_observations=self.concerns.get((old.paper_id,tuple(old.target_ids)),[]),
            instruction='Resolve the recorded failure against these original pages. Return exactly one schema-conforming JSON object, without trailing text or extra fields. record contains only claims and relationships; abstention_reason and revision_notes belong at the response root. Do not add *_note fields. Every included proposal_match needs supporting claim_ids; do not create empty claim references for unknown matches. Coverage and claim citations must name this prior paper; candidate-source passages are context, not prior-paper evidence. Verify any source observations against the actual passages; they are concerns to check, not acceptance instructions.')}
        if not old.evidence_reviews:
            return await super()._reviewed(payload,targets,passages,reviews,evidence_history,requests)
        builder=EvidenceBuilder(self.io,prompts)
        previous=old.evidence_reviews[-1]
        response=EvidenceResponse(record=attach_review_citations(previous.record,previous.report,passages),abstention_reason=None,
            revision_notes=['Attach known source citations identified by the preceding audit, then freshly re-audit saved evidence; the full parent history remains in the recovery artifact.'])
        seed=await builder.audit(payload,response,passages,evidence_history,'comparison_reopen')
        seed=await builder.repair_remaining(payload,targets,passages,evidence_history)
        prior_draft=corrections=None
        if old.reviews and old.reviews[-1].report and old.reviews[-1].report.decision=='revise':
            last=old.reviews[-1]
            ids,claims=eligible(seed)
            if claims==last.draft.claims and ids=={p.target_id for p in last.draft.pairs} and all(i.field_path.startswith('/pairs/') for i in last.report.issues):
                prior_draft,corrections=last.draft,last.report.model_dump()
        return await super()._reviewed(payload,targets,passages,reviews,evidence_history,requests,
            seed=seed,prior_draft=prior_draft,initial_corrections=corrections)

    async def recover(self,generation,search):
        if not self.pending: raise ValueError('no incomplete papers to recover')
        fresh=await self.run(generation,search,paper_ids={pid for pid,_ in self.pending})
        return ComparisonRecoveryResult(parent=self.parent,fresh=fresh,merged=merge_recovery(self.parent,fresh))
