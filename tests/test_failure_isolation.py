"""Acceptance tests for local failures and reviewed subset publication."""
import asyncio
import json
from copy import deepcopy
import pytest
from llm_client import ChatResult
from directions.generator import DirectionGenerator
from reasoning.evidence import PaperStore
from novelty.assessment import NoveltyAssessor, AssessmentSettings
from novelty.assessment_schemas import NoveltyAssessmentResult
from novelty.assessment_evidence import build_packet, locked_targets
from critic.pipeline import ResearchCritic
from critic.refinement_loop import RefinementLoop
from portfolio.pipeline import build_portfolio
from tests.test_direction_generator import inputs, Chat as GeneratorChat
from tests.test_novelty_search import run as search_run
from tests.test_novelty_comparison import run as comparison_run
from tests.test_novelty_assessment import Chat as AssessmentChat
from tests.test_research_critic import Chat as CriticChat


@pytest.fixture
def isolated(inputs):
    root,land,reason,mining=inputs
    def independent(out,payload):
        for i,e in enumerate(out['direction']['experiments']):
            e['hypothesis_ids']=[out['direction']['hypotheses'][i]['hypothesis_id']]
    gen=asyncio.run(DirectionGenerator(PaperStore(root),GeneratorChat(independent)).run(land,reason,mining))
    original=gen.directions[0]
    gen.directions=[original.model_copy(update={'direction_id':f'D{i}'},deep=True) for i in range(2)]
    folder=root/'outside-paper';folder.mkdir()
    (folder/'paper.json').write_text((root/land.paper_ids[0]/'paper.json').read_text())
    (folder/'01.md').write_text('We evaluate caching under stable workloads. Compression is not evaluated in these experiments.')
    (folder/'02.md').write_text('Future work could study compression-aware routing.')
    search,_,_=search_run((root,gen),max_calls=100)
    comparison,_=comparison_run((root,gen,search),max_calls=100)
    return root,gen,search,comparison


class IsolatedChat(AssessmentChat):
    def __init__(self, failures=()):
        self.failures=set(failures)
        self.requests=[]
        super().__init__(mutate=self.synthesis)
    def synthesis(self,out,req):
        if 'review_novelty_assessment' in req['task']: return
        packet=req['packet']
        if 'locked_hypotheses' not in packet: return
        coverage={c['target_id']:c['complete'] for c in packet['coverage']}
        retained=[h['target_id'] for h in packet['locked_hypotheses'] if h['finding'] not in ('UNRESOLVED','ALREADY_STUDIED') and coverage[h['target_id']]]
        original=[h['hypothesis_id'] for h in packet['candidate']['hypotheses']]
        if retained:
            out['refinement'].update(action='narrow' if retained!=original else 'retain',retained_hypothesis_ids=retained,removed_hypothesis_ids=[h for h in original if h not in retained])
        else: out['refinement'].update(action='defer')
    async def __call__(self,**kwargs):
        req=json.loads(kwargs['messages'][1]['content']);self.requests.append(req)
        packet=req['packet']
        if len(packet['coverage'])==1 and (packet['direction_id'],packet['coverage'][0]['target_id']) in self.failures:
            return ChatResult('{"targets":[],"refinement":{}}','stop','fixture')
        return await super().__call__(**kwargs)


def assess(isolated,chat,recovery=None):
    _,g,s,c=isolated
    return asyncio.run(NoveltyAssessor(chat,settings=AssessmentSettings()).run(g,s,c,recovery=recovery))


def test_failed_hypothesis_does_not_block_reviewed_subset_or_other_direction(isolated):
    hid=isolated[1].directions[0].proposal.hypotheses[1].hypothesis_id
    chat=IsolatedChat([('D0',hid)])
    assessment=assess(isolated,chat)
    assert assessment.candidates[0].assessment
    assert assessment.candidates[0].assessment.targets[2].finding=='UNRESOLVED'
    assert hid not in assessment.candidates[0].selection_scope
    critic=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(assessment))
    result=build_portfolio(critic)
    assert result.counts['selected']==2
    narrowed=next(c for c in result.candidates if c.candidate_id=='D0')
    assert len(narrowed.possible_hypotheses)==1
    assert all(hid not in e.hypothesis_ids for e in narrowed.suggested_initial_experiments)
    assert all('targets' not in r['schema']['properties'] for r in chat.requests if len(r['packet']['coverage'])==1 and 'review_novelty_assessment' not in r['task'])
    data=assessment.model_dump()
    data['candidates'][0]['assessment']['targets'][2]['finding']='PARTIAL_OVERLAP'
    with pytest.raises(ValueError): NoveltyAssessmentResult.model_validate(data)


