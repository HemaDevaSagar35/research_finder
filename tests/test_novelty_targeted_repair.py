"""Targeted repair must preserve evidence, dependency checks and failed-stage state."""
from copy import deepcopy
import pytest
from novelty.comparison_records import EvidenceRecord, EvidenceReport, EvidenceReview
from novelty.comparison_repair import (EvidencePatch, InterpretationPatch, attach_review_citations,
    repair_scope, apply_evidence_patch, apply_interpretation_patch, review_addresses)
from novelty.comparison_workflow import eligible
from tests.test_novelty_result_scope import table_fixture
from tests.test_novelty_comparison import ready, run, Chat, evidence_report
from tests.test_novelty_search import prepared
from tests.test_direction_generator import inputs


def reviewed_record():
    passages, record = table_fixture()
    record.claims.append(record.claims[0].model_copy(update={'claim_id':'unaffected','text':'Unchanged evidence.'}))
    report=EvidenceReport.model_validate(evidence_report(record.model_dump()))
    report.claim_checks[0].decision='revise'
    report.claim_checks[0].scope_checks[0].decision='overstated'
    review=EvidenceReview(version=0,trigger='initial',record=record,revision_notes=[],model='fixture',report=report,error=None)
    return passages,review


def test_claim_patch_preserves_unaffected_claim_and_reaudits_dependents():
    passages,review=reviewed_record();before=review.record.model_dump()
    scope=repair_scope(review)
    assert scope['claim_ids']==['c1'] and scope['target_ids']==['H1']
    fixed=review.record.claims[0].model_copy(update={'text':'Qualified result under Model A.'})
    patch=EvidencePatch(claims=[fixed],relationships=[],revision_notes=['Qualified scope.'],abstention_reason=None)
    result=apply_evidence_patch(review.record,patch,scope)
    assert result.record.claims[1]==review.record.claims[1]
    assert result.record.relationships==review.record.relationships
    assert review.record.model_dump()==before
    assert eligible(review)[0]==set()  # merging a patch never approves its evidence


@pytest.mark.parametrize('fault',['unaffected_claim','unknown_claim','unknown_target','duplicate_claim'])
def test_patch_rejects_unrequested_mutation(fault):
    _,review=reviewed_record();scope=repair_scope(review)
    claim=review.record.claims[0].model_copy(deep=True)
    claims=[claim];relationships=[]
    if fault=='unaffected_claim':claim.claim_id='unaffected'
    if fault=='unknown_claim':claim.claim_id='invented'
    if fault=='unknown_target':relationships=[review.record.relationships[0].model_copy(update={'target_id':'unknown'})]
    if fault=='duplicate_claim':claims=[claim,claim]
    patch=EvidencePatch(claims=claims,relationships=relationships,revision_notes=[],abstention_reason=None)
    with pytest.raises(ValueError):apply_evidence_patch(review.record,patch,scope)


def test_missing_citation_repair_changes_only_references_and_does_not_accept_claim():
    passages,review=reviewed_record()
    check=review.report.claim_checks[0]
    check.scope_checks[0].decision='missing_citation'
    check.scope_checks[0].passage_ids=[passages[0].passage_id]
    assert repair_scope(review)['claim_ids']==[]
    attached=attach_review_citations(review.record,review.report,passages)
    assert attached.claims[0].text==review.record.claims[0].text
    assert attached.relationships==review.record.relationships
    assert passages[0].passage_id in attached.claims[0].passage_ids
    assert eligible(review)[0]==set()
    check.scope_checks[0].passage_ids=['fabricated']
    with pytest.raises(ValueError,match='unknown'):attach_review_citations(review.record,review.report,passages)


def test_pair_grounding_patch_keeps_evidence_and_other_pairs_exactly(ready):
    def mutate(out,req):
        if req['task']=='review_comparison' and out['decision']=='revise':
            out['issues'][0].update(category='grounding',passage_ids=[req['payload']['prior_passages'][0]['passage_id']])
    result,chat=run(ready,Chat(mutate,decisions=['revise','pass']))
    paper=result.candidates[0].papers[0]
    assert paper.status=='complete' and len(paper.evidence_reviews)==1
    assert result.calls['patch_comparison']==1
    assert paper.reviews[0].draft.pairs[1:]==paper.reviews[1].draft.pairs[1:]
    assert paper.reviews[0].draft.claims==paper.reviews[1].draft.claims
    req=next(r for r,_ in chat.calls if r['task']=='review_comparison')
    assert req['valid_review_fields']['/pairs/0/dimensions/0/rationale'].startswith(paper.target_ids[0])


def test_wrong_target_interpretation_patch_is_rejected(ready):
    result,_=run(ready);draft=result.candidates[0].papers[0].reviews[0].draft
    raw=draft.pairs[1].model_dump();raw.pop('hypothesis_tested');raw.pop('hypothesis_claim_ids')
    patch=InterpretationPatch(pairs=[raw],abstention_reason=None)
    with pytest.raises(ValueError,match='unaffected'):apply_interpretation_patch(draft,patch,{draft.pairs[0].target_id})


def test_failed_patch_keeps_last_valid_review_and_unaffected_accepted_pairs(ready):
    result,_=run(ready,Chat(decisions=['revise'],fail='patch_comparison'))
    paper=result.candidates[0].papers[0]
    assert paper.status=='partial' and len(paper.comparison.pairs)==2
    assert len(paper.reviews)==1 and paper.reviews[0].report
    assert paper.outcomes()[0]['repair_blockers']==['processing_error']


def test_successive_citation_repairs_are_monotonic_and_freshly_reviewed(ready):
    (ready[0]/'outside-paper'/'03.md').write_text('A second source condition.')
    audits=0
    def mutate(out,req):
        nonlocal audits
        if req['task']=='review_comparison_evidence':
            audits+=1
            if audits<=2:
                out['claim_checks'][0]['scope_checks'][0]['passage_ids']=[req['payload']['prior_passages'][audits]['passage_id']]
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='complete' and len(paper.evidence_reviews)==3
    assert not any(r['task']=='patch_comparison_evidence' for r,_ in chat.calls)
    for old,new in zip(paper.evidence_reviews,paper.evidence_reviews[1:]):
        assert old.record.claims[0].text==new.record.claims[0].text
        assert set(old.record.claims[0].passage_ids)<set(new.record.claims[0].passage_ids)
        assert old.record.relationships==new.record.relationships
    assert result.schema_version=='novelty_comparison_v6'


def test_unchanged_disagreement_does_not_retry_indefinitely(ready):
    def mutate(out,req):
        if req['task']=='review_comparison_evidence':
            out['claim_checks'][0]['decision']='unresolved'
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.comparison is None and len(paper.evidence_reviews)==2
    assert result.calls['patch_comparison_evidence']==1
    assert len(chat.calls)==4
