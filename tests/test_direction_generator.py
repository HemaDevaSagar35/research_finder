"""Offline contract/evidence/async checks, not a judge of scientific value."""
import asyncio
import json

import httpx
import openai
import pytest

from directions.generator import DirectionGenerator, Settings
from directions.schemas import GenerationResult
from llm_client import ChatResult
from opportunities.miner import OpportunityMiner, digest
from opportunities.schemas import MiningResult
from reasoning.evidence import PaperStore
from tools.opportunity_validation_cases import fixture


@pytest.fixture
def inputs(tmp_path):
    land, reason, candidate = fixture(tmp_path, 'mechanistic_interaction')

    async def review(**kw):
        p = json.loads(kw['messages'][1]['content'])['payload']
        response = {'candidate_id': p['candidate_id'], 'decision': 'accept',
            'notes': 'Synthetic stub acceptance.', 'page_ids': [x['page_id'] for x in p['pages']],
            'factual_status': 'supported', 'required_corrections': [],
            'paper_assessments': [{'paper_id': pid, 'role': 'supporting', 'rationale': 'Fixture.',
                'evidence_ids': [e['evidence_id'] for e in p['evidence'] if e['paper_id'] == pid],
                'page_ids': [page['page_id'] for page in p['pages'] if page['paper_id'] == pid]}
                for pid in land.paper_ids]}
        return ChatResult(json.dumps(response), 'stop', 'stub')

    miner = OpportunityMiner(PaperStore(tmp_path), review)
    _, _, sources, evidence = miner._inputs(land, reason)
    op = asyncio.run(miner._review('op-0', candidate, sources, evidence))
    result = MiningResult(topic=land.topic, landscape_ref=reason.landscape_ref,
        reasoning_ref={'schema_version': reason.schema_version, 'sha256': digest(reason.model_dump())},
        opportunities=[op], diagnostics=[], coverage={}, calls={}, usage={}, run={})
    return tmp_path, land, reason, result


def response(payload):
    op = payload['opportunity']
    eids = [e['evidence_id'] for e in op['evidence']]
    pids = [p['page_id'] for p in payload['pages']]
    hypothesis = {'hypothesis_id': 'H1', 'condition': 'Model M, workload W, batch 1.',
        'intervention': 'Enable compression with expert caching.', 'expected_effect': 'Lower cache misses.',
        'mechanism': 'More concentrated routing may increase expert reuse.',
        'assumptions': ['Entropy reduction persists with caching enabled.'],
        'falsification_criterion': 'No hit-rate gain under controlled paired trials.', 'evidence_ids': eids}
    experiment = {'experiment_id': 'E1', 'stage': 'initial', 'hypothesis_ids': ['H1', 'H2'], 'depends_on': [],
        'objective': 'Test whether the proposed interaction changes cache hit rate.',
        'setup': 'Replay fixed routing traces before attempting an integrated implementation.',
        'intervention': 'Compare compressed versus uncompressed traces.', 'baselines': ['Same cache without compression.'],
        'metrics': ['Hit rate', 'Replay latency'], 'controls': ['Identical cache capacity and trace length.'],
        'resource_requirements': 'One CPU trace-replay process; availability unknown.',
        'cost_rationale': 'Trace replay avoids initial integration and GPU-serving costs.',
        'informative_outcomes': 'A gain motivates integration; no gain challenges the proposed locality mechanism.'}
    return {'direction': {'title': 'Compression-aware expert caching',
        'research_direction': 'Study whether routing changes can guide cache policy.',
        'rationale': [{'statement': 'Separate reports connect compression, entropy and caching.',
                       'evidence_ids': eids, 'page_ids': pids}],
        'proposed_mechanism': 'Compression may improve cache locality through concentrated routing.',
        'scope': op['candidate']['scope'], 'assumptions': ['Both components can be combined.'],
        'hypotheses': [hypothesis, {**hypothesis, 'hypothesis_id': 'H2', 'expected_effect': 'Lower replay latency.'}],
        'experiments': [experiment, {**experiment, 'experiment_id': 'E2', 'stage': 'followup',
            'depends_on': ['E1'], 'setup': 'Integrated serving benchmark.',
            'cost_rationale': 'Integration is costlier and depends on the initial locality result.'}],
        'risks': ['Compression overhead may exceed the cache benefit.'],
        'uncertainties': ['The joint effect has not been demonstrated.'],
        'what_would_falsify_it': 'No locality benefit or losses exceeding saved transfers.'}, 'abstention_reason': None}