def test_blocked_direction_does_not_block_selection_or_refinement(isolated):
    failures=[('D0',h.hypothesis_id) for h in isolated[1].directions[0].proposal.hypotheses]
    assessment=assess(isolated,IsolatedChat(failures))
    critic=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(assessment))
    result=build_portfolio(critic)
    assert result.status=='partial'
    assert result.counts['selected']==1 and result.counts['pending']==1
    assert result.candidates[0].candidate_id=='D1'


def test_retry_reuses_accepted_hypotheses_and_unaffected_direction(isolated):
    hid=isolated[1].directions[0].proposal.hypotheses[0].hypothesis_id
    first=assess(isolated,IsolatedChat([('D0',hid)]))
    # A published narrowed scope must still permit explicitly pending children to be retried.
    retry=IsolatedChat()
    second=assess(isolated,retry,recovery=first)
    assert second.candidates[1]==first.candidates[1]
    assert all(r['packet']['direction_id']=='D0' for r in retry.requests)
    singles=[r for r in retry.requests if len(r['packet']['coverage'])==1]
    assert singles and all(r['packet']['coverage'][0]['target_id']==hid for r in singles)
    assert len(second.candidates[0].selection_scope)==2


def test_shared_claim_concern_blocks_dependents_only(isolated):
    assessed=assess(isolated,IsolatedChat())
    candidate=assessed.candidates[0].model_copy(deep=True)
    packet=build_packet(assessed.inputs,'D0')
    first=candidate.hypothesis_assessments[0]
    evidence=next(e for e in packet['evidence'] if e['target_id']==first.coverage[0].target_id)
    from novelty.assessment_schemas import ReassessmentRequest
    first.reviews[-1].report.reassessment_requests=[ReassessmentRequest(paper_id=evidence['paper_id'],target_ids=[first.coverage[0].target_id],claim_ids=[evidence['claims'][0]['claim_id']],passage_ids=[],reason='Shared source claim requires review.')]
    targets=locked_targets(packet,candidate.hypothesis_assessments)
    assert all(t.finding=='UNRESOLVED' for t in targets) # fixture hypotheses share c1
    assert assessed.candidates[1].assessment is not None
    from critic.repair_routes import source_concerns
    from types import SimpleNamespace
    scoped=assessed.model_copy(update={'candidates':[candidate,assessed.candidates[1]]})
    concerns,_=source_concerns(SimpleNamespace(novelty=scoped,candidates=[]))
    assert len(concerns)==1 and concerns[0].passage_ids


def test_joint_experiment_is_not_silently_reassigned(isolated):
    from critic.evidence import packet_for
    from tests.test_research_critic import run as critic_run
    # Retaining only the followup leaves no initial test: keep that direction pending,
    # while its sibling remains eligible; never change a test stage to force selection.
    hid=isolated[1].directions[0].proposal.hypotheses[0].hypothesis_id
    assessment=assess(isolated,IsolatedChat([('D0',hid)]))
    critic=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(assessment))
    assert critic.candidates[0].blocked_at=='source_loading'
    portfolio=build_portfolio(critic)
    assert portfolio.counts['selected']==1 and portfolio.candidates[0].candidate_id=='D1'


def test_dispatcher_preserves_unrelated_selection_when_repairs_fail(isolated,monkeypatch):
    import critic.refinement_loop as module
    from critic.repair_routes import RepairRound
    from opportunities.miner import digest
    failures=[('D0',h.hypothesis_id) for h in isolated[1].directions[0].proposal.hypotheses]
    assessed=assess(isolated,IsolatedChat(failures))
    critic=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(assessed))
    calls=[]
    async def still_blocked(core,current):
        calls.append(current)
        return RepairRound(parent_sha256=digest(current.model_dump()),evidence_repairs=[],failures=['D0: provider failure'],critic=current)
    monkeypatch.setattr(module,'repair_round',still_blocked)
    result=asyncio.run(RefinementLoop(PaperStore(isolated[0]),CriticChat(),max_cycles=1).run(critic))
    portfolio=build_portfolio(result)
    assert len(calls)==1
    assert portfolio.status=='partial' and portfolio.counts['selected']==1
    assert portfolio.candidates[0].candidate_id=='D1'


