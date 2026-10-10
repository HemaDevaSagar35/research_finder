import asyncio
import pytest
from llm_client.sizing import fit_context, reduce_after_context_error
from novelty.comparison import NoveltyComparator
from novelty.pipeline import NoveltySearcher
from reasoning.evidence import PaperStore
from research.retrieval import MultiQueryRetriever
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared, Backend, run as search_run
from tests.test_novelty_comparison import ready, run as compare_run
from tests.test_novelty_assessment import bundle
from tests.test_research_critic import source, Chat
from critic.hypotheses import review_hypotheses


def test_context_sizing_keeps_evidence(monkeypatch):
    monkeypatch.setattr('reasoning.budget.estimate_tokens',lambda text:748376)
    p={'messages':[{'role':'user','content':'complete evidence'}],'max_tokens':393216}
    fit_context(p,{'context_token_limit':1048576})
    assert p['messages'][0]['content']=='complete evidence'
    assert 1024 <= p['max_tokens'] < 300000
    class Error(Exception): status_code=400
    p['max_tokens']=393216
    assert reduce_after_context_error(Error('maximum context length is 1048576 tokens. (748376 in the messages, 393216 in the completion)'),p)
    assert p['max_tokens']==292008
    assert not reduce_after_context_error(Error('invalid model'),p)


def test_context_too_large_is_explicit(monkeypatch):
    monkeypatch.setattr('reasoning.budget.estimate_tokens',lambda text:2000000)
    with pytest.raises(ValueError,match='context_budget'):
        fit_context({'messages':[],'max_tokens':500000},{'context_token_limit':1048576})


def test_recovery_reuses_complete_comparisons_without_calls(ready):
    old,_=compare_run(ready)
    async def forbidden(**kwargs): raise AssertionError('paid call should not occur')
    root,generation,search=ready
    new=asyncio.run(NoveltyComparator(PaperStore(root),forbidden).run(generation,search,recovery=old))
    assert new.candidates==old.candidates
    assert new.calls['total']==0


def test_recovery_reuses_searches_without_retrieval(prepared):
    old,_,_=search_run(prepared)
    root,generation=prepared
    backend=Backend(fail=True)
    async def forbidden(**kwargs): raise AssertionError('unexpected model call')
    async def exercise():
        async with MultiQueryRetriever(backend) as retriever:
            return await NoveltySearcher(PaperStore(root),retriever,forbidden).run(generation,recovery=old)
    new=asyncio.run(exercise())
    assert new.candidates==old.candidates
    assert backend.calls==[]


def test_independent_hypothesis_scope_does_not_require_other_targets(source):
    root,novelty=source
    def narrow(out,req):
        scope=req['packet'].get('review_target_ids')
        if scope:
            if 'targets' in out: out['targets']=[t for t in out['targets'] if t['target_id'] in scope]
            if 'target_checks' in out:
                out['target_checks']=[t for t in out['target_checks'] if t['target_id'] in scope]
                out['test_link_checks']=[t for t in out['test_link_checks'] if t['hypothesis_id'] in scope]
    result=asyncio.run(review_hypotheses(novelty,PaperStore(root),chat=Chat(mutate=narrow)))
    assert result['hypotheses']
    assert all(h['status']=='approved' for h in result['hypotheses'])
    assert all(len(h['critique']['targets'])==1 for h in result['hypotheses'])


def test_failed_comparison_is_retried(ready):
    from tests.test_novelty_comparison import Chat as ComparisonChat
    old,_=compare_run(ready,ComparisonChat(fail='compare_prior_work'))
    assert any(p.status!='complete' for c in old.candidates for p in c.papers)
    root,generation,search=ready
    chat=ComparisonChat()
    new=asyncio.run(NoveltyComparator(PaperStore(root),chat).run(generation,search,recovery=old))
    assert all(p.status=='complete' for c in new.candidates for p in c.papers)
    assert chat.calls


def test_independent_review_still_blocks_incomplete_target(source):
    from critic.hypotheses import eligibility
    root,novelty=source
    candidate=novelty.candidates[0].model_copy(deep=True)
    direction=novelty.inputs.generation.directions[0]
    hid=direction.proposal.hypotheses[0].hypothesis_id
    cov=next(c for c in candidate.coverage if c.target_id==hid)
    cov.complete=False
    cov.blockers=['failed prior paper']
    assert 'Incomplete' in eligibility(candidate,direction,hid)
    other=direction.proposal.hypotheses[1].hypothesis_id
    assert eligibility(candidate,direction,other) is None
