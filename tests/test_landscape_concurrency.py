"""Independent work overlaps while failures drain and output order is stable."""
import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from landscape import aggregation, builder


@pytest.mark.parametrize('fail', [False, True])
def test_cluster_summaries_overlap_and_close_after_drain(monkeypatch, fail):
    async def exercise():
        both_started = asyncio.Event()
        state = {'active': 0, 'peak': 0, 'finished': 0, 'closed': False}

        class Client:
            raw = None

            def __init__(self, *args):
                self.raw = self
                self.sem = asyncio.Semaphore(2)

            async def chat(self, prompt, **kwargs):
                async with self.sem:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                    if state['active'] == 2:
                        both_started.set()
                    try:
                        await asyncio.wait_for(both_started.wait(), timeout=2)
                        if fail and 'first' in prompt:
                            raise RuntimeError('provider failed')
                        await asyncio.sleep(0)
                        return '{"statement": "' + ('first' if 'first' in prompt else 'second') + '"}'
                    finally:
                        state['active'] -= 1
                        state['finished'] += 1

            async def close(self):
                assert state['active'] == 0
                assert state['finished'] == 2
                state['closed'] = True

        clusters = [[aggregation.RawStatement(text=label, kind='finding', paper_id=pid)
                     for pid in ['p1', 'p2']] for label in ['first', 'second']]
        monkeypatch.setattr(aggregation, '_collect_statements', lambda cards: sum(clusters, []))
        monkeypatch.setattr(aggregation, 'embed_texts', lambda *args: np.ones((4, 2)))
        monkeypatch.setattr(aggregation, '_cluster', lambda *args: clusters)
        monkeypatch.setattr(aggregation, 'AsyncLLMClient', Client)
        if fail:
            with pytest.raises(RuntimeError, match='provider failed'):
                await aggregation.aggregate_findings({})
        else:
            findings, _, _ = await aggregation.aggregate_findings({})
            assert [f.statement for f in findings] == ['first', 'second']
        assert state['peak'] == 2
        assert state['closed']

    asyncio.run(exercise())


@pytest.mark.parametrize('fail', [False, True])
def test_graph_and_aggregation_overlap_and_drain(monkeypatch, fail):
    async def exercise():
        graph_started, aggregation_started = asyncio.Event(), asyncio.Event()
        finished = []

        async def graph(*args, **kwargs):
            graph_started.set()
            await asyncio.wait_for(aggregation_started.wait(), timeout=2)
            if fail:
                raise RuntimeError('graph failed')
            return [], [], [], []

        async def aggregate(*args, **kwargs):
            aggregation_started.set()
            await asyncio.wait_for(graph_started.wait(), timeout=2)
            await asyncio.sleep(0)
            finished.append(True)
            return [], [], []

        monkeypatch.setattr(builder, '_build_graph', graph)
        monkeypatch.setattr(builder, 'aggregate_findings', aggregate)
        contexts = {'p1': SimpleNamespace(card=object())}
        if fail:
            with pytest.raises(RuntimeError, match='graph failed'):
                await builder.build_landscape('topic', contexts)
        else:
            land = await builder.build_landscape('topic', contexts)
            assert land.paper_ids == ['p1']
        assert finished == [True]

    asyncio.run(exercise())
