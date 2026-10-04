"""Deterministic evidence dependencies, bounded review, and partial publication."""
from novelty.comparison_records import EvidenceResponse, EvidenceReport, EvidenceReview, ReviewFormatRepair
from novelty.comparison_schemas import ComparisonDraft, PairComparison, Passage
from novelty.pipeline import ModelOutputError
from llm_client import errors


def proposal_requirements(target):
    return [f'/{key}/{i}' for key in ('intervention','comparison','regime','expected_effect')
        for i,f in enumerate(getattr(target,key)) if f.basis=='candidate_proposal']


def validate_record(record,targets,passages,require_alignment=False,require_context=False):
    if {r.target_id for r in record.relationships}!={t.target_id for t in targets}:
        raise ValueError('evidence record must cover exactly the requested targets')
    by_target={t.target_id:t for t in targets}
    for r in record.relationships:
        matches={m.facet_path:m for m in r.proposal_matches}
        required=set(proposal_requirements(by_target[r.target_id]))
        if len(matches)!=len(r.proposal_matches) or not set(matches)<=required:
            raise ValueError('invalid or duplicate proposal facet anchors')
        if require_alignment and r.coverage in ('direct_empirical','direct_theoretical','inferred_implication'):
            if not required or set(matches)!=required or any(m.relationship_match!='covered' for m in matches.values()):
                raise ValueError(f'{r.target_id}: direct/inferred coverage requires all proposed intervention/comparison/regime/effect facets {sorted(required)} to be covered by cited claims. Background problem evidence is insufficient; use related or insufficient_evidence if the proposed relationship is not established.')
    if require_context:
        from novelty.comparison_evidence import attach_table_context
        if attach_table_context(record,passages)!=record:
            raise ValueError('cited table is missing its explicitly associated caption/note')
    known={p.passage_id for p in passages}
    for c in record.claims:
        missing=set(c.passage_ids)-known
        if missing:raise ValueError(f'claim {c.claim_id}: unknown/wrong-paper passage IDs {sorted(missing)}')


def validate_evidence_report(report,record,passages,require_scope=False):
    claims={c.claim_id:c for c in record.claims}; targets={r.target_id for r in record.relationships}
    if len(report.claim_checks)!=len(claims) or {c.claim_id for c in report.claim_checks}!=set(claims):
        raise ValueError('review must check every evidence claim exactly once')
    if len(report.coverage_checks)!=len(targets) or {c.target_id for c in report.coverage_checks}!=targets:
        raise ValueError('review must check every target for mapping and omitted evidence exactly once')
    known={p.passage_id for p in passages}
    for c in report.claim_checks+report.coverage_checks:
        if not set(c.passage_ids)<=known:raise ValueError(f'review entry {getattr(c, "claim_id", getattr(c, "target_id", ""))}: unknown/wrong-paper passage IDs {sorted(set(c.passage_ids)-known)}')
    for c in report.claim_checks:
        if require_scope and (c.scope_checks is None or c.scope_summary is None):
            raise ValueError(f'claim {c.claim_id}: explicit scope checks and summary required')
        if require_scope and claims[c.claim_id].kind in ('result','evaluation','theoretical_result') and not c.scope_checks:
            raise ValueError(f'claim {c.claim_id}: result/evaluation/theory needs explicit assertion and condition checks')
        for check in c.scope_checks or []:
            if not set(check.passage_ids)<=known:
                raise ValueError('scope check contains unknown/wrong-paper passage IDs')
            if check.decision=='supported' and not check.passage_ids:
                raise ValueError(f'claim {c.claim_id}: supported scope assertion requires evidence')
            if c.decision=='supported' and check.decision!='supported':
                raise ValueError(f'claim {c.claim_id}: unresolved/overstated/uncited scope cannot be marked supported')
        if c.decision=='supported' and not set(c.passage_ids)&set(claims[c.claim_id].passage_ids):
            raise ValueError(f'claim {c.claim_id} marked supported without referencing its attached passages {claims[c.claim_id].passage_ids}; review cites {c.passage_ids}. Inspect the attached evidence: include its supporting ID if entailed, otherwise request citation/claim repair instead of supported status.')


