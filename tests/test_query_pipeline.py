"""One-query orchestration, actual downstream stages, restart and failure boundaries."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from research import pipeline as p
from research.__main__ import parser, configuration
from research.query_planner import QueryPlan
from research.retrieval import RetrievalResult, RetrievalFilters, RetrievedPaper, MultiQueryRetriever
from portfolio import FinalPortfolio
from tests.test_direction_generator import inputs, Chat as DirectionChat
from tests.test_novelty_search import prepared, Backend, Chat as SearchChat
from tests.test_novelty_comparison import ready, Chat as ComparisonChat
from tests.test_novelty_assessment import Chat as AssessmentChat
from tests.test_research_critic import Chat as CriticChat


@pytest.fixture
def wired(inputs, ready, monkeypatch):
    root, land, reason, mining = inputs
    # Scientific model responses are fake; downstream stage implementations,
    # schemas, review gates and all runner persistence are real.
    for name, chat in [('DirectionGenerator', DirectionChat), ('NoveltySearcher', SearchChat),
                       ('NoveltyComparator', ComparisonChat), ('NoveltyAssessor', AssessmentChat),
                       ('ResearchCritic', CriticChat)]:
        original = getattr(p, name)
        def construct(*args, _original=original, _chat=chat, **kwargs):
            return _original(*args, chat=_chat(), **kwargs)
        monkeypatch.setattr(p, name, construct)
    configs, calls, instances = [], [], []
    class Runner(p.StageRunner):
        fail = None
        empty = False
        def __init__(self, config):
            super().__init__(config)
            configs.append(config)
            self.retriever = MultiQueryRetriever(Backend())
            self.closed = False
            instances.append(self)
        async def execute(self, name, state):
            calls.append(name)
            if name == self.fail:
                raise RuntimeError('injected interruption')
            if name == 'plan':
                return QueryPlan(queries=[self.config.query])
            if name == 'retrieval':
                return RetrievalResult(queries=state['plan'].queries, filters=RetrievalFilters(), rrf_k=60,
                    papers=[] if self.empty else [RetrievedPaper(paper_id=pid, retrieval_score=1, matches=[], records=[])
                                               for pid in land.paper_ids])
            if name == 'landscape':
                assert set(state['contexts'].papers) == set(land.paper_ids)
                return land
            if name == 'reasoning':
                assert state['landscape'] == land
                return reason
            if name == 'opportunities':
                assert state['reasoning'] == reason
                return mining
            return await super().execute(name, state)
        async def close(self):
            await super().close()
            self.closed = True
    config = p.PipelineConfig(query=land.topic, root=root, allow_s3=False,
        max_refinement_cycles=0, selection={'min_directions': 1, 'max_directions': 5})
    return config, Runner, calls, instances


def test_query_to_real_downstream_and_resume_without_calls(wired, tmp_path):
    config, runner, calls, instances = wired
    out = tmp_path/'run'
    summary = asyncio.run(p.run_pipeline(config, out, runner_factory=runner))
    assert summary['status'] == 'ready'
    assert calls == [s for s in p.STAGES if s != 'refinement']
    assert all(i.closed for i in instances)
    result = FinalPortfolio.model_validate_json((out/'final_portfolio.json').read_text())
    assert result.candidates and result.topic == config.query
    before = (out/'final_portfolio.json').read_bytes()
    calls.clear()
    assert asyncio.run(p.run_pipeline(config, out, resume=True, runner_factory=runner)) == summary
    assert calls == []
    assert (out/'final_portfolio.json').read_bytes() == before
    assert (out/'final_portfolio.md').exists()


def test_interrupted_run_resumes_only_missing_suffix(wired, tmp_path):
    config, runner, calls, instances = wired
    out = tmp_path/'run'
    runner.fail = 'comparison'
    with pytest.raises(RuntimeError, match='interruption'):
        asyncio.run(p.run_pipeline(config, out, runner_factory=runner))
    assert instances[-1].closed
    assert json.loads((out/'summary.json').read_text())['stopped_at'] == 'comparison'
    before = (out/'novelty_search.json').read_bytes()
    runner.fail = None
    calls.clear()
    result = asyncio.run(p.run_pipeline(config, out, resume=True, runner_factory=runner))
    assert result['status'] == 'ready'
    assert calls == ['comparison','assessment','critic','portfolio']
    assert (out/'novelty_search.json').read_bytes() == before


@pytest.mark.parametrize('change', ['query', 'corrupt', 'gap', 'code'])
def test_resume_refuses_mismatches_before_new_calls(wired, tmp_path, monkeypatch, change):
    config, runner, calls, _ = wired
    out = tmp_path/'run'
    runner.fail = 'landscape'
    with pytest.raises(RuntimeError):
        asyncio.run(p.run_pipeline(config, out, runner_factory=runner))
    calls.clear()
    if change == 'query': config.query = 'Another question'
    if change == 'corrupt':
        data = json.loads((out/'plan.json').read_text())
        data['result']['queries'] = ['tampered']
        (out/'plan.json').write_text(json.dumps(data))
    if change == 'gap': (out/'plan.json').unlink()
    if change == 'code': monkeypatch.setattr(p, 'code_identity', lambda: 'changed')
    with pytest.raises(ValueError):
        asyncio.run(p.run_pipeline(config, out, resume=True, runner_factory=runner))
    assert not calls


def test_empty_search_stops_without_scientific_calls(wired, tmp_path):
    config, runner, calls, _ = wired
    runner.empty = True
    result = asyncio.run(p.run_pipeline(config, tmp_path/'empty', runner_factory=runner))
    assert result['status'] == 'empty' and result['portfolio'] is None
    assert calls == ['plan', 'retrieval']


def test_missing_paper_is_not_silently_skipped(wired, tmp_path):
    config, runner, calls, instances = wired
    for file in config.root.glob('*/paper.json'):
        file.unlink()
    with pytest.raises(ValueError, match='Cannot load retrieved paper'):
        asyncio.run(p.run_pipeline(config, tmp_path/'missing', runner_factory=runner))
    assert calls == ['plan','retrieval','contexts']
    assert instances[-1].closed


def test_config_and_exclusive_directory(wired, tmp_path):
    config, runner, _, _ = wired
    args = parser().parse_args(['topic', '--out', str(tmp_path/'run'), '--concurrency', '2',
                               '--max-refinement-cycles', '0', '--min-directions', '1'])
    parsed = configuration(args)
    assert parsed.query == 'topic' and parsed.comparison.concurrency == 2
    assert parsed.max_refinement_cycles == 0
    out = tmp_path/'existing'
    out.mkdir()
    with pytest.raises(FileExistsError):
        asyncio.run(p.run_pipeline(config, out, runner_factory=runner))


def test_lock_prevents_concurrent_use(tmp_path):
    with p.run_lock(tmp_path):
        with pytest.raises(ValueError, match='Another process'):
            with p.run_lock(tmp_path):
                pass


def test_refinement_receives_configured_search_limits(wired, monkeypatch):
    config, _, _, _ = wired
    config.max_refinement_cycles = 1
    config.novelty_search.rerank_k = 7
    config.novelty_search.candidate_k = 31
    seen = {}
    class Loop:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
        async def run(self, value):
            from types import SimpleNamespace
            return SimpleNamespace(repair_rounds=None,cycles=[])
    monkeypatch.setattr(p, 'RefinementLoop', Loop)
    runner = p.StageRunner(config)
    sentinel = object()
    monkeypatch.setattr(runner, 'get_retriever', lambda: sentinel)
    asyncio.run(runner.execute('refinement', {'critic': 'saved-critic'}))
    assert seen['search_settings'] == config.novelty_search
    assert seen['retriever'] is sentinel and seen['max_cycles'] == 1


def test_planner_and_retrieval_dispatch_configured_query(wired, monkeypatch):
    config, _, _, _ = wired
    planner = AsyncMock(return_value=QueryPlan(queries=[config.query, 'expanded query']))
    monkeypatch.setattr(p, 'plan_queries', planner)
    runner = p.StageRunner(config)
    retriever = type('Retriever', (), {'retrieve': AsyncMock(return_value='retrieved')})()
    monkeypatch.setattr(runner, 'get_retriever', lambda: retriever)
    async def execute():
        plan = await runner.execute('plan', {})
        return await runner.execute('retrieval', {'plan': plan})
    assert asyncio.run(execute()) == 'retrieved'
    assert planner.call_args.args == (config.query,)
    assert retriever.retrieve.call_args.kwargs['paper_k'] == config.initial_papers
    assert retriever.retrieve.call_args.args[0].queries[0] == config.query


def test_hundred_concurrency_defaults_and_cli_override():
    config = p.PipelineConfig(query='topic')
    names = ('opportunities', 'directions', 'novelty_search', 'comparison',
             'assessment', 'critic', 'refinement')
    assert config.concurrency == 100
    assert all(getattr(config, name).concurrency == 100 for name in names)
    overridden = configuration(parser().parse_args(['topic', '--out', '/tmp/concurrency-test',
                                                   '--concurrency', '100']))
    assert all(getattr(overridden, name).concurrency == 100 for name in names)


def test_runner_passes_concurrency_to_landscape(monkeypatch):
    build = AsyncMock(return_value='landscape')
    monkeypatch.setattr(p, 'build_landscape', build)
    runner = p.StageRunner(p.PipelineConfig(query='topic', allow_s3=False, concurrency=100))
    from types import SimpleNamespace
    async def exercise():
        try:
            assert await runner.execute('landscape', {'contexts': SimpleNamespace(papers={})}) == 'landscape'
        finally:
            await runner.close()
    asyncio.run(exercise())
    assert build.call_args.kwargs['concurrency'] == 100


def test_unlimited_call_budgets_round_trip():
    config = p.PipelineConfig(query='topic')
    names = ('reasoning', 'opportunities', 'directions', 'novelty_search',
             'comparison', 'assessment', 'critic', 'refinement')
    assert all(getattr(config, name).max_calls is None for name in names)
    restored = p.PipelineConfig.model_validate_json(config.model_dump_json())
    assert all(getattr(restored, name).max_calls is None for name in names)


def test_stage_progress_logs_duration_and_checkpoint(monkeypatch, capsys):
    from research import __main__ as cli
    ticks = iter([10, 22.5, 30, 35, 40])
    monkeypatch.setattr(cli.time, 'perf_counter', lambda: next(ticks))
    progress = cli.StageProgress()
    progress('landscape', 'running')
    progress('landscape', 'completed')
    progress('reasoning', 'running')
    progress('reasoning', 'failed')
    progress('retrieval', 'reused')
    output = capsys.readouterr().err
    assert 'landscape: completed (12.50s)' in output
    assert 'reasoning: failed (5.00s)' in output
    assert 'retrieval: reused (checkpoint; not rerun)' in output
    assert '+00:00]' in output


def test_automatic_output_uses_query_and_time(monkeypatch):
    import hashlib
    from research import __main__ as cli
    monkeypatch.setattr(cli.time, 'time_ns', lambda: 123456789)
    first = cli.automatic_output('topic')
    assert first.parent == Path(cli.__file__).resolve().parents[2] / 'research_runs'
    assert first.name == hashlib.sha256(json.dumps(['topic', 123456789], ensure_ascii=False).encode()).hexdigest()[:16]
    assert cli.automatic_output('different topic') != first
    monkeypatch.setattr(cli.time, 'time_ns', lambda: 123456790)
    assert cli.automatic_output('topic') != first
    assert parser().parse_args(['topic']).out is None
    with pytest.raises(ValueError, match='--resume requires --out'):
        configuration(parser().parse_args(['--resume']))
