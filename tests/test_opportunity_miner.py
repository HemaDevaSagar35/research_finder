"""Opportunity proposal/review boundaries and concurrency, without network."""
import asyncio
import json
from pathlib import Path

import httpx
import openai
import pytest

from landscape.schemas import Landscape
from llm_client import ChatResult
from opportunities.miner import OpportunityMiner, Settings, digest
from opportunities.schemas import MiningResult
from reasoning.cross_paper import CrossPaperReasoner
from reasoning.evidence import PaperStore
from reasoning.fake import FakeChat
from reasoning.fixtures import build_demo_corpus


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path/'corpus'
    _, ids = build_demo_corpus(root)
    land = Landscape(topic='Expert caching',paper_ids=[ids['P1'],ids['P2']],
        recurring_limitations=[{'statement':'Cache performance depends on workload and capacity.',
                               'supporting_papers':[ids['P1'],ids['P2']]}])
    out = asyncio.run(CrossPaperReasoner(PaperStore(root),FakeChat()).run(land))
    assert out.findings
    return root,land,out


class Chat:
    def __init__(self, change_proposal=None, change_review=None, truncate=False):
        self.calls=[]
        self.change_proposal=change_proposal
        self.change_review=change_review
        self.truncate=truncate

    async def __call__(self, *, messages, **kwargs):
        self.calls.append(kwargs)
        # The first user message remains the payload after a repair round.
        p=json.loads(messages[1]['content'])
        payload=p['payload']
        if p['task']=='proposal':
            sources=payload['sources']
            response={'candidates':[{
                'category':'missing_evaluation','question':'How does caching behave under changing workloads?',
                'rationale':'The reviewed results depend on locality and cache capacity.',
                'scope':'The reviewed papers and their tested cache/workload settings.',
                'uncertainties':['These papers do not establish literature-wide novelty.'],
                'source_ids':[s['source_id'] for s in sources],
                'evidence_ids':[e['evidence_id'] for s in sources for e in s['evidence']],
            }]}
            if self.change_proposal:
                self.change_proposal(response,payload)
        else:
            response={'candidate_id':payload['candidate_id'],'decision':'accept',
                      'notes':'The question remains open within the reviewed pages.',
                      'page_ids':[p['page_id'] for p in payload['pages']]}
            response.update(factual_status='supported', required_corrections=[],
                paper_assessments=[{'paper_id': pid, 'role': 'supporting', 'rationale': 'Supported in stub.',
                    'evidence_ids': [e['evidence_id'] for e in payload['evidence'] if e['paper_id']==pid],
                    'page_ids': [p['page_id'] for p in payload['pages'] if p['paper_id']==pid]}
                    for pid in sorted({e['paper_id'] for e in payload['evidence']})])
            if self.change_review:
                self.change_review(response,payload)
        return ChatResult(text=json.dumps(response),finish_reason='length' if self.truncate else 'stop',model='fake')


def run(inputs, chat=None, **settings):
    root,land,out=inputs
    miner=OpportunityMiner(PaperStore(root),chat or Chat(),settings=Settings(**settings))
    result=asyncio.run(miner.run(land,out))
    return result


def test_accepts_scoped_opportunity_with_code_owned_provenance(inputs):
    out=run(inputs)
    MiningResult.model_validate_json(out.model_dump_json())
    assert len(out.opportunities)==1
    op=out.opportunities[0]
    assert op.support=='multiple_papers'
    assert op.literature_novelty=='not_assessed'
    assert op.verification=='supported_in_reviewed_corpus'
    assert op.sources and op.evidence and op.review_sources
    assert out.calls['proposal']==out.calls['review']==1
    assert out.landscape_ref['sha256']==digest(inputs[1].model_dump())
    assert out.reasoning_ref['sha256']==digest(inputs[2].model_dump())


def test_rejects_wrong_landscape_before_calls(inputs):
    root,land,out=inputs
    out=out.model_copy(deep=True)
    out.landscape_ref['sha256']='wrong'
    chat=Chat()
    with pytest.raises(ValueError,match='exact landscape'):
        asyncio.run(OpportunityMiner(PaperStore(root),chat).run(land,out))
    assert chat.calls==[]


