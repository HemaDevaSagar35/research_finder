"""Live sections-13/14 validation with raw calls and immutable input provenance."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from directions.schemas import GenerationResult
from llm_client import AsyncLLMClient
from novelty.schemas import NoveltySearchResult
from novelty.comparison_schemas import NoveltyComparisonResult
from novelty.assessment_schemas import Invalidations, NoveltyAssessmentResult
from novelty.assessment import NoveltyAssessor, Settings
from novelty.assessment_evidence import build_packet, source_aliases
from tools.validate_novelty_search import save


async def main(args):
    generation = GenerationResult.model_validate_json(Path(args.directions).read_text())
    search = NoveltySearchResult.model_validate_json(Path(args.search).read_text())
    comparisons = NoveltyComparisonResult.model_validate_json(Path(args.comparisons).read_text())
    invalidations = Invalidations.model_validate_json(Path(args.invalidations).read_text()) if args.invalidations else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    save(out/'manifest.json', vars(args))
    events = []
    client = AsyncLLMClient('deepseek', concurrency=args.concurrency, max_retries=0, timeout=600)
    async def chat(**kw):
        req = json.loads(kw['messages'][1]['content'])
        event = dict(id=len(events), task=req['task'], direction_id=req['packet']['direction_id'], start=time.time())
        events.append(event)
        try:
            result = await client.chat_result(**kw)
            save(out/'calls'/f"{event['id']:03d}.json", dict(request=kw, response=asdict(result)))
            event.update(model=result.model, finish_reason=result.finish_reason)
            return result
        except Exception as exc:
            event['error'] = str(exc)
            raise
        finally:
            event['end'] = time.time()
            save(out/'calls.json', events)
            print(json.dumps(event), flush=True)
    try:
        result = await NoveltyAssessor(chat, provider=client.provider, model=client.default_model,
            settings=Settings(max_calls=args.max_calls, concurrency=args.concurrency)).run(
                generation, search, comparisons, invalidations=invalidations)
        save(out/'result.json', result.model_dump())
        saved = NoveltyAssessmentResult.model_validate_json((out/'result.json').read_text())
        save(out/'source_aliases.json', {c.direction_id: source_aliases(build_packet(saved.inputs, c.direction_id))
            for c in saved.candidates})
        by_id = {d.direction_id: d for d in generation.directions}
        summary = dict(calls=saved.calls, usage=saved.usage, candidates=[
            dict(direction_id=c.direction_id, published=c.assessment is not None,
                outcomes=c.outcomes(), refinement=c.refinement_handoff(by_id[c.direction_id]),
                diagnostic=c.diagnostic, review_decisions=[r.report.decision if r.report else 'error' for r in c.reviews])
            for c in saved.candidates])
        save(out/'summary.json', summary)
        print(json.dumps(summary, indent=2))
        return all(c.assessment is not None for c in saved.candidates)
    finally:
        await client.raw.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('directions', 'search', 'comparisons', 'out'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--invalidations')
    p.add_argument('--max-calls', type=int, default=8)
    p.add_argument('--concurrency', type=int, default=4)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
