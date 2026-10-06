import asyncio
from copy import deepcopy
import pytest
from novelty.reassessment import InterpretationConcern,InterpretationReassessor,ReassessmentResult,merge_reassessment
from reasoning.evidence import PaperStore
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready,run,Chat


def execute(ready,mutate=None):
    parent,_=run(ready);p=parent.candidates[0].papers[0]
    concern=InterpretationConcern(direction_id=parent.candidates[0].direction_id,paper_id=p.paper_id,
        target_ids=[p.target_ids[0]],passage_ids=[p.passages[0].passage_id],reason='Do not transfer an unstated measurement condition.')
    def patch(out,req):
        if req['task']=='patch_comparison_evidence':
            out['record']['relationships'][0]['explanation']='Separate observations do not establish the same condition.'
        if req['task']=='patch_comparison':
            out['pairs'][0]['rationale']='The original relationship remains untested under the unspecified condition.'
        if mutate:mutate(out,req)
    chat=Chat(mutate=patch)
    runner=InterpretationReassessor(PaperStore(ready[0]),parent,[concern],chat)
    result=asyncio.run(runner.reassess(ready[1],ready[2]))
    return result,chat


def test_scoped_reopening_preserves_claims_and_unaffected_interpretations(ready):
    result,chat=execute(ready)
    old=result.parent.candidates[0].papers[0];new=result.merged.candidates[0].papers[0]
    assert old.evidence_reviews[-1].record.claims==new.evidence_reviews[-1].record.claims
    assert old.evidence_reviews[-1].record.relationships[1:]==new.evidence_reviews[-1].record.relationships[1:]
    assert old.comparison.pairs[1:]==new.comparison.pairs[1:]
    assert old.comparison.pairs[0]!=new.comparison.pairs[0]
    assert len(chat.calls)==4
    assert ReassessmentResult.model_validate_json(result.model_dump_json())==result


def test_claim_mutation_is_not_accepted_as_interpretation_repair(ready):
    def mutate(out,req):
        if req['task']=='patch_comparison_evidence':out['record']['claims'][0]['text']='An invented replacement fact.'
        if req['task']=='repair_evidence_patch':out['claims']=[{**req['record']['claims'][0],'text':'An invented replacement fact.'}]
    with pytest.raises(ValueError,match='not independently complete'):execute(ready,mutate)


def test_saved_reassessment_cannot_change_an_unrelated_pair(ready):
    result,_=execute(ready);data=result.model_dump()
    data['merged']['candidates'][0]['papers'][0]['comparison']['pairs'][1]['rationale']='Unreviewed edit.'
    with pytest.raises(ValueError):ReassessmentResult.model_validate(data)


def test_source_change_blocks_reassessment_before_api(ready):
    parent,_=run(ready);paper=parent.candidates[0].papers[0]
    concern=InterpretationConcern(direction_id=parent.candidates[0].direction_id,paper_id=paper.paper_id,
        target_ids=[paper.target_ids[0]],passage_ids=[paper.passages[0].passage_id],reason='Correct scope.')
    (ready[0]/paper.paper_id/'01.md').write_text('A changed source page.')
    chat=Chat();runner=InterpretationReassessor(PaperStore(ready[0]),parent,[concern],chat)
    with pytest.raises(ValueError,match='not independently complete'):asyncio.run(runner.reassess(ready[1],ready[2]))
    assert not chat.calls


def test_changed_unrelated_relationship_is_rejected(ready):
    def mutate(out,req):
        if req['task']=='patch_comparison_evidence':out['record']['relationships'][1]['explanation']='Unrequested alteration.'
        if req['task']=='repair_evidence_patch':out['relationships']=[{**req['record']['relationships'][1],'explanation':'Unrequested alteration.'}]
    with pytest.raises(ValueError,match='not independently complete'):execute(ready,mutate)