def test_diagnostics_are_never_proposal_sources(inputs):
    _,_,reasoning=inputs
    reasoning.findings=[];reasoning.observations=[];reasoning.tensions=[]
    chat=Chat();out=run(inputs,chat)
    assert not chat.calls and not out.opportunities
    assert out.diagnostics[0].reason=='no_reviewed_sources'


@pytest.mark.parametrize('which',['source','evidence','duplicate'])
def test_unknown_or_duplicate_references_never_reach_review(inputs,which):
    def bad(response,payload):
        c=response['candidates'][0]
        if which=='source': c['source_ids']=['invented']
        elif which=='evidence': c['evidence_ids']=['invented']
        else: c['evidence_ids'].append(c['evidence_ids'][0])
    chat=Chat(change_proposal=bad);out=run(inputs,chat)
    assert not out.opportunities
    assert out.diagnostics[0].reason=='invalid_reference'
    assert len(chat.calls)==1


@pytest.mark.parametrize('decision,reason',[('reject','rejected'),('insufficient','insufficient')])
def test_review_blocks_promotion(inputs,decision,reason):
    def review(r,p):r.update(decision=decision,notes='Already answered or not established by the supplied pages.')
    out=run(inputs,Chat(change_review=review))
    assert not out.opportunities
    assert out.diagnostics[0].reason==reason


@pytest.mark.parametrize('mode',['unknown_page','unknown_candidate','empty_accept'])
def test_invalid_review_is_repaired_then_rejected(inputs,mode):
    def bad(r,p):
        if mode=='unknown_page':r['page_ids']=['invented']
        elif mode=='unknown_candidate':r['candidate_id']='invented'
        else:r['page_ids']=[]
    out=run(inputs,Chat(change_review=bad))
    assert not out.opportunities
    assert out.diagnostics[0].reason=='invalid_review'
    assert out.calls['repair']==1


def test_stale_artifact_never_accepted(inputs):
    root,_,reasoning=inputs
    ev=reasoning.findings[0].evidence[0]
    path=root/ev.paper_id/'paper.json'
    path.write_text(path.read_text()+'\n')
    out=run(inputs)
    assert not out.opportunities
    assert out.diagnostics[0].reason=='stale_evidence'


def test_stale_review_page_never_accepted(inputs):
    root,_,reasoning=inputs
    src=reasoning.findings[0].review_sources[0]
    path=root/src.paper_id/f'{src.page:02d}.md'
    path.write_text(path.read_text()+'\nchanged')
    out=run(inputs)
    assert out.diagnostics[0].reason=='stale_evidence'
    assert not out.opportunities


def test_fabricated_value_under_genuine_hash_never_accepted(inputs):
    inputs[2].findings[0].evidence[0].source_value='Invented source text'
    out=run(inputs)
    assert out.diagnostics[0].reason=='stale_evidence'


def test_missing_page_never_accepted(inputs):
    root,_,reasoning=inputs
    src=reasoning.findings[0].review_sources[0]
    (root/src.paper_id/f'{src.page:02d}.md').unlink()
    out=run(inputs)
    assert out.diagnostics[0].reason=='missing_pages'


@pytest.mark.parametrize('settings,reason',[
    ({'max_review_pages':1},'page_budget'),({'max_input_tokens':1},'input_budget'),
    ({'max_calls':1},'call_budget'),({'max_calls':0},'call_budget')])
def test_budgets_fail_closed(inputs,settings,reason):
    out=run(inputs,**settings)
    assert not out.opportunities
    assert out.diagnostics[0].reason==reason
    assert out.calls['total']<=settings.get('max_calls',40)


def test_truncation_consumes_attempts_and_cannot_be_accepted(inputs):
    out=run(inputs,Chat(truncate=True))
    assert not out.opportunities
    assert out.calls['total']==2
    assert out.diagnostics[0].reason=='invalid_proposal'