class Chat:
    def __init__(self, mutate=None, finish='stop'):
        self.mutate, self.finish = mutate, finish
        self.calls = []
        self.active = self.peak = 0

    async def __call__(self, **kw):
        self.calls.append(kw)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        p = json.loads(kw['messages'][1]['content'])['payload']
        out = response(p)
        if self.mutate:
            self.mutate(out, p)
        return ChatResult(json.dumps(out), self.finish, 'stub')


def run(inputs, chat=None, **settings):
    root, land, reason, mining = inputs
    return asyncio.run(DirectionGenerator(PaperStore(root), chat or Chat(), settings=Settings(**settings)).run(land, reason, mining))


def test_complete_handoff_real_pages_and_many_to_many_experiments(inputs):
    chat = Chat()
    result = run(inputs, chat)
    assert not result.diagnostics
    result = GenerationResult.model_validate_json(result.model_dump_json())
    d = result.directions[0]
    assert result.opportunities_ref['sha256'] == digest(inputs[3].model_dump())
    assert d.opportunity == inputs[3].opportunities[0]
    assert d.hypothesis_status == 'proposed_untested'
    assert d.scientific_review == d.literature_novelty == 'not_assessed'
    assert d.evidence_validation == 'references_and_artifacts_checked'
    assert d.recommended_next_experiment_id == 'dir-000-e01'
    assert d.proposal.experiments[0].hypothesis_ids == ['dir-000-h01', 'dir-000-h02']
    assert d.proposal.experiments[1].depends_on == ['dir-000-e01']
    payload = json.loads(chat.calls[0]['messages'][1]['content'])['payload']
    assert payload['landscape'] == inputs[1].model_dump()
    assert len(payload['cross_paper_sources']) == 2
    for page in payload['pages']:
        assert page['text'] == (inputs[0]/page['paper_id']/'01.md').read_text()
    assert set(d.paper_artifact_hashes) == set(inputs[1].paper_ids)
    assert result.calls['generation'] == 1 and result.calls['total'] == 1


@pytest.mark.parametrize('target', ['landscape', 'reasoning', 'topic'])
def test_lineage_mismatch_stops_before_api(inputs, target):
    if target == 'landscape': inputs[3].landscape_ref['sha256'] = 'wrong'
    elif target == 'reasoning': inputs[3].reasoning_ref['sha256'] = 'wrong'
    else: inputs[3].topic = 'other'
    chat = Chat()
    with pytest.raises(ValueError, match='lineage'):
        run(inputs, chat)
    assert not chat.calls


@pytest.mark.parametrize('change', ['evidence', 'roles', 'context_page', 'source', 'attribution'])
def test_invalid_upstream_contract_never_reaches_model(inputs, change):
    op = inputs[3].opportunities[0]
    if change == 'evidence': op.evidence[0].source_value = 'invented'
    elif change == 'roles': op.supporting_paper_ids = []
    elif change == 'context_page': op.review_sources.pop()
    elif change == 'source': op.sources[0].refines = ['missing']
    else: op.evidence[0].origin = 'author_stated'
    chat = Chat()
    result = run(inputs, chat)
    assert not chat.calls and result.diagnostics[0].reason == 'invalid_source'


@pytest.mark.parametrize('change,expected', [('page', 'stale_evidence'), ('paper', 'stale_evidence'),
    ('missing_page', 'missing_pages'), ('missing_paper', 'missing_artifact')])
def test_changed_or_missing_original_evidence(inputs, change, expected):
    root, land, _, _ = inputs
    folder = root/land.paper_ids[0]
    if change == 'page': (folder/'01.md').write_text('changed page')
    elif change == 'paper':
        p = folder/'paper.json'; p.write_text(p.read_text()+'\n')
    elif change == 'missing_page': (folder/'01.md').unlink()
    else: (folder/'paper.json').unlink()
    chat = Chat()
    result = run(inputs, chat)
    assert not chat.calls and result.diagnostics[0].reason == expected


