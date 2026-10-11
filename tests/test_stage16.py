import asyncio
from copy import deepcopy
import pytest
from tests.test_failure_isolation import isolated, inputs, assess, IsolatedChat, CriticChat
from reasoning.evidence import PaperStore
from critic.pipeline import ResearchCritic
from critic.hypotheses import review_hypotheses
from research.publication import stage16, markdown


def test_stage16_preserves_approved_hypotheses_and_pending_sibling(isolated, monkeypatch, tmp_path):
    hid=isolated[1].directions[0].proposal.hypotheses[1].hypothesis_id
    novelty=assess(isolated,IsolatedChat([('D0',hid)]))
    critic=asyncio.run(ResearchCritic(PaperStore(isolated[0]),CriticChat()).run(novelty))
    report=asyncio.run(review_hypotheses(novelty,PaperStore(isolated[0]),existing=critic,chat=CriticChat()))
    result=stage16(critic,report)
    assert result['counts']==dict(approved=3,pending=1,discarded=0)
    assert result['status']=='partial' and not result['automatic_revision']
    rendered=markdown(result)
    assert 'Supporting papers and evidence' in rendered
    assert 'exact saved passages' in rendered
    for h in result['hypotheses']:
        assert h['direction_title'] in rendered
        assert h['reading_context']['coverage']['target_id']==h['hypothesis_id']
        assert all(e['target_id']==h['hypothesis_id'] for e in h['reading_context']['prior_work'])
    pending=next(h for h in result['hypotheses'] if h['status']=='pending')
    assert pending['reading_context']['supporting_evidence']
    assert pending['direction_context']['research_direction']
    bad=deepcopy(report)
    pending=next(h for h in bad['hypotheses'] if h['status']=='pending')
    pending['status']='approved'
    with pytest.raises(ValueError): stage16(critic,bad)
    bad=deepcopy(report)
    bad['hypotheses'][0]['proposal']['condition']='Changed scientific proposal'
    with pytest.raises(ValueError,match='proposal changed'): stage16(critic,bad)

    # Checkpoint-only publication must never initialize the paid pipeline runner.
    import json
    from research import recover
    from research.pipeline import PipelineConfig
    source=tmp_path/'saved';source.mkdir()
    (source/'hypothesis_reviews.json').write_text(json.dumps(report))
    config=PipelineConfig(query='fixture',root=isolated[0],max_refinement_cycles=2)
    monkeypatch.setattr(recover,'load_source',lambda root:(config,{'critic':critic},{}))
    def forbidden(*args,**kwargs): raise AssertionError('No model/retrieval runner may be created')
    monkeypatch.setattr(recover,'StageRunner',forbidden)
    out=tmp_path/'published'
    summary=asyncio.run(recover.recover(source,out,start_at='portfolio'))
    assert summary['model_calls']==summary['retrieval_calls']==0
    assert summary['hypothesis_counts']['approved']==3
    assert (out/'stage16.md').exists()


def test_reading_report_links_and_status_folders(tmp_path):
    import json
    from research.reading_report import write_reading_report
    # Use a saved-style row with no review packet: reading context must stand alone.
    row=dict(hypothesis_id='D-h1',direction_id='D',direction_title='Direction',status='pending',
        direction_context=dict(research_direction='Study',opportunity=dict(rationale='Problem',question='Question?',scope='Scope',uncertainties=[])),
        proposal=dict(expected_effect='Prediction',condition='Setting',intervention='Change',mechanism='Reason',falsification_criterion='Failure',assumptions=[]),
        diagnostic='Missing review',critique=None,linked_experiments=[],
        reading_context=dict(novelty_judgments=[],supporting_evidence=[],missing_support_evidence_ids=[],prior_work=[],coverage_threshold=.8,
            coverage=dict(target_id='D-h1',papers=[],blockers=['No comparisons'],complete=False)))
    write_reading_report(tmp_path,dict(status='blocked',counts=dict(approved=0,pending=1,discarded=0),hypotheses=[row]))
    index=json.loads((tmp_path/'summary.json').read_text())
    for key in ('proposal_md','proposal_json','evidence_md'):
        assert (tmp_path/index['hypotheses'][0][key]).exists()
    text=(tmp_path/'proposals/pending/D-h1.md').read_text()
    assert '## Problem' in text and '## Hypothesis' in text and 'Missing review' in text
    assert '[the hypothesis summary](summary.md)' in (tmp_path/'stage16.md').read_text()


def test_reference_links_keep_proposal_and_evidence_anchors(tmp_path):
    import json,re
    from research.report_references import export_references
    (tmp_path/'proposals/approved').mkdir(parents=True)
    (tmp_path/'evidence').mkdir()
    (tmp_path/'summary.md').write_text('dir-001-h01')
    (tmp_path/'proposals/approved/dir-001-h01.md').write_text('observation:t001-o01 and C1')
    (tmp_path/'evidence/dir-001-h01.md').write_text('`/claims_and_evidence/0` and C2')
    h=dict(hypothesis_id='dir-001-h01',direction_id='dir-001',direction_title='Title',direction_context={},proposal={},status='approved',linked_experiments=[],reading_context=dict(supporting_evidence=[],prior_work=[],coverage=dict(papers=[])))
    export_references(tmp_path,dict(hypotheses=[h]))
    for f in [tmp_path/'summary.md',*tmp_path.glob('proposals/*/*.md'),*tmp_path.glob('evidence/*.md')]:
        for dest in re.findall(r'\]\(([^)]+)\)',f.read_text()):
            path,_,fragment=dest.partition('#')
            target=f.parent/path
            assert target.exists()
            if fragment: assert f'id="{fragment}"' in target.read_text()
    records=[json.loads(f.read_text()) for f in tmp_path.glob('evidence/sources/*.json')]
    assert any(r['availability'].startswith('source unavailable') for r in records)