def test_duplicate_candidates_reviewed_once(inputs):
    def duplicate(r,p):r['candidates']*=2
    out=run(inputs,Chat(change_proposal=duplicate))
    assert len(out.opportunities)==1
    assert out.calls['review']==1
    assert out.diagnostics[0].reason=='duplicate'


def test_no_candidate_is_valid_result(inputs):
    def empty(r,p):r['candidates']=[]
    out=run(inputs,Chat(change_proposal=empty))
    assert not out.opportunities and not out.diagnostics
    assert out.calls['total']==1


def test_concurrent_reviews_and_atomic_budget(inputs):
    class Concurrent(Chat):
        def __init__(self):
            super().__init__(change_proposal=self.proposals)
            self.active=0;self.peak=0;self.two=asyncio.Event()
        def proposals(self,r,p):
            base=r['candidates'][0]
            r['candidates']=[dict(base,question=f'Question {i}?') for i in range(4)]
        async def __call__(self,**kw):
            if json.loads(kw['messages'][1]['content'])['task']=='review':
                self.active+=1;self.peak=max(self.peak,self.active)
                if self.active==2:self.two.set()
                try:
                    await asyncio.wait_for(self.two.wait(),2)
                    return await super().__call__(**kw)
                finally:self.active-=1
            return await super().__call__(**kw)
    chat=Concurrent();out=run(inputs,chat,concurrency=2,max_calls=3)
    assert chat.peak==2
    assert out.calls['total']==3
    assert len(out.opportunities)==2
    assert sum(d.reason=='call_budget' for d in out.diagnostics)==2


def test_fatal_provider_stops_new_calls(inputs):
    class Fatal:
        async def __call__(self,**kwargs):
            raise openai.AuthenticationError('bad key',response=httpx.Response(401,request=httpx.Request('POST','https://example.test')),body={})
    out=run(inputs,Fatal(),batch_size=1)
    assert not out.opportunities
    assert out.run['stopped_for_provider_error']
    assert out.calls['total']==1


def test_single_paper_observation_remains_single_paper(inputs):
    root, land, reasoning = inputs
    pid = land.paper_ids[0]
    narrow = Landscape(topic=land.topic,paper_ids=[pid],recurring_limitations=[{
        'statement':'Caching depends on workload.','supporting_papers':[pid]}])
    reasoned = asyncio.run(CrossPaperReasoner(PaperStore(root),FakeChat()).run(narrow))
    assert reasoned.observations and not reasoned.findings
    out = run((root,narrow,reasoned))
    assert out.opportunities[0].support == 'single_paper'
    assert out.opportunities[0].paper_ids == [pid]


def test_review_cannot_accept_cross_paper_support_citing_one_paper(inputs):
    def bad(r,p):
        first=p['pages'][0]['paper_id']
        r['page_ids']=[page['page_id'] for page in p['pages'] if page['paper_id']==first]
    out=run(inputs,Chat(change_review=bad))
    assert not out.opportunities
    assert out.diagnostics[0].reason=='invalid_review'


def test_source_batch_limit_is_visible(inputs):
    reasoned=inputs[2]
    copied=reasoned.findings[0].model_copy(deep=True)
    copied.finding_id+='-second'
    reasoned.findings.append(copied)
    out=run(inputs,batch_size=1,max_batches=1)
    assert out.coverage['reviewed_sources']==2
    assert any(d.reason=='batch_limit' for d in out.diagnostics)


def test_too_many_candidates_requires_repair_then_fails(inputs):
    def too_many(r,p):r['candidates']*=6
    out=run(inputs,Chat(change_proposal=too_many))
    assert not out.opportunities
    assert out.diagnostics[0].reason=='invalid_proposal'
    assert out.calls['total']==2


def test_ready_batch_reviews_without_waiting_for_other_proposals(inputs):
    copied=inputs[2].findings[0].model_copy(deep=True)
    copied.finding_id+='-second'
    inputs[2].findings.append(copied)
    class Pipelined(Chat):
        def __init__(self):
            super().__init__()
            self.review_started=asyncio.Event()
        async def __call__(self,**kwargs):
            data=json.loads(kwargs['messages'][1]['content'])
            if data['task']=='proposal' and data['payload']['sources'][0]['source_id'].endswith('-second'):
                await asyncio.wait_for(self.review_started.wait(),2)
            elif data['task']=='review':
                self.review_started.set()
            return await super().__call__(**kwargs)
    out=run(inputs,Pipelined(),batch_size=1,concurrency=2)
    assert len(out.opportunities)==2
    assert out.calls['total']==4


