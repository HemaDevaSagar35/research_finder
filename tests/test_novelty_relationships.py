"""Support/omission gates, revisitable evidence, semantic distinctions and dependencies."""
import pytest
from pydantic import ValidationError
from novelty.comparison_schemas import NoveltyComparisonResult
from novelty.comparison_workflow import proposal_requirements
from novelty.schemas import TargetSignature
from tests.test_novelty_comparison import ready, Chat, run
from tests.test_novelty_search import prepared
from tests.test_direction_generator import inputs

EVIDENCE=('extract_comparison_evidence','revise_comparison_evidence','repair_comparison_evidence')
AUDIT=('review_comparison_evidence','repair_evidence_review')
INTERPRET=('compare_prior_work','revise_comparison','repair_comparison')


@pytest.mark.parametrize('coverage,kind,result,status',[
    ('direct_empirical','result','contradicts_prediction','tested'),
    ('direct_empirical','result','inconclusive','tested'),
    ('direct_theoretical','theoretical_result','supports_prediction','tested'),
    ('inferred_implication','theoretical_result','supports_prediction','inferred'),
    ('discussed','discussion','not_assessed','discussed_only')])
def test_relationship_investigation_is_separate_from_prediction_confirmation(ready,coverage,kind,result,status):
    def mutate(out,req):
        if req['task'] in EVIDENCE:
            out['record']['claims'][0]['kind']=kind
            for r in out['record']['relationships']:
                r.update(coverage=coverage,result=result,conclusion=['c1'])
                if coverage!='discussed':
                    t=next(t for t in req['payload']['targets'] if t['target_id']==r['target_id'])
                    r['proposal_matches']=[{'facet_path':path,'claim_ids':['c1'],'relationship_match':'covered',
                        'explanation':'Fixture scientific relationship match.'} for path in proposal_requirements(TargetSignature.model_validate(t))]
                if coverage=='inferred_implication':r['inference']={'premise_claim_ids':['c1'],
                    'steps':['Fixture deductive step.'],'assumptions':['The stated bound applies under the candidate conditions.'],
                    'assumptions_status':'established'}
    run_result,_=run(ready,Chat(mutate));paper=run_result.candidates[0].papers[0]
    assert paper.status=='complete'
    assert all(p.hypothesis_tested==status for p in paper.comparison.pairs)
    assert all(o['result']==result for o in paper.outcomes())
    assert run_result.literature_novelty=='not_assessed'


def test_unproved_implication_assumptions_cannot_be_published(ready):
    def mutate(out,req):
        if req['task'] in EVIDENCE:
            r=out['record']['relationships'][0]
            r.update(coverage='inferred_implication',conclusion=['c1'],inference={'premise_claim_ids':['c1'],
                'steps':['The approximation bound might improve downstream accuracy.'],
                'assumptions':['Accuracy is monotonic in this error.'],'assumptions_status':'unresolved'})
    result,chat=run(ready,Chat(mutate))
    assert result.candidates[0].papers[0].comparison is None
    assert len(chat.calls)==2


@pytest.mark.parametrize('coverage,status',[('no_match_found','no_matching_result_found'),('insufficient_evidence','insufficient_evidence')])
def test_missing_match_and_inability_to_assess_never_mean_novel(ready,coverage,status):
    def mutate(out,req):
        if req['task'] in EVIDENCE:
            for r in out['record']['relationships']:r['coverage']=coverage
        if req['task'] in INTERPRET and coverage=='insufficient_evidence':
            for p in out['pairs']:p['classification']=None
    result,_=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='complete'
    assert all(o['review_status']==status and o['literature_novelty']=='not_assessed' for o in paper.outcomes())
    if coverage=='no_match_found':assert all('inspected evidence' in o['absence_statement'] for o in paper.outcomes())


def test_omitted_decisive_passage_reopens_record_before_interpretation(ready):
    n=0
    def mutate(out,req):
        nonlocal n
        if req['task']=='review_comparison_evidence':
            n+=1
            if n==1:out['coverage_checks'][0].update(decision='revise',
                explanation='The second passage discusses compression; examine omitted evidence.',
                passage_ids=[req['payload']['prior_passages'][1]['passage_id']])
        if req['task']=='revise_comparison_evidence':
            assert 'omitted evidence' in req['corrections']['coverage_checks'][0]['explanation']
            out['record']['claims'].append({'claim_id':'c2','text':'Future work could study compression-aware routing.',
                'kind':'discussion','passage_ids':[req['payload']['prior_passages'][1]['passage_id']]})
            out['record']['relationships'][0].update(coverage='discussed',conclusion=['c2'])
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert len(paper.evidence_reviews)==2 and paper.comparison.pairs[0].hypothesis_tested=='discussed_only'
    assert next(i for i,(r,_) in enumerate(chat.calls) if r['task']=='compare_prior_work')==4