def scope_citation_gaps(report,record):
    """Known but unattached scope evidence is a record repair, not malformed JSON."""
    if report is None:return {}
    attached={c.claim_id:set(c.passage_ids) for c in record.claims}
    gaps={c.claim_id:sorted({p for check in c.scope_checks or [] for p in check.passage_ids}-attached[c.claim_id])
        for c in report.claim_checks}
    return {cid:refs for cid,refs in gaps.items() if refs}


def eligible(review):
    if review.report is None:return set(),[]
    gaps=scope_citation_gaps(review.report,review.record)
    supported={c.claim_id for c in review.report.claim_checks if c.decision=='supported' and c.claim_id not in gaps}
    adequate={c.target_id for c in review.report.coverage_checks if c.decision=='adequate'}
    targets={r.target_id for r in review.record.relationships if r.target_id in adequate and r.refs()<=supported}
    return targets,[c for c in review.record.claims if c.claim_id in supported]


def bind_relationships(draft,evidence):
    relationships={r.target_id:r for r in evidence.record.relationships}
    pairs=[]
    for p in draft.pairs:
        r=relationships[p.target_id]
        status={'direct_empirical':'tested','direct_theoretical':'tested','inferred_implication':'inferred','discussed':'discussed_only'}.get(r.coverage,'not_established')
        refs=r.conclusion if status in ('tested','inferred','discussed_only') else []
        pairs.append(type(p).model_validate({**p.model_dump(),'hypothesis_tested':status,'hypothesis_claim_ids':refs}))
    return type(draft)(claims=draft.claims,pairs=pairs)


def assemble(response,evidence,targets):
    ids,claims=eligible(evidence)
    if {p.target_id for p in response.pairs}!=ids or len(response.pairs)!=len(ids):
        raise ValueError('interpretation must cover exactly evidence-eligible targets')
    relationships={r.target_id:r for r in evidence.record.relationships}
    pairs=[]
    for p in response.pairs:
        r=relationships[p.target_id]
        status={'direct_empirical':'tested','direct_theoretical':'tested','inferred_implication':'inferred','discussed':'discussed_only'}.get(r.coverage,'not_established')
        if r.coverage=='insufficient_evidence' and p.classification is not None:
            raise ValueError('insufficient evidence must not yield an overlap verdict')
        pairs.append(PairComparison.model_validate({**p.model_dump(),'hypothesis_tested':status,
            'hypothesis_claim_ids':r.conclusion if status in ('tested','inferred','discussed_only') else []}))
    return ComparisonDraft(claims=claims,pairs=pairs)


def accepted_targets(report,draft,evidence=None):
    if report is None or report.decision=='abstain':return set()
    all_ids={p.target_id for p in draft.pairs}
    if report.decision=='pass':return all_ids
    blocked=set()
    relationships={r.target_id:r for r in evidence.record.relationships} if evidence else {}
    for issue in report.issues:
        parts=issue.field_path.strip('/').split('/')
        if len(parts)>=2 and parts[0]=='pairs' and parts[1].isdigit() and int(parts[1])<len(draft.pairs):
            blocked.add(draft.pairs[int(parts[1])].target_id)
        elif len(parts)>=2 and parts[0]=='claims' and parts[1].isdigit() and int(parts[1])<len(draft.claims):
            cid=draft.claims[int(parts[1])].claim_id
            blocked|={p.target_id for p in draft.pairs if cid in set(p.hypothesis_claim_ids)|{c for d in p.dimensions for c in d.claim_ids}|(relationships[p.target_id].refs() if p.target_id in relationships else set())}
        else:return set()  # unlocalizable objection cannot silently approve other pairs
    return all_ids-blocked


def project(draft,target_ids):
    pairs=[p for p in draft.pairs if p.target_id in target_ids]
    if not pairs:return None
    used={c for p in pairs for d in p.dimensions for c in d.claim_ids}|{c for p in pairs for c in p.hypothesis_claim_ids}
    return type(draft)(claims=[c for c in draft.claims if c.claim_id in used],pairs=pairs)