@pytest.mark.parametrize('signal', ['corrections', 'status', 'revise', 'malformed_with_corrections'])
def test_factual_correction_veto_cannot_be_repaired_into_acceptance(inputs, signal):
    def correction(r, p):
        if signal in ('corrections', 'malformed_with_corrections'):
            r['required_corrections'] = ['TP-4 uses four GPUs of the same eight-GPU machine.']
        elif signal == 'status':
            r['factual_status'] = 'needs_correction'
        else:
            r['decision'] = 'revise'
        if signal == 'malformed_with_corrections':
            del r['paper_assessments']
    out = run(inputs, Chat(change_review=correction))
    assert not out.opportunities
    diag = out.diagnostics[0]
    assert diag.reason == 'requires_correction'
    assert diag.candidate and diag.review
    assert out.calls['total'] == 2
    assert out.calls.get('repair', 0) == 0


def test_uncertain_facts_cannot_be_accepted(inputs):
    def uncertain(r, p): r['factual_status'] = 'uncertain'
    out = run(inputs, Chat(change_review=uncertain))
    assert not out.opportunities
    assert out.diagnostics[0].reason == 'insufficient'
    assert out.calls.get('repair', 0) == 0


def test_context_only_paper_retains_citations_without_inflating_support(inputs):
    def roles(r, p):
        r['paper_assessments'][1]['role'] = 'context_only'
        r['paper_assessments'][1]['rationale'] = 'Candidate excludes this paper from its scope.'
    out = run(inputs, Chat(change_review=roles))
    op = out.opportunities[0]
    assert out.schema_version == 'opportunities_v2'
    assert len(op.paper_ids) == 2
    assert len(op.supporting_paper_ids) == len(op.context_paper_ids) == 1
    assert set(op.paper_ids) == set(op.supporting_paper_ids + op.context_paper_ids)
    assert op.support == 'single_paper'
    assert {e.paper_id for e in op.evidence} == set(op.paper_ids)
    assert {s.paper_id for s in op.review_sources} >= set(op.paper_ids)
    assert len(op.paper_assessments) == 2


def test_upstream_contradicting_stance_can_support_new_discrepancy(inputs):
    # A stance concerns the original finding; it does not fix the new paper role.
    inputs[2].findings[0].evidence[-1].stance = 'contradicts'
    out = run(inputs)
    op = out.opportunities[0]
    assert op.support == 'multiple_papers'
    assert set(op.supporting_paper_ids) == set(op.paper_ids)
    assert op.context_paper_ids == []


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'unknown', 'foreign_evidence', 'foreign_page', 'legacy'])
def test_acceptance_requires_complete_resolved_paper_roles(inputs, mode):
    def bad(r, p):
        roles = r['paper_assessments']
        if mode == 'missing': roles.pop()
        elif mode == 'duplicate': roles[1] = roles[0]
        elif mode == 'unknown': roles[0]['paper_id'] = 'invented'
        elif mode == 'foreign_evidence': roles[0]['evidence_ids'] = roles[1]['evidence_ids']
        elif mode == 'foreign_page': roles[0]['page_ids'] = roles[1]['page_ids']
        else:
            for key in ['paper_assessments', 'required_corrections', 'factual_status']: r.pop(key)
    out = run(inputs, Chat(change_review=bad))
    assert not out.opportunities
    assert out.diagnostics[0].reason == 'invalid_review'


def test_context_without_any_support_is_insufficient(inputs):
    def context(r, p):
        for a in r['paper_assessments']: a['role'] = 'context_only'
    out = run(inputs, Chat(change_review=context))
    assert not out.opportunities
    assert out.diagnostics[0].reason == 'insufficient'
    assert out.calls.get('repair', 0) == 0
