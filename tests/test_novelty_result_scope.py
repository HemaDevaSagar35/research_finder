"""Result scope must be auditable without mistaking a changed setting for novelty."""
import pytest
from pydantic import ValidationError
from novelty.comparison_evidence import passages_for_pages, table_context_refs, attach_table_context
from novelty.comparison_records import EvidenceRecord, EvidenceReport
from novelty.comparison_workflow import validate_evidence_report
from novelty.comparison_schemas import NoveltyComparisonResult
from tests.test_novelty_comparison import ready, run, Chat, evidence_report
from tests.test_novelty_search import prepared
from tests.test_direction_generator import inputs


def table_fixture():
    passages=passages_for_pages([{'page_id':'P#p1','text':
        '## Table 1: Accuracy (Model A, cache 40%)\n\n| Method | Score |\n|---|---|\n| A | 80 |\n\n'
        'Note: average across tasks.\n\n## Table 2: Model B\n\n| Method | Score |\n|---|---|\n| B | 70 |'}])
    record=EvidenceRecord(claims=[dict(claim_id='c1',text='Model A scores 80 at cache 40%.',kind='result',passage_ids=[passages[1].passage_id])],
        relationships=[dict(target_id='H1',intervention=['c1'],comparator=[],conditions=[],outcome=[],conclusion=[],coverage='related',result='not_assessed',inference=None,explanation='Related result.')])
    return passages,record


def test_table_context_attaches_conditions_and_notes_without_changing_passage_identity():
    passages,record=table_fixture(); revised=attach_table_context(record,passages)
    assert revised.claims[0].passage_ids==[passages[i].passage_id for i in (1,0,2)]
    assert record.claims[0].passage_ids==[passages[1].passage_id]
    assert attach_table_context(revised,passages)==revised
    assert table_context_refs(passages)[passages[4].passage_id]==[passages[3].passage_id]


def test_next_table_caption_and_remote_setup_not_attached_to_previous_table():
    passages=passages_for_pages([{'page_id':'P#p1','text':
        '| A | B |\n|---|---|\n| 1 | 2 |\n\nTable 2: other experiment\n\n| C | D |\n|---|---|\n| 3 | 4 |\n\nUnrelated setup paragraph.'}])
    context=table_context_refs(passages)
    assert passages[0].passage_id not in context
    assert context[passages[2].passage_id]==[passages[1].passage_id]


@pytest.mark.parametrize('defect',['missing_checks','empty_evidence','contradictory_support','unknown_source'])
def test_scope_review_cannot_approve_incomplete_or_inconsistent_audit(defect):
    passages,record=table_fixture(); report=evidence_report(record.model_dump());check=report['claim_checks'][0]
    if defect=='missing_checks':check['scope_checks']=[]
    elif defect=='empty_evidence':check['scope_checks'][0]['passage_ids']=[]
    elif defect=='contradictory_support':check['scope_checks'][0]['decision']='overstated'
    else:check['scope_checks'][0]['passage_ids']=['other#paper']
    with pytest.raises(ValueError):validate_evidence_report(EvidenceReport.model_validate(report),record,passages,require_scope=True)


def test_missing_caption_can_be_reported_then_repaired_without_changing_claim():
    passages,record=table_fixture(); report=evidence_report(record.model_dump());check=report['claim_checks'][0]
    check['decision']='revise';check['scope_checks'][0].update(decision='missing_citation',passage_ids=[passages[0].passage_id])
    validate_evidence_report(EvidenceReport.model_validate(report),record,passages,require_scope=True)
    revised=attach_table_context(record,passages)
    validate_evidence_report(EvidenceReport.model_validate(evidence_report(revised.model_dump())),revised,passages,require_scope=True)
    assert revised.claims[0].text==record.claims[0].text


def test_mixed_model_scope_revision_happens_before_interpretation(ready):
    audits=0
    def mutate(out,req):
        nonlocal audits
        if req['task']=='review_comparison_evidence':
            audits+=1
            if audits==1:
                check=out['claim_checks'][0];check['decision']='revise'
                check['scope_checks'][0].update(aspect='model',decision='overstated',explanation='Model A result was generalized.')
        if req['task']=='patch_comparison_evidence':
            assert req['corrections']['claim_checks'][0]['scope_checks'][0]['decision']=='overstated'
            out['record']['claims'][0]['text']='Caching is evaluated under the stated stable workloads.'
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='complete' and len(paper.evidence_reviews)==2
    assert next(i for i,(r,_) in enumerate(chat.calls) if r['task']=='compare_prior_work')==4
    assert all(p.classification=='PARTIAL_OVERLAP' for p in paper.comparison.pairs)


