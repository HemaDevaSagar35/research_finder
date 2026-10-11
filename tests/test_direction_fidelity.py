"""Tension projection and evidence preservation; no claims about model judgment."""
import asyncio
import json

import pytest

from directions.generator import DirectionGenerator, Settings
from llm_client import ChatResult
from reasoning.evidence import PaperStore
from tools.direction_validation_cases import CASE_NAMES, build_case


@pytest.mark.parametrize('name', CASE_NAMES)
def test_control_context_preserves_gap_pages_and_allowed_references(tmp_path, name):
    land, reasoning, mining, _ = build_case(tmp_path, name)
    op = mining.opportunities[0]
    captured = []
    async def chat(**kw):
        captured.append(json.loads(kw['messages'][1]['content'])['payload'])
        return ChatResult(json.dumps({'direction': None, 'abstention_reason': 'Offline projection check only.'}), 'stop', 'stub')
    result = asyncio.run(DirectionGenerator(PaperStore(tmp_path), chat).run(land, reasoning, mining))
    assert result.diagnostics[0].reason == 'abstained'
    assert len(captured) == 1
    payload = captured[0]
    assert payload['opportunity'] == op.model_dump()
    assert payload['citable_evidence_ids'] == op.candidate.evidence_ids
    assert {p['paper_id'] for p in payload['pages']} == set(land.paper_ids)
    for page in payload['pages']:
        assert page['text'] == (tmp_path/page['paper_id']/'01.md').read_text()
    if name.startswith('tension'):
        source = payload['cross_paper_sources'][0]
        tension = reasoning.tensions[0]
        assert source['kind'] == 'tension'
        assert source['content']['sides'] == [s.assertion for s in tension.sides]
        assert source['content']['candidate_explanations'] == [
            {'text': e.text, 'hypothesis': e.hypothesis} for e in tension.candidate_explanations]
        assert source['content']['candidate_explanations'][0]['hypothesis'] is True
        assert len(set(source['content']['selected_evidence_ids'])) == 2
        assert '10 ms' in source['content']['sides'][0]
        assert '30 ms' in source['content']['sides'][1]
    if name == 'gap_with_distraction':
        assert any('UNRELATED' in i['statement'] for i in payload['landscape']['aggregated_findings'])
        assert 'p95 and p99' in payload['opportunity']['candidate']['question']


@pytest.mark.parametrize('damage', ['drop_page', 'change_page', 'drop_side_evidence', 'alter_side_evidence'])
def test_damaged_tension_context_cannot_reach_generation(tmp_path, damage):
    land, reasoning, mining, _ = build_case(tmp_path, 'tension_different_conditions')
    op = mining.opportunities[0]
    if damage == 'drop_page':
        op.review_sources.pop()
    elif damage == 'change_page':
        (tmp_path/land.paper_ids[1]/'01.md').write_text('Different conditions after review.')
    elif damage == 'drop_side_evidence':
        op.evidence.pop()
    else:
        op.evidence[1].source_value = 'Invented matched conditions.'
    async def chat(**kw):
        pytest.fail('Damaged context must not reach the API')
    result = asyncio.run(DirectionGenerator(PaperStore(tmp_path), chat, settings=Settings(repair_rounds=0)).run(land, reasoning, mining))
    assert not result.directions
    assert result.diagnostics[0].reason == ('stale_evidence' if damage == 'change_page' else 'invalid_source')


