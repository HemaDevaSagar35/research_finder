"""Rerun selected failed rankings using exact saved hybrid pools (no embeddings)."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from llm_client import AsyncLLMClient
from novelty.pipeline import NoveltySearcher, Settings
from novelty.schemas import NoveltySearchResult
from reasoning.evidence import PaperStore
from research.retrieval import RetrievalFilters
from tools.validate_novelty_search import save


async def main(args):
    prior = NoveltySearchResult.model_validate_json(Path(args.input).read_text())
    candidate = next(c for c in prior.candidates if c.direction_id == args.direction)
    selected = args.target or [s.target_id for s in candidate.searches if s.status == 'failed']
    targets = [t for t in candidate.signature.targets if t.target_id in selected]
    if len(targets) != len(selected): raise ValueError('unknown or duplicate target')
    pools = {tuple(s.retrieval.queries): s.retrieval for s in candidate.searches if s.retrieval}
    out = Path(args.out); out.mkdir(parents=True, exist_ok=False)
    events, active, peak = [], 0, 0
    class Replay:
        async def retrieve(self, plan, **kw):
            if kw['filters'] != RetrievalFilters(): raise ValueError('unexpected filter')
            return pools[tuple(plan.queries)].model_copy(deep=True)
    client = AsyncLLMClient('deepseek', concurrency=4, max_retries=0, timeout=300)
    async def chat(**kw):
        nonlocal active, peak
        event = {'id':len(events), 'start':time.time()}; events.append(event)
        active += 1; peak = max(peak, active)
        try:
            response = await client.chat_result(**kw)
            save(out/'calls'/f"{event['id']:03d}.json", {'request':kw,'response':asdict(response)})
            return response
        except Exception as exc:
            event['error'] = type(exc).__name__; raise
        finally:
            active -= 1; event['end'] = time.time()
            save(out/'calls.json', {'events':events,'peak':peak})
    try:
        runner = NoveltySearcher(PaperStore(Path(args.root)), Replay(), chat, provider=client.provider,
            model=client.default_model, settings=Settings(max_calls=len(targets),rerank_k=args.keep))
        results = await asyncio.gather(*(runner._search(t) for t in targets))
        save(out/'results.json', [r.model_dump() for r in results])
        summary = {'source':args.input, 'calls':runner.budget.snapshot(), 'peak':peak,
            'targets':[{'id':r.target_id,'status':r.status,'ranked':len(r.ranked),'diagnostic':r.diagnostic} for r in results],
            'note':'Exact saved hybrid pools; no regeneration, embeddings or new signature approval.'}
        save(out/'summary.json',summary); print(json.dumps(summary))
        return all(r.status == 'complete' for r in results)
    finally: await client.raw.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--direction',default='dir-000');parser.add_argument('--target',action='append')
    parser.add_argument('--root',default='/srv/research_finder/markdown');parser.add_argument('--keep',type=int,default=5)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
