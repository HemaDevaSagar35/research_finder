"""Reviewed sparse revisions, provenance, conservative reuse and bounded routing."""
import asyncio
from copy import deepcopy
import json
import pytest
from critic.refinement_loop import RefinementLoop, RefinementResult, requests_for, BorrowedIO
from directions.revision import revise_direction, revised_generation, apply_patch
from directions.revision_schemas import Patch, DirectionRevision
from novelty.revalidation import rebind_reused_inputs, scientific_projection
from directions.schemas import CorrectnessIssue
from llm_client import ChatResult
from opportunities.miner import digest
from reasoning.evidence import PaperStore
from tests.test_direction_generator import inputs, link_checks
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready
from tests.test_novelty_assessment import bundle, Chat as AssessmentChat
from tests.test_research_critic import source, run, Chat as CriticChat


PATH='/experiments/0/informative_outcomes'


def issue():
    return dict(category='consistency',field_path=PATH,explanation='The measured null is misclassified.',
        required_change='Explicitly classify the resolved null.',source_spans=[])


def blocked(source):
    def mutate(out,req):
        if 'review_' not in req['task']: return
        out['decision']='revise'
        out['test_link_checks'][0]['decision']='contradiction'
        out['upstream_requests']=[dict(stage='direction_correctness',target_id=req['packet']['direction_id'],
            basis='proposal_consistency',field_paths=[PATH],passage_ids=[],reason='Measured null misclassified.')]
    return run(source,CriticChat(mutate))[0]


class Chat:
    def __init__(self, mode=None):
        self.mode=mode
        self.tasks=[]
        self.critic=CriticChat()
        self.assessment=AssessmentChat()
    async def __call__(self,**kw):
        req=json.loads(kw['messages'][1]['content']);task=req['task'];self.tasks.append(task)
        if task.endswith('review_revision_input'):
            out=dict(decision='revise',summary='Located contradiction.',issues=[issue()],test_link_checks=link_checks(req['candidate']))
            if self.mode=='disagree':out.update(decision='pass',issues=[])
        elif task.endswith('patch_direction'):
            value=req['candidate']['experiments'][0]['informative_outcomes']
            new='A resolved null falsifies the predicted difference.'
            if isinstance(value,list):new=[new]
            out=dict(edits=[dict(field_path=PATH,previous_value=value,value=new,issue_ids=[i['issue_id'] for i in req['issues']])],abstention_reason=None)
        elif task.endswith('review_direction_revision'):
            out=dict(decision='pass',summary='Corrected entire candidate.',issues=[],test_link_checks=link_checks(req['candidate']),
                resolutions=[dict(issue_id=i['issue_id'],resolved=True,reasoning='Null interpretation fixed.') for i in req['issues']])
            if self.mode=='reject':out.update(decision='abstain',summary='Evidence unclear.')
        elif 'novelty_reuse' in task:
            p=req['packet'];out=dict(targets=[dict(target_id=t,decision='reuse',reasoning='Only explanatory wording changed.')
                for t in [p['direction_id'],*[h['hypothesis_id'] for h in p['candidate']['hypotheses']]]])
            if task.endswith('review_novelty_reuse'):
                out.update(decision='pass',issues=[])
                if self.mode=='recheck':out['targets'][0]['decision']='recheck';out['decision']='revise'
        elif task in ('assess_novelty','review_novelty_assessment','revise_novelty_assessment'):
            return await self.assessment(**kw)
        else:return await self.critic(**kw)
        return ChatResult(json.dumps(out),'stop','fixture')


def loop(source,mode=None):
    original=blocked(source);chat=Chat(mode)
    result=asyncio.run(RefinementLoop(PaperStore(source[0]),chat,review_chat=chat).run(original))
    return result,chat


