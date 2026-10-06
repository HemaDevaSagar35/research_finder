"""Limitation attribution survives projection, aggregation, drafting and review."""
import asyncio
import copy
import json

import numpy as np
import pytest
from pydantic import ValidationError

from landscape import aggregation
from landscape.paper_context import _project
from landscape.schemas import Landscape, LimitationSource
from reasoning.cross_paper import CrossPaperReasoner, Budgets
from reasoning.evidence import PaperStore
from reasoning.fake import FakeChat, cooperative_review
from indexing.index_contract import synthetic_paper
from reasoning.fixtures import build_demo_corpus, _loc


def cards_for_test():
    def paper(author, inferred):
        return {'paper_metadata': {'title': 'Example'},
                'limitations': {'author_stated': author, 'inferred': inferred}}
    a, _ = _project('a', paper(
        [{'limitation': ''}, {'limitation': 'Only one device is tested.', 'source_locations': [_loc(2)]}], []))
    b, _ = _project('b', paper([], [
        {'limitation': 'Only one device is tested.', 'source_locations': [_loc(3)]}]))
    return {'a': a, 'b': b}


def test_projection_preserves_original_indices_and_missing_pages():
    cards = cards_for_test()
    a, b = cards['a'].limitation_sources[0], cards['b'].limitation_sources[0]
    assert (a.origin, a.value_path, a.source_locations[0].page) == (
        'author_stated', '/limitations/author_stated/1', 2)
    assert (b.origin, b.value_path, b.source_locations[0].page) == (
        'model_inferred', '/limitations/inferred/0', 3)
    card, _ = _project('c', {'limitations': {'inferred': [{'limitation': 'Unclear generalization.'}]}})
    assert card.limitation_sources[0].source_locations == []


def test_legacy_card_has_known_category_without_invented_path():
    cards = cards_for_test()
    cards = {pid: card.model_copy(update={'limitation_sources': []}) for pid,card in cards.items()}
    sources = [s.source for s in aggregation._collect_statements(cards) if s.kind == 'limitation']
    assert {s.origin for s in sources} == {'author_stated', 'model_inferred'}
    assert all(s.value_path is None and s.source_locations == [] for s in sources)


@pytest.mark.parametrize('path', ['/limitations/inferred/0', '/limitations/author_stated/not-an-index'])
def test_origin_cannot_disagree_with_path(path):
    with pytest.raises(ValidationError):
        LimitationSource(paper_id='a', origin='author_stated', value_path=path, statement='x')


def test_mixed_aggregation_retains_sources_and_roundtrips(monkeypatch):
    prompts = []
    class Client:
        async def chat(self, prompt, **kw):
            prompts.append(prompt)
            return json.dumps({'statement': 'Evaluation appears limited to one device.'})
    monkeypatch.setattr(aggregation, 'embed_texts', lambda texts,*args: np.ones((len(texts),2)))
    _, items, _ = asyncio.run(aggregation.aggregate_findings(cards_for_test(), client=Client()))
    assert len(items) == 1
    item = items[0]
    assert '[author_stated]' in prompts[0] and '[model_inferred]' in prompts[0]
    assert {(s.paper_id,s.origin,s.value_path) for s in item.limitation_sources} == {
        ('a','author_stated','/limitations/author_stated/1'),
        ('b','model_inferred','/limitations/inferred/0')}
    land = Landscape(topic='coverage',paper_ids=['a','b'],recurring_limitations=items)
    assert Landscape.model_validate_json(land.model_dump_json()) == land
    bad = land.model_dump()
    bad['recurring_limitations'][0]['limitation_sources'][0]['paper_id'] = 'unknown'
    with pytest.raises(ValidationError):
        Landscape.model_validate(bad)


@pytest.mark.parametrize("overclaim", [False, True])
def test_attribution_reaches_draft_review_and_accepted_evidence(tmp_path, overclaim):
    root = tmp_path / 'corpus'
    _, ids = build_demo_corpus(root)
    pid = ids['P1']
    paper = json.loads((root / pid / 'paper.json').read_text())
    # Both origins use the same paper/page so this exercises attribution,
    # independently of cross-paper acceptance requirements.
    source_record = {'limitation': 'Evaluation covers a single hardware setup.',
                     'source_locations': [_loc(2)]}
    original, _ = synthetic_paper()
    for section in ['author_stated','inferred']:
        record=copy.deepcopy(original['limitations'][section][0])
        record.update(source_record)
        paper['limitations'][section]=[record]
    (root / pid / 'paper.json').write_text(json.dumps(paper))
    card, _ = _project(pid,paper)
    land = Landscape(topic='coverage',paper_ids=[pid],recurring_limitations=[{
        'statement':'Evaluation scope is limited.','supporting_papers':[pid],
        'limitation_sources':[s.model_dump() for s in card.limitation_sources]}])
    observed = {'draft':False,'review':False}
    def drafting(payload):
        assert {s['origin'] for s in payload['limitation_sources']} == {'author_stated','model_inferred'}
        evs = [e for e in payload['evidence'] if e['origin'] is not None]
        assert {e['origin'] for e in evs} == {'author_stated','model_inferred'}
        observed['draft']=True
        return {'thread_id':payload['thread_id'],'outcome':'candidates','comparability_notes':[],
                'tensions':[], 'findings':[{'statement':('Authors explicitly admit the limitation.' if overclaim else
                              'Evaluation appears limited to one setup.'),
                'finding_kind':'qualifies','conditions':[],
                'evidence_ids':[e['evidence_id'] for e in evs],
                'stance_by_evidence':{e['evidence_id']:'qualifies' for e in evs}}]}
    def reviewing(payload):
        evs=payload['candidates'][0]['payload']['evidence']
        assert {e['origin'] for e in evs} == {'author_stated','model_inferred'}
        observed['review']=True
        if overclaim:
            return {'decisions':[{'candidate_id':payload['candidates'][0]['candidate_id'],
                'decision':'reject','notes':'Inferred limitation is not an explicit author admission.',
                'page_ids':[p['page_id'] for p in payload['pages']]}]}
        return cooperative_review(payload)
    reasoner=CrossPaperReasoner(PaperStore(root),FakeChat(draft_fn=drafting,review_fn=reviewing),
                               budgets=Budgets(max_threads=1,max_calls=2))
    out=asyncio.run(reasoner.run(land))
    assert all(observed.values())
    if overclaim:
        assert not out.observations
        assert out.diagnostics[0].reason == 'rejected_on_review'
    else:
        assert len(out.observations)==1
        assert {e.origin for e in out.observations[0].evidence} == {'author_stated','model_inferred'}