def test_wrong_reviewer_objection_can_be_disputed_using_source(ready):
    count=0
    def mutate(out,req):
        nonlocal count
        if req['task']=='review_comparison_evidence':
            count+=1
            if count==1:out['claim_checks'][0].update(decision='revise',explanation='Claim allegedly absent.')
        if req['task']=='revise_comparison_evidence':
            out['revision_notes']=['Retained claim: the cited source explicitly states caching is evaluated.']
        if req['task'] in AUDIT:assert 'corrections' not in req  # fresh audit, objection not treated as truth
    result,_=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='complete'
    assert paper.evidence_reviews[0].record==paper.evidence_reviews[1].record
    assert paper.evidence_reviews[1].revision_notes


def test_bad_nonessential_claim_does_not_block_unrelated_comparisons(ready):
    def mutate(out,req):
        if req['task'] in EVIDENCE:
            out['record']['claims'].append({'claim_id':'incidental','text':'Unestablished incidental setting.',
                'kind':'evaluation','passage_ids':[req['payload']['prior_passages'][0]['passage_id']]})
        if req['task'] in AUDIT:out['claim_checks'][-1].update(decision='unresolved')
    result,_=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='complete'
    assert [c.claim_id for c in paper.comparison.claims]==['c1']
    assert paper.evidence_reviews[-1].report.claim_checks[-1].decision=='unresolved'


def test_material_unresolved_claim_blocks_only_dependent_target(ready):
    def mutate(out,req):
        if req['task'] in EVIDENCE:
            out['record']['claims'].append({'claim_id':'c2','text':'Unestablished decisive condition.',
                'kind':'evaluation','passage_ids':[req['payload']['prior_passages'][0]['passage_id']]})
            out['record']['relationships'][0]['conditions']=['c2']
        if req['task'] in AUDIT:out['claim_checks'][-1]['decision']='unresolved'
    result,_=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.status=='partial' and len(paper.comparison.pairs)==2
    assert paper.outcomes()[0]['review_status']=='review_unresolved'
    assert paper.outcomes()[0]['coverage'] is None


def test_interpretation_can_reopen_evidence_once_and_only_once(ready):
    def mutate(out,req):
        if req['task'] in INTERPRET:
            out.update(pairs=[],evidence_requests=[{'target_ids':req['eligible_target_ids'],
                'reason':'Inspect original discussion.', 'passage_ids':[req['payload']['prior_passages'][1]['passage_id']]}])
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert len(paper.evidence_requests)==2 and paper.comparison is None
    assert sum(r['task']=='revise_comparison_evidence' for r,_ in chat.calls)==1
    assert paper.evidence_reviews[-1].trigger=='comparison_reopen'


def test_late_grounding_issue_versions_evidence_and_rechecks_interpretation(ready):
    def mutate(out,req):
        if req['task']=='review_comparison' and out['decision']=='revise':
            out['issues'][0].update(category='grounding',field_path='/claims/0/text',
                passage_ids=[req['payload']['prior_passages'][0]['passage_id']])
    result,_=run(ready,Chat(mutate,decisions=['revise','pass']));paper=result.candidates[0].papers[0]
    assert paper.status=='complete' and len(paper.evidence_reviews)==2
    assert [r.evidence_version for r in paper.reviews]==[0,1]
    assert paper.evidence_reviews[-1].trigger=='comparison_reopen'


@pytest.mark.parametrize('tamper',['claim','support','coverage','version','projection'])
def test_saved_artifact_cannot_bypass_evidence_or_partial_review(ready,tamper):
    result,_=run(ready);data=result.model_dump();paper=data['candidates'][0]['papers'][0]
    if tamper=='claim':paper['evidence_reviews'][0]['record']['claims'][0]['text']='Changed after review'
    elif tamper=='support':paper['evidence_reviews'][0]['report']['claim_checks'][0]['decision']='unresolved'
    elif tamper=='coverage':paper['evidence_reviews'][0]['report']['coverage_checks']=[]
    elif tamper=='version':paper['reviews'][0]['evidence_version']=2
    else:paper['comparison']['pairs']=paper['comparison']['pairs'][:1]
    with pytest.raises(ValidationError):NoveltyComparisonResult.model_validate(data)


def test_missing_audit_entry_is_repaired_or_withheld(ready):
    def mutate(out,req):
        if req['task'] in AUDIT:out['coverage_checks']=[]
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.comparison is None and paper.evidence_reviews[0].error
    assert len(chat.calls)==3


def test_failed_local_repair_retains_already_accepted_unaffected_pairs(ready):
    result,_=run(ready,Chat(decisions=['revise'],fail='revise_comparison'))
    paper=result.candidates[0].papers[0]
    assert paper.status=='partial' and len(paper.comparison.pairs)==2
    assert paper.outcomes()[0]['review_status']=='review_unresolved'


def test_established_background_is_not_tested_proposed_direction(ready):
    def mutate(out,req):
        if req['task'] in EVIDENCE:
            out['record']['relationships'][0].update(coverage='direct_empirical',result='supports_prediction',conclusion=['c1'])
    result,chat=run(ready,Chat(mutate));paper=result.candidates[0].papers[0]
    assert paper.comparison is None and len(chat.calls)==2
    assert 'proposed' in paper.diagnostic