def repair_blockers(paper, target_id):
    """Distinguish processing defects from an accepted insufficient-evidence finding."""
    if paper.status == 'skipped': return ['not_attempted']
    if paper.diagnostic and ('evidence_abstained:' in paper.diagnostic or 'comparison_abstained:' in paper.diagnostic):
        return ['model_abstention']
    if paper.diagnostic and paper.diagnostic.startswith(('ValueError:', 'RuntimeError:', 'HTTP ', 'input_budget:')):
        return ['processing_error']
    if not paper.evidence_reviews: return ['evidence_not_reviewed']
    review = paper.evidence_reviews[-1]
    if not review.report: return ['evidence_review_error']
    relationship = next(r for r in review.record.relationships if r.target_id == target_id)
    refs = relationship.refs()
    blockers = []
    if refs & scope_citation_gaps(review.report, review.record).keys(): blockers.append('citation_repair')
    if any(c.claim_id in refs and c.decision != 'supported' for c in review.report.claim_checks): blockers.append('claim_repair')
    if any(c.target_id == target_id and c.decision != 'adequate' for c in review.report.coverage_checks): blockers.append('coverage_review')
    return blockers or ['interpretation_review']


def outcomes(paper):
    accepted={p.target_id:p for p in paper.comparison.pairs} if paper.comparison else {}
    evidence=paper.evidence_reviews[-1] if paper.evidence_reviews else None
    relationships={r.target_id:r for r in evidence.record.relationships} if evidence else {}
    result=[]
    for tid in paper.target_ids:
        rel=relationships.get(tid)
        reviewed=tid in accepted
        status=('insufficient_evidence' if rel and rel.coverage=='insufficient_evidence' else
            'no_matching_result_found' if rel and rel.coverage=='no_match_found' else 'reviewed') if reviewed else (
            'not_assessed' if paper.status=='skipped' else 'review_unresolved')
        result.append({'target_id':tid,'review_status':status,'repair_blockers':[] if reviewed else repair_blockers(paper,tid),'coverage':rel.coverage if reviewed else None,
            'result':rel.result if reviewed else None,'relationship':rel.model_dump() if reviewed else None,
            'classification':accepted[tid].classification if reviewed else None,
            'evidence_scope':paper.evidence_scope,'missing_referenced_pages':paper.missing_referenced_pages,
            'absence_statement':'No matching result was established in the inspected evidence.' if reviewed and rel and rel.coverage=='no_match_found' else None,
            'literature_novelty':'not_assessed'})
    return result