def test_new_scope_citations_are_attached_then_independently_reviewed(ready):
    def mutate(out,req):
        if req['task']=='review_comparison_evidence':
            attached=req['record']['claims'][0]['passage_ids']
            other=next(p['passage_id'] for p in req['payload']['prior_passages'] if p['passage_id'] not in attached) if len(attached)==1 else attached[-1]
            out['claim_checks'][0]['scope_checks'].append(dict(aspect='regime',assertion='Scope supported by another original passage.',
                decision='supported',passage_ids=[other],explanation='Original-page support.'))
    result,chat=execute(ready,mutate)
    paper=result.merged.candidates[0].papers[0]
    old=result.parent.candidates[0].papers[0].evidence_reviews[-1].record.claims[0]
    new=paper.evidence_reviews[-1].record.claims[0]
    assert new.text==old.text and len(new.passage_ids)>len(old.passage_ids)
    assert len(paper.evidence_reviews)==2
    assert len([c for c,_ in chat.calls if c['task']=='patch_comparison_evidence'])==1
    assert ReassessmentResult.model_validate_json(result.model_dump_json())==result


def test_reassessment_cannot_simply_republish_unchanged_parent(ready):
    result,_=execute(ready)
    with pytest.raises(ValueError,match='no correction'):
        merge_reassessment(result.parent,result.parent,result.concerns)


@pytest.mark.parametrize('failure',['extract_comparison_evidence','review_comparison_evidence'])
def test_incomplete_recovery_reuses_saved_evidence_and_reviews_again(ready,failure):
    from novelty.reassessment import IncompleteComparisonRecovery,ComparisonRecoveryResult
    parent,_=run(ready,Chat(fail=failure));chat=Chat()
    result=asyncio.run(IncompleteComparisonRecovery(PaperStore(ready[0]),parent,chat).recover(ready[1],ready[2]))
    assert result.merged.candidates[0].papers[0].status=='complete'
    tasks=[q['task'] for q,_ in chat.calls]
    assert tasks.count('extract_comparison_evidence')==(1 if failure=='extract_comparison_evidence' else 0)
    assert 'review_comparison_evidence' in tasks and 'review_comparison' in tasks
    assert ComparisonRecoveryResult.model_validate_json(result.model_dump_json())==result


def test_recovery_preserves_complete_papers_and_previously_accepted_targets(ready):
    from novelty.reassessment import IncompleteComparisonRecovery,merge_recovery
    complete,_=run(ready);changed,_=execute(ready)
    assert merge_recovery(complete,changed.merged).candidates==complete.candidates
    partial,_=run(ready,Chat(decisions=['revise']))
    assert partial.candidates[0].papers[0].status=='partial'
    result=asyncio.run(IncompleteComparisonRecovery(PaperStore(ready[0]),partial,Chat(fail='review_comparison_evidence')).recover(ready[1],ready[2]))
    assert result.merged.candidates==partial.candidates


def test_recovery_does_not_hide_a_new_substantive_objection(ready):
    from novelty.reassessment import IncompleteComparisonRecovery
    parent,_=run(ready,Chat(decisions=['revise']))
    disputed=parent.candidates[0].papers[0].target_ids[1]
    def mutate(out,req):
        if req['task']=='review_comparison_evidence':
            out['coverage_checks'][1]['decision']='unresolved'
            out['coverage_checks'][1]['explanation']='The previously accepted relationship is not established by these sources.'
    result=asyncio.run(IncompleteComparisonRecovery(PaperStore(ready[0]),parent,Chat(mutate=mutate)).recover(ready[1],ready[2]))
    paper=result.merged.candidates[0].papers[0]
    assert disputed in {p.target_id for p in parent.candidates[0].papers[0].comparison.pairs}
    assert disputed not in {p.target_id for p in paper.comparison.pairs}
    assert paper.status=='partial'


def test_recovery_patches_reviewed_comparison_when_evidence_stays_exact(ready):
    from novelty.reassessment import IncompleteComparisonRecovery
    parent,_=run(ready,Chat(decisions=['revise']));chat=Chat()
    result=asyncio.run(IncompleteComparisonRecovery(PaperStore(ready[0]),parent,chat).recover(ready[1],ready[2]))
    assert result.merged.candidates[0].papers[0].status=='complete'
    tasks=[q['task'] for q,_ in chat.calls]
    assert 'patch_comparison' in tasks and 'compare_prior_work' not in tasks