def test_persisted_result_cannot_lose_scope_review(ready):
    result,_=run(ready);data=result.model_dump()
    data['candidates'][0]['papers'][0]['evidence_reviews'][0]['report']['claim_checks'][0]['scope_checks']=None
    with pytest.raises(ValidationError):NoveltyComparisonResult.model_validate(data)


def test_known_unattached_scope_support_enters_evidence_repair_not_format_retry(ready):
    audits=0
    def mutate(out,req):
        nonlocal audits
        if req['task']=='review_comparison_evidence':
            audits+=1
            if audits==1:
                out['claim_checks'][0]['scope_checks'][0]['passage_ids']=[req['payload']['prior_passages'][1]['passage_id']]
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='complete' and len(paper.evidence_reviews)==2
    assert not any(r['task']=='repair_evidence_review' for r,_ in chat.calls)
    assert paper.evidence_reviews[0].report.claim_checks[0].decision=='supported'
    assert paper.evidence_reviews[1].record.claims[0].passage_ids!=paper.evidence_reviews[0].record.claims[0].passage_ids


def test_new_scope_citation_gap_after_bounded_repair_blocks_publication(ready):
    for number in (3,4,5):
        (ready[0]/'outside-paper'/f'{number:02d}.md').write_text(f'Additional condition {number} discovered on re-audit.')
    audits=0
    def mutate(out,req):
        nonlocal audits
        if req['task']=='review_comparison_evidence':
            audits+=1
            out['claim_checks'][0]['scope_checks'][0]['passage_ids']=[req['payload']['prior_passages'][audits]['passage_id']]
    result,_=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.comparison is None and len(paper.evidence_reviews)==4


def test_current_records_cannot_drop_associated_table_context():
    from novelty.comparison_workflow import validate_record
    from tools.validate_novelty_evidence_controls import target
    from novelty.schemas import TargetSignature
    passages,record=table_fixture();t=TargetSignature.model_validate(target('A proposed caching relation.'))
    record.relationships[0].target_id=t.target_id
    with pytest.raises(ValueError,match='caption'):
        validate_record(record,[t],passages,require_context=True)
    validate_record(attach_table_context(record,passages),[t],passages,require_context=True)


def test_caption_with_baseline_bridge_is_not_replaced_by_next_table_caption():
    passages=passages_for_pages([{'page_id':'P#p10','text':
        '## Table 5: Approximation order (Model A, L=128)\n\nBaseline: SnapKV.\n\n'
        '| Order | Score |\n|---|---|\n| First | 46 |\n\n'
        '## Table 6: Different comparison\n\nNC: correction on top of selection.\n\n'
        '| Method | Score |\n|---|---|\n| Other | 47 |'}])
    contexts=table_context_refs(passages)
    assert contexts[passages[2].passage_id]==[passages[0].passage_id,passages[1].passage_id]
    assert contexts[passages[5].passage_id]==[passages[3].passage_id,passages[4].passage_id]


def test_interpretation_reviewer_receives_explicit_eligible_and_withheld_targets(ready):
    seen=[]
    def mutate(out,req):
        if req['task'] in ('extract_comparison_evidence','revise_comparison_evidence'):
            out['record']['claims'].append({'claim_id':'unresolved','text':'Missing condition.', 'kind':'evaluation',
                'passage_ids':[req['payload']['prior_passages'][0]['passage_id']]})
            out['record']['relationships'][0]['conditions']=['unresolved']
        if req['task']=='review_comparison_evidence':out['claim_checks'][-1]['decision']='unresolved'
        if req['task']=='review_comparison':
            assert len(req['eligible_target_ids'])==2 and len(req['withheld_target_ids'])==1
            assert {p['target_id'] for p in req['comparison']['pairs']}==set(req['eligible_target_ids'])
            assert not set(req['eligible_target_ids'])&set(req['withheld_target_ids'])
            seen.append(req)
    result,_=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert seen and paper.status=='partial'
    assert len(paper.evidence_view()['pairs'])==2
    assert len(paper.outcomes())==3