@pytest.mark.parametrize('mutation', ['unknown_evidence', 'unknown_page', 'wrong_paper_page',
    'hypothesis_ref', 'unknown_hypothesis', 'uncovered_hypothesis', 'duplicate_id', 'cycle', 'initial_order', 'novelty'])
def test_invalid_generated_contract_is_not_promoted(inputs, mutation):
    def change(out, payload):
        d = out['direction']
        if mutation == 'unknown_evidence': d['rationale'][0]['evidence_ids'] = ['missing']
        elif mutation == 'unknown_page': d['rationale'][0]['page_ids'] = ['missing#p1']
        elif mutation == 'wrong_paper_page': d['rationale'][0]['page_ids'] = d['rationale'][0]['page_ids'][:1]
        elif mutation == 'hypothesis_ref': d['hypotheses'][0]['evidence_ids'] = ['missing']
        elif mutation == 'unknown_hypothesis': d['experiments'][0]['hypothesis_ids'] = ['missing']
        elif mutation == 'uncovered_hypothesis':
            for e in d['experiments']: e['hypothesis_ids'] = ['H1']
        elif mutation == 'duplicate_id': d['hypotheses'][1]['hypothesis_id'] = 'H1'
        elif mutation == 'cycle': d['experiments'][1]['depends_on'] = ['E2']
        elif mutation == 'initial_order': d['experiments'].reverse()
        else: d['novelty_status'] = 'novel'
    chat = Chat(change)
    result = run(inputs, chat, repair_rounds=0)
    assert not result.directions and result.diagnostics[0].reason == 'invalid_generation'


def test_bounded_repair_and_truncation(inputs):
    chat = Chat(finish='length')
    result = run(inputs, chat)
    assert result.diagnostics[0].reason == 'invalid_generation'
    assert result.calls == {'generation': 1, 'repair': 1, 'total': 2, 'budget': 40}


def test_valid_reference_repair(inputs):
    chat = Chat()
    def change(out, payload):
        if len(chat.calls) == 1: out['direction']['rationale'][0]['page_ids'] = ['bad']
    chat.mutate = change
    result = run(inputs, chat)
    assert len(result.directions) == 1 and result.calls['repair'] == 1


@pytest.mark.parametrize('settings,reason', [({'max_input_tokens': 1}, 'input_budget'),
    ({'max_pages': 1}, 'page_budget'), ({'max_calls': 0}, 'call_budget')])
def test_pre_call_limits(inputs, settings, reason):
    chat = Chat()
    result = run(inputs, chat, **settings)
    assert not chat.calls and result.diagnostics[0].reason == reason


def test_abstention_is_not_repaired(inputs):
    def abstain(out, payload):
        out.update(direction=None, abstention_reason='Cannot form a concrete mechanism from this evidence.')
    chat = Chat(abstain)
    result = run(inputs, chat)
    assert result.diagnostics[0].reason == 'abstained' and len(chat.calls) == 1


def test_empty_accepted_set_makes_no_calls(inputs):
    inputs[3].opportunities = []
    chat = Chat()
    result = run(inputs, chat)
    assert result.diagnostics[0].reason == 'no_opportunities' and not chat.calls


def multiply(inputs, count):
    original = inputs[3].opportunities[0]
    inputs[3].opportunities = [original.model_copy(update={'opportunity_id': f'op-{i}'}, deep=True) for i in range(count)]


def test_async_independent_directions_and_deterministic_ids(inputs):
    multiply(inputs, 4)
    chat = Chat()
    result = run(inputs, chat, concurrency=2)
    assert chat.peak == 2
    assert [d.direction_id for d in result.directions] == [f'dir-{i:03d}' for i in range(4)]
    assert len({h.hypothesis_id for d in result.directions for h in d.proposal.hypotheses}) == 8


def test_atomic_call_budget_and_partial_result(inputs):
    multiply(inputs, 4)
    chat = Chat()
    result = run(inputs, chat, max_calls=2)
    assert len(result.directions) == len(chat.calls) == 2
    assert [d.reason for d in result.diagnostics] == ['call_budget', 'call_budget']


