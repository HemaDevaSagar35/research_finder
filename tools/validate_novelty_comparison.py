"""Live original-evidence comparison, with raw calls and explicit shortlist coverage."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from directions.schemas import GenerationResult
from llm_client import AsyncLLMClient
from novelty.comparison import NoveltyComparator, Settings
from novelty.comparison_schemas import NoveltyComparisonResult
from novelty.schemas import NoveltySearchResult
from reasoning.evidence import PaperStore
from tools.validate_novelty_search import save


async def main(args):
    generation = GenerationResult.model_validate_json(Path(args.directions).read_text())
    search = NoveltySearchResult.model_validate_json(Path(args.search).read_text())
    selected = args.paper_id
    if args.include_seeds:
        selected = sorted(set(selected or []) | {pid for d in generation.directions for pid in d.paper_artifact_hashes})
    out = Path(args.out); out.mkdir(parents=True, exist_ok=False)
    save(out/'manifest.json', {'directions':args.directions,'search':args.search,
        'selected_papers':selected, 'scope':'Selected papers only; all omitted shortlist pairs are explicit skipped outcomes.' if selected else 'All saved shortlists.'})
    events, active, peak = [], 0, 0
    client = AsyncLLMClient('deepseek', concurrency=args.concurrency, max_retries=0, timeout=600)
    async def chat(**kw):
        nonlocal active, peak
        req = json.loads(kw['messages'][1]['content'])
        event = {'id':len(events), 'task':req['task'], 'paper_id':req['payload']['prior_paper_id'], 'start':time.time()}
        events.append(event); active += 1; peak = max(peak,active)
        try:
            result = await client.chat_result(**kw)
            event.update(model=result.model, finish_reason=result.finish_reason)
            save(out/'calls'/f"{event['id']:03d}.json", {'request':kw,'response':asdict(result)})
            return result
        except Exception as exc:
            event['error'] = str(exc); raise
        finally:
            active -= 1; event['end'] = time.time()
            save(out/'calls.json', {'events':events,'peak':peak})
            print(json.dumps(event), flush=True)
    try:
        result = await NoveltyComparator(PaperStore(Path(args.root)), chat, provider=client.provider,
            model=client.default_model, settings=Settings(max_calls=args.max_calls, concurrency=args.concurrency)).run(
                generation,search,paper_ids=selected)
        save(out/'result.json',result.model_dump())
        saved = NoveltyComparisonResult.model_validate_json((out/'result.json').read_text())
        summary = {'api_calls':len(events), 'peak':peak, 'calls':saved.calls, 'usage':saved.usage,
            'papers':[{'direction_id':c.direction_id,'paper_id':p.paper_id,'status':p.status,
                'pages':len(p.context_pages),'missing_referenced_pages':p.missing_referenced_pages,
                'review_decisions':[r.report.decision if r.report else 'error' for r in p.reviews],
                'diagnostic':p.diagnostic,
                'pairs':[{'target':x.target_id,'label':x.classification,'hypothesis_tested':x.hypothesis_tested}
                    for x in p.comparison.pairs] if p.comparison else []}
                for c in saved.candidates for p in c.papers if p.status != 'skipped'],
            'skipped_pairs':sum(len(p.target_ids) for c in saved.candidates for p in c.papers if p.status == 'skipped'),
            'semantic_inspection':'pending', 'literature_novelty':'not_assessed'}
        save(out/'summary.json', summary); print(json.dumps(summary),flush=True)
        return bool(summary['papers']) and all(p['status'] == 'complete' for p in summary['papers']) and all(not c.diagnostic for c in saved.candidates)
    finally:
        await client.raw.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('directions','search','out'): p.add_argument('--'+name,required=True)
    p.add_argument('--root',default='/srv/research_finder/markdown')
    p.add_argument('--paper-id',action='append')
    p.add_argument('--include-seeds',action='store_true')
    p.add_argument('--max-calls',type=int,default=80)
    p.add_argument('--concurrency',type=int,default=4)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