def test_real_repair_dispatch_retries_failed_direction_only(isolated):
    from critic.repair_routes import repair_round, validate_rounds
    failures=[('D0',h.hypothesis_id) for h in isolated[1].directions[0].proposal.hypotheses]
    assessed=assess(isolated,IsolatedChat(failures))
    old=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(assessed))
    author=IsolatedChat();scientist=CriticChat()
    async def router(**kw):
        task=json.loads(kw['messages'][1]['content'])['task']
        return await (author if 'novelty' in task else scientist)(**kw)
    core=ResearchCritic(PaperStore(isolated[0]),router)
    repaired=asyncio.run(repair_round(core,old))
    assert validate_rounds(old,[repaired])==repaired.critic
    assert repaired.critic.candidates[1]==old.candidates[1]
    assert all(r['packet']['direction_id']=='D0' for r in author.requests)
    assert build_portfolio(repaired.critic).counts['selected']==2


def test_subset_requires_review_and_fresh_scientific_novelty(isolated):
    from directions.subset import revise_subset, SubsetRevision
    from directions.revision import revised_generation
    from novelty.revalidation import assess_reuse
    from tests.test_direction_generator import link_checks
    root,g,_,_=isolated
    original=g.directions[0]
    retained=[original.proposal.hypotheses[1].hypothesis_id]
    requests=[]
    async def router(**kw):
        req=json.loads(kw['messages'][1]['content']);requests.append(req['task'])
        if req['task']=='narrow_direction':
            out=deepcopy(req['original'])
            out['hypotheses']=out['hypotheses'][1:]
            out['experiments']=out['experiments'][1:]
            out['experiments'][0]['stage']='initial'
        else:
            assert req['task']=='review_subset_direction'
            out=dict(decision='pass',summary='Independent narrowed scope is coherent.',issues=[],test_link_checks=link_checks(req['candidate']))
        return ChatResult(json.dumps(out),'stop','fixture')
    core=ResearchCritic(PaperStore(root),router)
    revision=asyncio.run(revise_subset(core.io,original,retained))
    assert revision.proposal is not None, revision.diagnostic
    assert requests==['narrow_direction','review_subset_direction']
    # The fixture duplicates one opportunity; a real generation has one direction per opportunity.
    parent=g.model_copy(update={'directions':[original]},deep=True)
    revised=revised_generation(parent,[revision])
    assessed=assess(isolated,IsolatedChat())
    critic=asyncio.run(ResearchCritic(PaperStore(root),CriticChat()).run(assessed))
    certificate=asyncio.run(assess_reuse(core,critic,revised,revision))
    assert certificate.classification=='scientific_change' and certificate.decision=='recheck'
    altered=revision.model_dump()
    altered['attempts'][-1]['review']['decision']='revise'
    with pytest.raises(ValueError): SubsetRevision.model_validate(altered)


def test_accepted_scientific_narrowing_routes_to_subset_revision(isolated,monkeypatch):
    import critic.refinement_loop as module
    from critic.repair_routes import RepairRound
    from directions.subset import SubsetRevision
    from opportunities.miner import digest
    hid=isolated[1].directions[0].proposal.hypotheses[1].hypothesis_id
    class NarrowChat(IsolatedChat):
        def synthesis(self,out,req):
            super().synthesis(out,req)
            if req['task'].startswith('review') or 'locked_hypotheses' not in req['packet'] or req['packet']['direction_id']!='D0': return
            out['refinement']['scientific_edits']=[dict(field_path='/research_direction',proposed_value=req['packet']['candidate']['research_direction']+' Narrow scientific scope.',reason='Remove dependence on pending hypothesis.')]
    assessed=assess(isolated,NarrowChat([('D0',hid)]))
    old=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(assessed))
    assert old.candidates[0].blocked_at=='upstream'
    calls=[]
    async def unchanged(core,current):
        return RepairRound(parent_sha256=digest(current.model_dump()),evidence_repairs=[],failures=[],critic=current)
    async def pending(io,direction,retained):
        calls.append((direction.direction_id,retained))
        return SubsetRevision(original=direction,original_sha256=digest(direction.model_dump()),retained_hypothesis_ids=retained,support_context=None,attempts=[],proposal=None,diagnostic='Scientific review remains pending.')
    monkeypatch.setattr(module,'repair_round',unchanged)
    monkeypatch.setattr(module,'revise_subset',pending)
    result=asyncio.run(RefinementLoop(PaperStore(isolated[0]),CriticChat(),max_cycles=1).run(old))
    assert calls==[('D0',[isolated[1].directions[0].proposal.hypotheses[0].hypothesis_id])]
    assert build_portfolio(result).counts['selected']==1