def test_fatal_provider_rejection_stops_queued_calls(inputs):
    multiply(inputs, 3)
    calls = []
    async def rejected(**kw):
        calls.append(kw)
        raise openai.AuthenticationError('Invalid API key', response=httpx.Response(401,
            request=httpx.Request('POST','https://example.test')), body={'error': {'message': 'invalid api key'}})
    result = run(inputs, rejected, concurrency=1)
    assert len(calls) == 1 and result.run['stopped_for_provider_error']
    assert all(d.reason == 'provider_error' for d in result.diagnostics)


def test_one_instance_per_run(inputs):
    root, land, reasoning, mining = inputs
    generator = DirectionGenerator(PaperStore(root), Chat())
    asyncio.run(generator.run(land, reasoning, mining))
    with pytest.raises(ValueError, match='new generator'):
        asyncio.run(generator.run(land, reasoning, mining))


def test_direction_environment_and_explicit_allowances(monkeypatch):
    monkeypatch.setenv('DIRECTION_MAX_INPUT_TOKENS', '500000')
    monkeypatch.setenv('DIRECTION_MAX_OUTPUT_TOKENS', '500000')
    assert Settings().max_input_tokens == Settings().max_output_tokens == 500000
    assert Settings(max_output_tokens=1000).max_output_tokens == 1000


def test_context_paper_role_preserved_and_cannot_alone_ground_direction(inputs):
    op = inputs[3].opportunities[0]
    context = op.paper_ids[1]
    op.supporting_paper_ids = [op.paper_ids[0]]
    op.context_paper_ids = [context]
    op.support = 'single_paper'
    next(a for a in op.paper_assessments if a.paper_id == context).role = 'context_only'
    good = run(inputs)
    assert good.directions[0].opportunity.context_paper_ids == [context]
    assert len(good.directions[0].context_pages) == 2
    def only_context(out, payload):
        out['direction']['rationale'][0]['evidence_ids'] = [e['evidence_id'] for e in payload['opportunity']['evidence'] if e['paper_id'] == context]
    bad = run(inputs, Chat(only_context), repair_rounds=0)
    assert bad.diagnostics[0].reason == 'invalid_generation'


@pytest.mark.parametrize('field', ['max_hypotheses', 'max_experiments'])
def test_generation_limits_not_silently_trimmed(inputs, field):
    result = run(inputs, **{field: 1, 'repair_rounds': 0})
    assert not result.directions and result.diagnostics[0].reason == 'invalid_generation'


def test_owned_client_closed_and_transient_failure_preserves_other_directions(inputs, monkeypatch):
    from directions import generator as module
    multiply(inputs, 2)
    created = []
    class Client:
        provider = 'stub'
        default_model = 'stub'
        def __init__(self, *args, **kw):
            self.raw = self
            self.closed = False
            self.chat = Chat()
            created.append(self)
        async def chat_result(self, **kw):
            payload = json.loads(kw['messages'][1]['content'])['payload']
            if payload['opportunity']['opportunity_id'] == 'op-0':
                raise RuntimeError('Transient transport error')
            return await self.chat(**kw)
        async def close(self):
            self.closed = True
    monkeypatch.setattr(module, 'AsyncLLMClient', Client)
    root, land, reasoning, mining = inputs
    generator = DirectionGenerator(PaperStore(root))
    result = asyncio.run(generator.run(land, reasoning, mining))
    assert len(created) == 1 and created[0].closed
    assert len(result.directions) == 1 and result.directions[0].opportunity_id == 'op-1'
    assert result.diagnostics[0].reason == 'call_failed'
    assert not result.run['stopped_for_provider_error']


def test_cross_paper_context_does_not_offer_unselected_citation_ids(inputs):
    root, land, reasoning, mining = inputs
    source = reasoning.observations[0]
    source.evidence.append(source.evidence[0].model_copy(update={'evidence_id': 'unselected-record'}))
    mining.reasoning_ref['sha256'] = digest(reasoning.model_dump())
    chat = Chat()
    result = run(inputs, chat)
    assert len(result.directions) == 1
    p = json.loads(chat.calls[0]['messages'][1]['content'])['payload']
    assert 'unselected-record' not in json.dumps(p['cross_paper_sources'])
    assert set(p['citable_evidence_ids']) == set(mining.opportunities[0].candidate.evidence_ids)
    assert all(set(s['content']['selected_evidence_ids']) <= set(p['citable_evidence_ids']) for s in p['cross_paper_sources'])