class EvidenceBuilder:
    def __init__(self,io,prompts):self.io,self.prompts=io,prompts

    async def generate(self,payload,targets,passages,previous=None,corrections=None):
        if previous is not None:
            return await self.patch(payload, targets, passages, previous, corrections)
        body={'payload':payload}
        task='extract_comparison_evidence'
        request=body
        for attempt in range(2):
            try:
                out,_=await self.io._call(task if attempt==0 else 'repair_comparison_evidence',self.prompts.EXTRACT,EvidenceResponse,request)
                if out.record is None:raise ValueError('evidence_abstained: '+out.abstention_reason)
                from novelty.comparison_evidence import attach_table_context
                out.record=attach_table_context(out.record,passages)
                validate_record(out.record,targets,passages,require_alignment=True,require_context=True)
                return out
            except ModelOutputError as exc:detail,raw=str(exc),exc.raw
            except ValueError as exc:
                if str(exc).startswith('evidence_abstained:'):raise
                detail,raw=str(exc),out.model_dump()
            if attempt:raise ValueError('invalid_evidence_after_repair: '+detail)
            request={**body,'invalid_output':raw,'validation_errors':detail}

    async def patch(self,payload,targets,passages,previous,corrections=None):
        from novelty.comparison_repair import (EvidencePatch, attach_review_citations,
            repair_scope, apply_evidence_patch)
        from novelty.comparison_evidence import attach_table_context
        record = attach_review_citations(previous.record, previous.report, passages)
        scope = repair_scope(previous, corrections)
        notes = ['Attached known supporting citations from the independent audit; no claim is accepted without fresh review.']
        if not scope['claim_ids'] and not scope['target_ids'] and not corrections:
            return EvidenceResponse(record=record, abstention_reason=None, revision_notes=notes)
        body = dict(payload=payload, record=record.model_dump(), repair_scope=scope,
            corrections=corrections or (previous.report.model_dump() if previous.report else previous.error))
        request = body
        for attempt in range(2):
            try:
                patch, _ = await self.io._call('patch_comparison_evidence' if attempt == 0 else 'repair_evidence_patch',
                    self.prompts.PATCH_EVIDENCE, EvidencePatch, request)
                response = apply_evidence_patch(record, patch, scope)
                response.record = attach_table_context(response.record, passages)
                response.revision_notes = notes + response.revision_notes
                validate_record(response.record, targets, passages, require_alignment=True, require_context=True)
                return response
            except ModelOutputError as exc: detail, raw = str(exc), exc.raw
            except ValueError as exc:
                if str(exc).startswith('evidence_abstained:'): raise
                detail, raw = str(exc), patch.model_dump()
            if attempt: raise ValueError('invalid_evidence_patch_after_repair: ' + detail)
            request = {**body, 'invalid_output': raw, 'validation_errors': detail}

    async def audit(self,payload,response,passages,history,trigger):
        repairs=[];report=error=None;model=self.io.review_model or self.io.model
        body={'payload':payload,'record':response.record.model_dump(),
            'required_scope_claim_ids':[c.claim_id for c in response.record.claims
                if c.kind in ('result','evaluation','theoretical_result')]};request=body
        try:
            for attempt in range(2):
                try:
                    report,model=await self.io._call('review_comparison_evidence' if attempt==0 else 'repair_evidence_review',
                        self.prompts.EVIDENCE_REVIEW,EvidenceReport,request)
                    validate_evidence_report(report,response.record,passages,require_scope=True)
                    break
                except ModelOutputError as exc:detail,raw=str(exc),exc.raw
                except ValueError as exc:detail,raw=str(exc),report.model_dump()
                report=None
                if attempt:raise ValueError('invalid_evidence_review_after_repair: '+detail)
                repairs.append(ReviewFormatRepair(invalid_output=raw,validation_error=detail,model=model))
                request={**body,'invalid_review':raw,'validation_errors':detail}
        except Exception as exc:
            report,error=None,errors.describe(exc)
            raise
        finally:
            history.append(EvidenceReview(version=len(history),trigger=trigger,record=response.record,
                revision_notes=response.revision_notes,format_repairs=repairs,model=model,report=report,error=error))
        return history[-1]

    async def repair_remaining(self,payload,targets,passages,history):
        from opportunities.miner import digest
        seen = {digest(history[-1].record.model_dump())}
        for _ in range(3):
            audit = history[-1]
            needs_repair = (scope_citation_gaps(audit.report,audit.record)
                or any(c.decision!='supported' for c in audit.report.claim_checks)
                or any(c.decision!='adequate' for c in audit.report.coverage_checks))
            if not needs_repair: break
            response = await self.generate(payload,targets,passages,audit)
            fingerprint = digest(response.record.model_dump())
            repeated = fingerprint in seen
            seen.add(fingerprint)
            await self.audit(payload,response,passages,history,'evidence_repair')
            # A source-backed disagreement gets one fresh audit, but identical
            # or cycling records must not consume another correction round.
            if repeated: break
        return history[-1]

    async def initial(self,payload,targets,passages,history):
        response=await self.generate(payload,targets,passages)
        await self.audit(payload,response,passages,history,'initial')
        return await self.repair_remaining(payload,targets,passages,history)

    async def reopen(self,payload,targets,passages,history,corrections):
        response=await self.generate(payload,targets,passages,history[-1],corrections)
        await self.audit(payload,response,passages,history,'comparison_reopen')
        return await self.repair_remaining(payload,targets,passages,history)