def test_complete_explanation_revision_and_fresh_verdicts(source):
    result,chat=loop(source)
    assert result.status=='ready',result.diagnostic
    assert len(result.cycles)==1
    cycle=result.cycles[0]
    assert cycle.certificates[0].decision=='reuse'
    old=result.original.novelty.inputs.generation.directions[0]
    new=cycle.generation.directions[0]
    assert old.direction_id==new.direction_id
    assert scientific_projection(old.proposal)==scientific_projection(new.proposal)
    assert digest(old.model_dump())!=digest(new.model_dump())
    assert 'assess_novelty' in chat.tasks and 'research_critique' in chat.tasks
    assert 'create_signature' not in chat.tasks
    assert RefinementResult.model_validate_json(result.model_dump_json())==result


@pytest.mark.parametrize('mode',['disagree','reject','recheck'])
def test_uncertainty_cannot_promote_candidate(source,mode):
    result,chat=loop(source,mode)
    assert result.status=='blocked'
    assert result.cycles[0].critic is None
    assert 'research_critique' not in chat.tasks


@pytest.mark.parametrize('tamper',['value','old_value','ids','missing','scope'])
def test_patch_enforces_exact_scope_and_identity(source,tamper):
    proposal=source[1].inputs.generation.directions[0].proposal
    old=proposal.experiments[0].informative_outcomes
    edit=dict(field_path=PATH,previous_value=old,value=['Corrected null.'] if isinstance(old,list) else 'Corrected null.',issue_ids=['issue-0'])
    issues=[CorrectnessIssue(**issue())]
    if tamper=='value':edit['value']=old
    if tamper=='old_value':edit['previous_value']='wrong'
    if tamper=='ids':
        edit.update(field_path='/hypotheses/0/hypothesis_id',previous_value=proposal.hypotheses[0].hypothesis_id,value='new')
        issues[0].field_path=edit['field_path']
    if tamper=='missing':edit['issue_ids']=['issue-99']
    if tamper=='scope':edit['field_path']='/research_direction'
    with pytest.raises(ValueError):apply_patch(proposal,Patch(edits=[edit],abstention_reason=None),issues)


@pytest.mark.parametrize('tamper',['parent','unaccepted','proposal','audit','certificate','extra_certificate'])
def test_saved_loop_rejects_tampering(source,tamper):
    result,_=loop(source);data=result.model_dump();c=data['cycles'][0]
    if tamper=='parent':c['input_critic_sha256']='wrong'
    if tamper=='unaccepted':c['revisions'][0]['accepted_refinements']=[dict(field_path=PATH,reason='invented',required_change='invented')]
    if tamper=='proposal':c['revisions'][0]['proposal']['research_direction']='unreviewed'
    if tamper=='audit':c['revisions'][0]['attempts'][0]['review']['test_link_checks'].pop()
    if tamper=='certificate':c['certificates'][0]['review']['decision']='abstain'
    if tamper=='extra_certificate':c['certificates'].append(deepcopy(c['certificates'][0]))
    with pytest.raises(ValueError):RefinementResult.model_validate(data)


def test_reuse_rejects_changed_science(source):
    result,_=loop(source);cycle=result.cycles[0]
    revised=cycle.generation.model_copy(deep=True)
    revised.directions[0].proposal.hypotheses[0].mechanism+=' Different mechanism.'
    with pytest.raises(ValueError):rebind_reused_inputs(result.original.novelty,revised,cycle.certificates)


def test_already_ready_needs_no_calls(source):
    original=run(source)[0];chat=Chat()
    result=asyncio.run(RefinementLoop(PaperStore(source[0]),chat).run(original))
    assert result.status=='ready' and not result.cycles and not chat.tasks


def test_borrowed_client_not_closed_by_inner_stages():
    class Shared:client=object();model='model'
    io=BorrowedIO(Shared())
    assert io.client is None and io.model=='model'


def test_generator_review_rejects_missing_or_duplicate_test_links(source):
    from directions.test_links import LinkedCorrectnessResponse,validate_links
    p=source[1].inputs.generation.directions[0].proposal
    checks=link_checks(p.model_dump())
    for invalid in (checks[:-1],checks+[checks[0]]):
        report=LinkedCorrectnessResponse(decision='pass',summary='Checked.',issues=[],test_link_checks=invalid)
        with pytest.raises(ValueError):validate_links(report.test_link_checks,p)


