"""Paid live generator smoke test; reuse three saved, validated opportunity sets.

uv run python -m tools.validate_direction_generator --out NEW_DIRECTORY
Only direction generation runs. No novelty or scientific critique is performed.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from directions.generator import DirectionGenerator, Settings
from directions.schemas import GenerationResult
from landscape.schemas import Landscape
from llm_client import AsyncLLMClient, usage
from opportunities.miner import digest
from opportunities.schemas import MiningResult
from reasoning.evidence import PaperStore
from reasoning.schemas import CrossPaperReasoning
from tools.direction_validation_cases import CONSISTENCY_CHECKS, REAL_CONSISTENCY_CASES


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n')


def load_selected_inputs(inputs, selectors):
    """Validate the complete selection before opening a client or making any calls."""
    names = ('moe', 'kv_cache', 'rag_grounding')
    requested = {name: set() for name in names}
    for selector in selectors:
        name, sep, oid = selector.partition(':')
        if name not in requested or not sep or not oid:
            raise ValueError(f'Expected CORPUS:OPPORTUNITY_ID, got {selector!r}')
        requested[name].add(oid)
    jobs = []
    for name in names:
        if selectors and not requested[name]:
            continue
        src = Path(inputs)/name
        land = Landscape.model_validate_json((src/'landscape.json').read_text())
        reasoning = CrossPaperReasoning.model_validate_json((src/'reasoning.json').read_text())
        mining = MiningResult.model_validate_json((src/'opportunities.json').read_text())
        parent_digest = digest(mining.model_dump())
        unknown = requested[name] - {o.opportunity_id for o in mining.opportunities}
        if unknown:
            raise ValueError(f'Unknown opportunities in {name}: {sorted(unknown)}')
        if selectors:
            mining = mining.model_copy(deep=True, update={'opportunities': [
                op for op in mining.opportunities if op.opportunity_id in requested[name]]})
        jobs.append((name, land, reasoning, mining, parent_digest))
    return jobs


async def main(args):
    inputs = load_selected_inputs(args.inputs, args.opportunity)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    save(out/'selection.json', {'repeats': args.repeats, 'manual_checks': CONSISTENCY_CHECKS,
        'regression_checks': {key: checks for key, checks in REAL_CONSISTENCY_CASES.items()
                              if not args.opportunity or key in args.opportunity}, 'inputs': [
        {'corpus': name, 'parent_mining_sha256': parent,
         'selected_opportunity_ids': [o.opportunity_id for o in mining.opportunities]}
        for name, _, _, mining, parent in inputs]})
    client = AsyncLLMClient('deepseek', concurrency=4, max_retries=0, timeout=300)
    if str(client.raw.base_url).rstrip('/') != 'https://api.deepseek.com':
        await client.raw.close()
        raise RuntimeError('Expected the authorized DeepSeek endpoint')
    semaphore = asyncio.Semaphore(4)
    events = []
    active = peak = 0

    async def chat(name, **kw):
        nonlocal active, peak
        async with semaphore:
            if len(events) >= args.max_api_calls:
                raise RuntimeError('Validation API-attempt limit reached')
            payload = json.loads(kw['messages'][1]['content'])['payload']
            event = {'id': len(events), 'corpus': name, 'opportunity_id': payload['opportunity']['opportunity_id'],
                     'task': json.loads(kw['messages'][1]['content'])['task'], 'start': time.time()}
            events.append(event)
            active += 1
            peak = max(peak, active)
            try:
                result = await client.chat_result(**kw)
                event.update(finish_reason=result.finish_reason, model=result.model)
                save(out/'calls'/f"{event['id']:03d}.json", {'messages': kw['messages'], 'response': asdict(result)})
                return result
            except Exception as exc:
                event['error'] = type(exc).__name__
                raise
            finally:
                active -= 1
                event['end'] = time.time()
                save(out/'calls.json', {'events': events, 'peak': peak})

    async def run(corpus, land, reasoning, mining, parent_digest, repeat):
        name = corpus if args.repeats == 1 else f'{corpus}_r{repeat}'
        for filename, value in [('landscape', land), ('reasoning', reasoning), ('opportunities', mining)]:
            save(out/name/(filename+'.json'), value.model_dump())
        async def call(**kw):
            return await chat(name, **kw)
        generator = DirectionGenerator(PaperStore(Path(args.root)), call, provider=client.provider,
            model=client.default_model, settings=Settings(max_calls=6*len(mining.opportunities)))
        result = await generator.run(land, reasoning, mining)
        save(out/name/'directions.json', result.model_dump())
        # Validate the persisted consumer handoff, not just the in-memory response.
        saved = GenerationResult.model_validate_json((out/name/'directions.json').read_text())
        assert saved.schema_version == 'directions_v3'
        assert saved.opportunities_ref['sha256'] == digest(mining.model_dump())
        assert saved.landscape_ref == mining.landscape_ref and saved.reasoning_ref == mining.reasoning_ref
        upstream = {o.opportunity_id: o for o in mining.opportunities}
        for direction in saved.directions:
            assert direction.opportunity == upstream[direction.opportunity_id]
            assert direction.recommended_next_experiment_id == direction.proposal.experiments[0].experiment_id
            assert direction.literature_novelty == direction.scientific_review == 'not_assessed'
            assert direction.hypothesis_status == 'proposed_untested'
            assert direction.context_pages == direction.opportunity.review_sources
        row = {'corpus': corpus, 'repeat': repeat, 'output': name, 'coverage': saved.coverage, 'calls': saved.calls,
               'diagnostics': [d.reason for d in saved.diagnostics],
               'hypotheses': sum(len(d.proposal.hypotheses) for d in saved.directions),
               'experiments': sum(len(d.proposal.experiments) for d in saved.directions),
               'passed': not saved.diagnostics and len(saved.directions) == len(mining.opportunities)}
        print(json.dumps(row), flush=True)
        return row

    try:
        rows = await asyncio.gather(*(run(*item, repeat) for item in inputs for repeat in range(args.repeats)), return_exceptions=True)
        rows = [r if not isinstance(r, BaseException) else {'error': repr(r), 'passed': False} for r in rows]
        summary = {'results': rows, 'passed': all(r['passed'] for r in rows), 'api_attempts': len(events),
                   'peak_concurrency': peak, 'usage': {k: vars(v) for k, v in usage.snapshot().items()},
                   'note': 'Saved upstream inputs, only generator rerun. Process aggregate usage; concurrent component deltas overlap. No scientific value/novelty verdict.'}
        save(out/'summary.json', summary)
        print(json.dumps(summary), flush=True)
        return summary['passed']
    finally:
        await client.raw.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--inputs', default='/home/hema/research_runs/opportunity_boundaries_live')
    parser.add_argument('--root', default='/srv/research_finder/markdown')
    parser.add_argument('--max-api-calls', type=int, default=72)
    parser.add_argument('--opportunity', action='append', default=[], metavar='CORPUS:ID',
                        help='Only regenerate these saved opportunities; repeat for multiple selections.')
    parser.add_argument('--repeats', type=int, choices=range(1, 6), default=1)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