def test_outcome_scope_label_still_requires_supported_source_references(ready):
    def mutate(out,req):
        if req['task']=='review_comparison_evidence':
            out['claim_checks'][0]['scope_checks'][0]['aspect']='outcome'
    result,_=run(ready,Chat(mutate=mutate))
    assert result.candidates[0].papers[0].status=='complete'
    def invalid(out,req):
        if req['task'] in ('review_comparison_evidence','repair_evidence_review'):
            out['claim_checks'][0]['scope_checks'][0].update(aspect='outcome',passage_ids=['wrong-paper#p1:unknown'])
    result,_=run(ready,Chat(mutate=invalid))
    assert result.candidates[0].papers[0].comparison is None


@pytest.mark.parametrize('rejected', [False, True])
def test_recovery_source_concerns_are_checked_not_acceptance_instructions(ready, rejected):
    from novelty.reassessment import IncompleteComparisonRecovery
    parent, _ = run(ready, Chat(fail='extract_comparison_evidence'))
    c = parent.candidates[0]; p = c.papers[0]
    concern = InterpretationConcern(direction_id=c.direction_id, paper_id=p.paper_id,
        target_ids=[p.target_ids[0]], passage_ids=[p.passages[0].passage_id],
        reason='Check whether separate measurements actually share this condition.')
    chat = Chat(decisions=['revise'] if rejected else ['pass'])
    result = asyncio.run(IncompleteComparisonRecovery(PaperStore(ready[0]), parent, chat,
        concerns=[concern]).recover(ready[1], ready[2]))
    req = next(q for q, _ in chat.calls if q['task']=='extract_comparison_evidence')
    assert req['payload']['recovery_feedback']['source_observations'] == [concern.model_dump()]
    assert any(q['task']=='review_comparison_evidence' for q, _ in chat.calls)
    assert (result.merged.candidates[0].papers[0].status=='complete') == (not rejected)


@pytest.mark.parametrize('defect', ['page', 'target', 'direction', 'complete'])
def test_recovery_rejects_unlocated_source_concerns_before_calls(ready, defect):
    from novelty.reassessment import IncompleteComparisonRecovery
    parent, _ = run(ready, Chat() if defect=='complete' else Chat(fail='extract_comparison_evidence'))
    c=parent.candidates[0];p=c.papers[0]
    concern=dict(direction_id=c.direction_id,paper_id=p.paper_id,target_ids=[p.target_ids[0]],
        passage_ids=[p.passages[0].passage_id],reason='Source-scope concern.')
    if defect=='page':concern['passage_ids']=['wrong-paper#p1:unknown']
    if defect=='target':concern['target_ids']=['unknown']
    if defect=='direction':concern['direction_id']='unknown'
    chat=Chat()
    with pytest.raises(ValueError,match='recovery concern'):
        IncompleteComparisonRecovery(PaperStore(ready[0]),parent,chat,concerns=[concern])
    assert not chat.calls


def test_recovery_attaches_prior_audit_citations_before_its_first_fresh_review(ready):
    from novelty.reassessment import IncompleteComparisonRecovery
    from novelty.comparison_records import ScopeCheck
    parent,_=run(ready,Chat(fail='review_comparison'))
    p=parent.candidates[0].papers[0];v=p.evidence_reviews[-1];claim=v.record.claims[0]
    extra=next(x.passage_id for x in p.passages if x.passage_id not in claim.passage_ids)
    check=next(x for x in v.report.claim_checks if x.claim_id==claim.claim_id)
    check.scope_checks.append(ScopeCheck(aspect='regime',assertion='Additional original-page scope.',
        decision='supported',passage_ids=[extra],explanation='Known source context.'))
    p.reviews=[];p.comparison=None;p.status='unresolved';p.diagnostic='Awaiting recorded citation attachment.'
    chat=Chat()
    result=asyncio.run(IncompleteComparisonRecovery(PaperStore(ready[0]),parent,chat).recover(ready[1],ready[2]))
    first=chat.calls[0][0]
    assert first['task']=='review_comparison_evidence'
    new=next(x for x in first['record']['claims'] if x['claim_id']==claim.claim_id)
    assert extra in new['passage_ids'] and new['text']==claim.text
    assert extra not in claim.passage_ids
    assert result.merged.candidates[0].papers[0].status=='complete'
