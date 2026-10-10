import asyncio
from copy import deepcopy
import pytest
from novelty.assessment import NoveltyAssessor, AssessmentSettings
from novelty.assessment_schemas import NoveltyAssessmentResult
from novelty.assessment_evidence import build_packet, hypothesis_packet
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready
from tests.test_novelty_assessment import bundle, Chat


@pytest.mark.parametrize('accepted,passes', [(15,False),(16,True),(19,True),(20,True)])
def test_coverage_boundary_preserves_missing_papers(bundle, accepted, passes):
    bundle.comparison_coverage_threshold=.8
    candidate=bundle.comparisons.candidates[0]
    target=next(iter(candidate.shortlist))
    original=candidate.papers[0]
    candidate.papers=[]
    ids=[]
    for i in range(20):
        paper=deepcopy(original)
        paper.paper_id=f'paper{i}'
        if i>=accepted:
            paper.status='failed'
            paper.diagnostic='Invalid citation'
        candidate.papers.append(paper)
        ids.append(paper.paper_id)
    candidate.shortlist={tid:ids for tid in candidate.shortlist}
    packet=build_packet(bundle,candidate.direction_id)
    row=next(r for r in packet['coverage'] if r['target_id']==target)
    assert row['complete'] is passes
    assert len(row['papers'])==20
    assert sum(r['status']=='failed' for r in row['papers'])==20-accepted
    assert all(r['reasons']==['Invalid citation'] for r in row['papers'] if r['status']=='failed')


def test_individual_assessments_are_reviewed_and_persisted(bundle):
    chat=Chat()
    result=asyncio.run(NoveltyAssessor(chat,settings=AssessmentSettings()).run(bundle.generation,bundle.search,bundle.comparisons))
    result=NoveltyAssessmentResult.model_validate_json(result.model_dump_json())
    c=result.candidates[0]
    assert c.assessment
    assert len(c.hypothesis_assessments)==len(bundle.generation.directions[0].proposal.hypotheses)
    assert all(h.assessment and h.reviews[-1].report.decision=='pass' for h in c.hypothesis_assessments)
    scoped=[req for req,kw in chat.calls if len(req['packet']['coverage'])==1]
    assert len(scoped)==2*len(c.hypothesis_assessments)
    assert all({e['target_id'] for e in req['packet']['evidence']}=={req['packet']['coverage'][0]['target_id']} for req in scoped)
    synthesis=[req for req,kw in chat.calls if 'reviewed_hypotheses' in req]
    assert len(synthesis)==1
    data=result.model_dump()
    data['candidates'][0]['hypothesis_assessments'][0]['assessment']['targets'][0]['reasoning']='Unreviewed change'
    with pytest.raises(ValueError,match='independent acceptance'):
        NoveltyAssessmentResult.model_validate(data)


def test_partial_retrieval_still_blocks(bundle):
    bundle.comparison_coverage_threshold=.8
    c=bundle.comparisons.candidates[0]
    tid=next(iter(c.retrieval_status))
    c.retrieval_status[tid]='partial'
    row=next(r for r in build_packet(bundle,c.direction_id)['coverage'] if r['target_id']==tid)
    assert not row['complete']
    assert 'Retrieval is partial.' in row['blockers']


def test_recovery_loader_preserves_saved_comparisons(bundle, tmp_path, monkeypatch):
    import json
    import research.recover as recovery
    from opportunities.miner import digest
    from research.pipeline import PipelineConfig
    from novelty.assessment_schemas import AssessmentInputs
    base=tmp_path/'original'
    base.mkdir()
    config=PipelineConfig(query='fixture')
    manifest={'config':config.model_dump(mode='json')}
    (base/'manifest.json').write_text(json.dumps(manifest))
    schemas={k: type(v) for k,v in [('directions',bundle.generation),('novelty_search',bundle.search),('comparison',bundle.comparisons)]}
    monkeypatch.setattr(recovery,'STAGES',schemas)
    parent=digest(manifest)
    for stage,value in [('directions',bundle.generation),('novelty_search',bundle.search),('comparison',bundle.comparisons)]:
        data=value.model_dump(mode='json')
        wrapper=dict(stage=stage,parent_sha256=parent,result=data,result_sha256=digest(data))
        (base/f'{stage}.json').write_text(json.dumps(wrapper))
        parent=digest(wrapper)
    root=tmp_path/'recovery'
    root.mkdir()
    (root/'recovery_manifest.json').write_text(json.dumps(dict(source=str(base),source_manifest_sha256=digest(manifest),config=config.model_dump(mode='json'),rounds=2)))
    for i in (1,2):
        for name,value in [('novelty_search',bundle.search),('comparison',bundle.comparisons)]:
            data=value.model_dump(mode='json')
            (root/f'{name}_recovery_{i}.json').write_text(json.dumps(dict(result=data,result_sha256=digest(data))))
    loaded,state,_=recovery.load_source(root)
    assert state['comparison']==bundle.comparisons
    assert loaded.assessment.comparison_coverage_threshold==.8
    path=root/'comparison_recovery_2.json'
    data=json.loads(path.read_text())
    data['result_sha256']='tampered'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Invalid recovery checkpoint'):
        recovery.load_source(root)


def test_accepted_hypothesis_survives_withheld_direction(bundle, ready):
    from critic.hypotheses import eligibility
    def fail_direction_review(out, req):
        if 'review_novelty_assessment' in req['task'] and len(req['packet']['coverage'])>1:
            out.update(decision='abstain',summary='Direction synthesis unresolved.')
    result=asyncio.run(NoveltyAssessor(Chat(fail_direction_review),settings=AssessmentSettings()).run(bundle.generation,bundle.search,bundle.comparisons))
    candidate=result.candidates[0]
    assert candidate.assessment is None
    direction=bundle.generation.directions[0]
    for h in direction.proposal.hypotheses:
        assert eligibility(candidate,direction,h.hypothesis_id) is None
        outcome=next(o for o in candidate.outcomes() if o['target_id']==h.hypothesis_id)
        assert outcome['finding']=='PARTIAL_OVERLAP'

    from critic.hypotheses import review_hypotheses
    from reasoning.evidence import PaperStore
    from tests.test_research_critic import Chat as CriticChat
    def narrow(out, req):
        scope=req['packet'].get('review_target_ids')
        if scope:
            for key in ('targets','target_checks'):
                if key in out: out[key]=[t for t in out[key] if t['target_id'] in scope]
            if 'test_link_checks' in out:
                out['test_link_checks']=[t for t in out['test_link_checks'] if t['hypothesis_id'] in scope]
    review=asyncio.run(review_hypotheses(result,PaperStore(ready[0]),chat=CriticChat(mutate=narrow)))
    assert all(h['status']=='approved' for h in review['hypotheses'])