def test_refresh_requires_actual_linked_fresh_artifacts(source):
    result,_=loop(source);cycle=result.cycles[0];cert=cycle.certificates[0].model_copy(deep=True)
    cert.decision='recheck'
    with pytest.raises(ValueError,match='fresh search'):
        rebind_reused_inputs(result.original.novelty,cycle.generation,[cert])
    fresh_search=cycle.critic.novelty.inputs.search.model_copy(deep=True)
    fresh_comp=cycle.critic.novelty.inputs.comparisons.model_copy(deep=True)
    s,c=rebind_reused_inputs(result.original.novelty,cycle.generation,[cert],fresh_search,fresh_comp)
    assert s.run['execution']=='selective_refresh'
    fresh_comp.search_ref['sha256']='wrong'
    with pytest.raises(ValueError,match='lineage'):
        rebind_reused_inputs(result.original.novelty,cycle.generation,[cert],fresh_search,fresh_comp)


def test_manual_observation_is_reviewed_even_for_ready_candidate(source):
    original=run(source)[0];chat=Chat()
    request=dict(stage='direction_correctness',target_id=original.candidates[0].direction_id,
        basis='proposal_consistency',field_paths=[PATH],passage_ids=[],reason='Located manual consistency concern.')
    observations={original.candidates[0].direction_id:[request]}
    result=asyncio.run(RefinementLoop(PaperStore(source[0]),chat).run(original,observations=observations))
    assert result.status=='ready' and result.cycles
    assert result.observations
    assert RefinementResult.model_validate_json(result.model_dump_json())==result


def test_manual_observation_disagreement_cannot_use_old_ready_status(source):
    original=run(source)[0];chat=Chat('disagree')
    request=dict(stage='direction_correctness',target_id=original.candidates[0].direction_id,
        basis='proposal_consistency',field_paths=[PATH],passage_ids=[],reason='Located manual consistency concern.')
    result=asyncio.run(RefinementLoop(PaperStore(source[0]),chat).run(original,
        observations={original.candidates[0].direction_id:[request]}))
    assert result.status=='blocked' and result.diagnostic


def test_one_confirmed_issue_cannot_hide_another_request():
    from directions.revision import confirm_requests
    from directions.test_links import LinkedCorrectnessResponse
    class Report:decision='revise';issues=[CorrectnessIssue(**issue())]
    with pytest.raises(ValueError,match='disagreement'):
        confirm_requests(Report(),[dict(field_paths=['/experiments/1/why_this_test'])])


def test_recheck_executes_search_comparison_and_fresh_reviews(source):
    from tests.test_novelty_search import Backend,Chat as SearchChat
    from tests.test_novelty_comparison import Chat as ComparisonChat
    from research.retrieval import MultiQueryRetriever
    base=Chat('recheck');search=SearchChat();comparison=ComparisonChat();backend=Backend()
    async def chat(**kw):
        req=json.loads(kw['messages'][1]['content']);task=req['task']
        if task in ('create_signature','review_signature','rerank'):return await search(**kw)
        if 'comparison' in task or task=='compare_prior_work':return await comparison(**kw)
        return await base(**kw)
    original=blocked(source)
    async def execute():
        async with MultiQueryRetriever(backend) as retriever:
            return await RefinementLoop(PaperStore(source[0]),chat,retriever=retriever).run(original)
    result=asyncio.run(execute())
    assert result.status=='ready',(result.diagnostic, result.handoff(), [c.diagnostic for c in result.cycles[0].fresh_comparisons.candidates], [(p.status,p.diagnostic) for c in result.cycles[0].fresh_comparisons.candidates for p in c.papers])
    assert backend.calls and search.calls and comparison.calls
    assert backend.peak>1
    cycle=result.cycles[0]
    assert cycle.fresh_search and cycle.fresh_comparisons
    assert RefinementResult.model_validate_json(result.model_dump_json())==result
    data=result.model_dump();data['cycles'][0]['fresh_comparisons']['search_ref']['sha256']='wrong'
    with pytest.raises(ValueError):RefinementResult.model_validate(data)


