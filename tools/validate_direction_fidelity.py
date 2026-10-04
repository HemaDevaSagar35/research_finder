"""Paid generation-only controls; semantic fidelity is inspected against fixed rubrics.

uv run python -m tools.validate_direction_fidelity --out NEW_DIRECTORY
This is not a runtime scientific critic, a novelty benchmark, or an LLM judge.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from directions.generator import DirectionGenerator, Settings
from directions.schemas import GenerationResult
from llm_client import AsyncLLMClient, usage
from opportunities.miner import digest
from reasoning.evidence import PaperStore
from tools.direction_validation_cases import CASE_NAMES, build_case


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')


async def main(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    client = AsyncLLMClient('deepseek', concurrency=4, max_retries=0, timeout=300)
    if str(client.raw.base_url).rstrip('/') != 'https://api.deepseek.com':
        await client.raw.close()
        raise RuntimeError('Expected the authorized DeepSeek endpoint')
    sem = asyncio.Semaphore(4)
    events = []
    active = peak = 0

    async def call(case, **kw):
        nonlocal active, peak
        async with sem:
            if len(events) >= args.max_api_calls:
                raise RuntimeError('Fidelity validation call limit reached')
            event = {'id': len(events), 'case': case, 'start': time.time()}
            events.append(event)
            active += 1
            peak = max(peak, active)
            try:
                response = await client.chat_result(**kw)
                event.update(finish_reason=response.finish_reason, model=response.model)
                save(out/'calls'/f"{event['id']:03d}.json", {'case': case, 'messages': kw['messages'], 'response': asdict(response)})
                return response
            except Exception as exc:
                event['error'] = type(exc).__name__
                raise
            finally:
                active -= 1
                event['end'] = time.time()
                save(out/'calls.json', {'events': events, 'peak': peak})

    async def run(name, repeat, corpus, land, reasoning, mining, rubric):
        label = f'{name}_{repeat}'
        async def chat(**kw):
            return await call(label, **kw)
        generator = DirectionGenerator(PaperStore(corpus), chat, provider=client.provider, model=client.default_model,
            settings=Settings(max_calls=6, max_hypotheses=2, max_experiments=2))
        result = await generator.run(land, reasoning, mining)
        save(out/'results'/f'{label}.json', result.model_dump())
        saved = GenerationResult.model_validate_json((out/'results'/f'{label}.json').read_text())
        assert saved.schema_version == 'directions_v3'
        assert saved.opportunities_ref['sha256'] == digest(mining.model_dump())
        for d in saved.directions:
            assert d.opportunity == mining.opportunities[0]
            assert d.context_pages == d.opportunity.review_sources
            assert d.hypothesis_status == 'proposed_untested'
            assert d.literature_novelty == d.scientific_review == 'not_assessed'
        row = {'case': label, 'structural_passed': len(saved.directions) == 1 and not saved.diagnostics,
               'diagnostics': [d.reason for d in saved.diagnostics], 'calls': saved.calls,
               'semantic_fidelity': 'pending_manual_inspection', 'rubric': rubric}
        print(json.dumps(row), flush=True)
        return row

    jobs = []
    for name in CASE_NAMES:
        corpus = out/'synthetic_corpus'/name
        land, reasoning, mining, rubric = build_case(corpus, name)
        save(out/'inputs'/f'{name}.json', {'landscape': land.model_dump(), 'reasoning': reasoning.model_dump(),
             'opportunities': mining.model_dump(), 'rubric': rubric,
             'note': 'Fictional documents and hand-authored accepted upstream fixture; generator-only live run.'})
        for repeat in range(args.repeats):
            jobs.append(run(name, repeat, corpus, land, reasoning, mining, rubric))
    try:
        rows = await asyncio.gather(*jobs, return_exceptions=True)
        rows = [r if not isinstance(r, BaseException) else {'error': repr(r), 'structural_passed': False} for r in rows]
        summary = {'results': rows, 'structural_passed': all(r['structural_passed'] for r in rows),
                   'semantic_fidelity': 'pending_manual_inspection', 'api_attempts': len(events), 'peak': peak,
                   'usage': {k: vars(v) for k, v in usage.snapshot().items()},
                   'note': 'Inspect every generated proposal against the saved predeclared rubric; no automatic semantic verdict.'}
        save(out/'summary.json', summary)
        print(json.dumps({k: v for k, v in summary.items() if k != 'results'}), flush=True)
        return summary['structural_passed']
    finally:
        await client.raw.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--max-api-calls', type=int, default=48)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