@pytest.mark.parametrize('kind', ['observation', 'finding'])
def test_condition_hypothesis_flags_survive_context_projection(tmp_path, kind):
    from opportunities.miner import digest
    from reasoning.schemas import Condition, CrossPaperFinding, Agreement, AgreementCounts

    land, reasoning, mining, _ = build_case(tmp_path, 'untested_interaction')
    item = reasoning.observations[0]
    item.conditions = [
        Condition(text='Compression was evaluated with caching disabled.',
                  evidence_ids=['e1'], hypothesis=False),
        Condition(text='Lower entropy may improve locality when caching is enabled.',
                  evidence_ids=['e1', 'unselected-context-record'], hypothesis=True)]
    item.evidence.append(item.evidence[0].model_copy(update={'evidence_id': 'unselected-context-record'}))
    if kind == 'finding':
        source = CrossPaperFinding(finding_id=item.observation_id, thread_id=item.thread_id,
            refines=item.refines, finding_kind='qualifies', statement=item.statement,
            conditions=item.conditions, evidence=item.evidence,
            agreement=Agreement(scope='thread', denominator=1, paper_ids=[item.evidence[0].paper_id],
                                counts=AgreementCounts(supports=1)),
            confidence='medium', review_sources=item.review_sources)
        reasoning.observations.pop(0)
        reasoning.findings.append(source)
        op = mining.opportunities[0]
        old = 'observation:' + item.observation_id
        new = 'finding:' + item.observation_id
        op.candidate.source_ids = [new if x == old else x for x in op.candidate.source_ids]
        op.candidate.evidence_ids = [x.replace(old + '/', new + '/') for x in op.candidate.evidence_ids]
        for ref in op.sources:
            if ref.source_id == old:
                ref.source_id, ref.kind = new, 'finding'
        for ev in op.evidence:
            ev.evidence_id = ev.evidence_id.replace(old + '/', new + '/')
        for assessment in op.paper_assessments:
            assessment.evidence_ids = [x.replace(old + '/', new + '/') for x in assessment.evidence_ids]
    mining.reasoning_ref = {'schema_version': reasoning.schema_version, 'sha256': digest(reasoning.model_dump())}
    captured = []
    async def chat(**kw):
        captured.append(json.loads(kw['messages'][1]['content'])['payload'])
        return ChatResult(json.dumps({'direction': None, 'abstention_reason': 'Offline context check.'}), 'stop', 'stub')
    result = asyncio.run(DirectionGenerator(PaperStore(tmp_path), chat).run(land, reasoning, mining))
    assert result.diagnostics[0].reason == 'abstained'
    payload = captured[0]
    projected = payload['cross_paper_sources'][0]
    expected_id = kind + ':' + item.observation_id + '/e1'
    assert projected['content']['conditions'] == [
        {'text': item.conditions[0].text, 'hypothesis': False, 'selected_evidence_ids': [expected_id]},
        {'text': item.conditions[1].text, 'hypothesis': True, 'selected_evidence_ids': [expected_id]}]
    assert all('unselected-context-record' not in eid for eid in payload['citable_evidence_ids'])
    assert payload['opportunity'] == mining.opportunities[0].model_dump()


@pytest.mark.parametrize('selector', ['moe:missing', 'unknown:op-1', 'moe:'])
def test_live_selection_fails_before_client_creation(tmp_path, monkeypatch, selector):
    from types import SimpleNamespace
    from tools import validate_direction_generator as harness
    land, reasoning, mining, _ = build_case(tmp_path/'papers', 'untested_interaction')
    for name, value in [('landscape', land), ('reasoning', reasoning), ('opportunities', mining)]:
        harness.save(tmp_path/'inputs'/'moe'/(name+'.json'), value.model_dump())
    def forbidden_client(*args, **kwargs):
        pytest.fail('Invalid selections must fail before any API client is opened')
    monkeypatch.setattr(harness, 'AsyncLLMClient', forbidden_client)
    args = SimpleNamespace(inputs=str(tmp_path/'inputs'), opportunity=[selector], out=str(tmp_path/'out'))
    with pytest.raises(ValueError):
        asyncio.run(harness.main(args))
    assert not (tmp_path/'out').exists()


def test_live_selection_retains_exact_opportunity_and_parent_lineage(tmp_path):
    from opportunities.miner import digest
    from tools import validate_direction_generator as harness
    land, reasoning, mining, _ = build_case(tmp_path/'papers', 'untested_interaction')
    wanted = mining.opportunities[0]
    mining.opportunities.append(wanted.model_copy(deep=True, update={'opportunity_id': 'other'}))
    for name, value in [('landscape', land), ('reasoning', reasoning), ('opportunities', mining)]:
        harness.save(tmp_path/'inputs'/'moe'/(name+'.json'), value.model_dump())
    jobs = harness.load_selected_inputs(tmp_path/'inputs', ['moe:'+wanted.opportunity_id])
    assert len(jobs) == 1
    name, saved_land, saved_reasoning, selected, parent = jobs[0]
    assert name == 'moe' and saved_land == land and saved_reasoning == reasoning
    assert selected.opportunities == [wanted]
    assert parent == digest(mining.model_dump())
    assert selected.landscape_ref == mining.landscape_ref
    assert selected.reasoning_ref == mining.reasoning_ref
    assert json.loads((tmp_path/'inputs'/'moe'/'opportunities.json').read_text()) == mining.model_dump()