def test_two_cycle_bound_preserves_remaining_refinement(source):
    base=Chat()
    def refine(out,req):
        if 'review_' in req['task']:return
        out['targets'][1]['action']='REFINE'
        out['revisions']=[dict(target_id=out['targets'][1]['target_id'],field_path=PATH,
            required_change='Clarify remaining test interpretation.',reason='Remaining scientific ambiguity.')]
    critic=CriticChat(refine)
    async def chat(**kw):
        req=json.loads(kw['messages'][1]['content']);task=req['task']
        if 'research_critique' in task:return await critic(**kw)
        response=await base(**kw)
        if task=='patch_direction':
            out=json.loads(response.text)
            edit=out['edits'][0]
            edit['value']=str(edit['previous_value'])+' Clarified interpretation.'
            return ChatResult(json.dumps(out),'stop','fixture')
        return response
    result=asyncio.run(RefinementLoop(PaperStore(source[0]),chat,max_cycles=2).run(blocked(source)))
    assert len(result.cycles)==2 and result.status=='needs_revision',result.diagnostic
    assert all(c.critic for c in result.cycles)
    assert result.handoff()['candidates'][0]['next_stage']=='direction_revision_then_correctness_and_novelty'
    assert RefinementResult.model_validate_json(result.model_dump_json())==result


def test_ready_artifact_cannot_skip_observations(source):
    original=run(source)[0]
    result=asyncio.run(RefinementLoop(PaperStore(source[0]),Chat()).run(original))
    data=result.model_dump()
    data['observations']={original.candidates[0].direction_id:[dict(stage='direction_correctness',
        target_id=original.candidates[0].direction_id,basis='proposal_consistency',
        field_paths=[PATH],passage_ids=[],reason='Unprocessed concern.')]}
    with pytest.raises(ValueError,match='skipped manual observations'):RefinementResult.model_validate(data)


def test_saved_preflight_cannot_forge_original_page_quote(source):
    result,_=loop(source);data=result.model_dump()
    pre=data['cycles'][0]['revisions'][0]['preflight']['issues'][0]
    pre['source_spans']=[dict(page_id='invented#p1',quote='A fabricated quotation.')]
    # Keep the patch's issue provenance internally equal, leaving source validation
    # as the failing check rather than only mismatched issue records.
    data['cycles'][0]['revisions'][0]['attempts'][0]['issues'][0]['source_spans']=pre['source_spans']
    with pytest.raises(ValueError,match='source quote'):RefinementResult.model_validate(data)


def test_rationale_eligibility_does_not_ignore_evidence_or_science(source):
    proposal=source[1].inputs.generation.directions[0].proposal
    correction=proposal.model_copy(deep=True)
    correction.rationale[0].statement='Qualified source attribution.'
    assert scientific_projection(correction)==scientific_projection(proposal)
    correction.rationale[0].evidence_ids=['different-evidence']
    assert scientific_projection(correction)!=scientific_projection(proposal)
    correction=proposal.model_copy(deep=True)
    correction.hypotheses[0].condition+=' In a new regime.'
    assert scientific_projection(correction)!=scientific_projection(proposal)


def test_upstream_relationship_error_retains_reassessment_route(source,bundle):
    from tests.test_novelty_assessment import run as assess_run
    def request(out,req):
        if 'review_novelty_assessment' not in req['task']:return
        evidence=req['packet']['evidence'][0]
        out.update(decision='revise',issues=[],reassessment_requests=[dict(paper_id=evidence['paper_id'],
            target_ids=[evidence['target_id']],claim_ids=[c['claim_id'] for c in evidence['claims']],
            passage_ids=[],reason='The accepted relationship transfers conditions between separate claims.')])
    novelty,_=assess_run(bundle,AssessmentChat(mutate=request))
    original=run((source[0],novelty))[0]
    assert original.handoff()['candidates'][0]['reassessment_requests']
    chat=Chat();result=asyncio.run(RefinementLoop(PaperStore(source[0]),chat).run(original))
    assert result.status=='blocked' and not chat.tasks
    assert result.diagnostic.startswith('upstream_evidence_reassessment_required')
    assert result.handoff()['candidates'][0]['reassessment_requests']
