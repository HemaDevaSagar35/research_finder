"""Bounded live judge controls and real saved-draft revision/review validation.

uv run python -m tools.validate_direction_judge --out NEW_DIRECTORY
Only correctness review and any needed revision run live. Saved real drafts and
hand-authored controls are used; generation reuse is explicitly recorded.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from directions.generator import DirectionGenerator, Settings
from directions.schemas import DirectionDraft, GenerationResult
from llm_client import AsyncLLMClient, ChatResult, usage
from reasoning.evidence import PaperStore
from tools.direction_validation_cases import build_case
from tools.validate_direction_generator import load_selected_inputs, save


def control_draft(op):
    eids = op.candidate.evidence_ids
    return DirectionDraft.model_validate({
        'title': 'Compression and cache locality',
        'research_direction': 'Investigate whether compression-induced routing changes improve cache latency on model M and workload W.',
        'rationale': [{'statement': 'Compression reduced routing entropy with caching disabled; a separate caching study used compression disabled.',
                       'evidence_ids': eids, 'page_ids': [f'{p.paper_id}#p{p.page}' for p in op.review_sources]}],
        'proposed_mechanism': 'Lower routing entropy may concentrate expert accesses and increase cache reuse. This is a proposed explanation.',
        'scope': 'Model M and workload W; the joint intervention is untested.',
        'assumptions': ['Compression and caching can be combined without changing the task.'],
        'hypotheses': [{'hypothesis_id': 'H1', 'condition': 'Model M, workload W, caching enabled.',
            'intervention': 'Enable compression.', 'expected_effect': 'Mean request latency is lower than with compression disabled.',
            'mechanism': 'More concentrated routing may improve reuse.', 'assumptions': ['Saved transfers outweigh compression overhead.'],
            'falsification_criterion': 'Mean latency is equal or higher under the same comparison.', 'evidence_ids': eids}],
        'experiments': [{'experiment_id': 'E1', 'stage': 'initial', 'hypothesis_ids': ['H1'],
            'objective': 'Test the latency prediction.',
            'comparison': 'Compare caching with and without compression on the same model and workload.',
            'observations': ['Mean request latency', 'Cache locality'],
            'why_this_test': 'A direct comparison tests the joint effect before developing a new cache policy.',
            'informative_outcomes': 'Lower latency supports H1; equal or higher latency contradicts H1. The causal explanation requires further investigation.'}],
        'risks': ['Compression overhead may dominate saved transfers.'],
        'uncertainties': ['The joint effect has not been measured.'],
        'what_would_falsify_it': 'No latency gain would contradict the proposed benefit while still answering the interaction question.'})


async def main(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    sem = asyncio.Semaphore(4)
    client = AsyncLLMClient('deepseek', concurrency=4, max_retries=0, timeout=300)
    if str(client.raw.base_url).rstrip('/') != 'https://api.deepseek.com':
        await client.raw.close()
        raise RuntimeError('Expected the authorized DeepSeek endpoint')
    events, active, peak = [], 0, 0

    async def call(label, **kw):
        nonlocal active, peak
        async with sem:
            if len(events) >= args.max_api_calls:
                raise RuntimeError('Live judge validation attempt cap exhausted')
            req = json.loads(kw['messages'][1]['content'])
            event = {'id': len(events), 'case': label, 'task': req['task'], 'start': time.time()}
            events.append(event)
            active += 1
            peak = max(peak, active)
            try:
                value = await client.chat_result(**kw)
                event.update(model=value.model, finish_reason=value.finish_reason)
                save(out/'calls'/f"{event['id']:03d}.json", {'case': label, 'messages': kw['messages'], 'response': asdict(value)})
                return value
            except Exception as exc:
                event['error'] = type(exc).__name__
                raise
            finally:
                event['end'] = time.time()
                active -= 1
                save(out/'calls.json', {'events': events, 'peak': peak})

    async def review_only(label, root, land, reasoning, mining, draft, expected):
        async def chat(**kw): return await call(label, **kw)
        generator = DirectionGenerator(PaperStore(root), chat, provider=client.provider,
            model=client.default_model, settings=Settings(max_calls=1))
        sources = {kind + ':' + getattr(x, key): (kind, x)
                   for kind, xs, key in [('observation', reasoning.observations, 'observation_id'),
                                         ('finding', reasoning.findings, 'finding_id'), ('tension', reasoning.tensions, 'tension_id')]
                   for x in xs}
        op = mining.opportunities[0]
        payload, _, _ = await generator._payload(op, land, sources)
        generator._validate_draft(draft, op)
        records = []
        try:
            report, _ = await generator._review(op, payload, generator._assign_ids(draft, 0), 0, 0, records)
            row = {'case': label, 'expected': expected, 'decision': report.decision,
                   'expectation_met': report.decision == expected}
        except Exception as exc:
            row = {'case': label, 'expected': expected, 'error': str(exc), 'expectation_met': False}
        save(out/'reviews'/(label+'.json'), {'result': row, 'records': [r.model_dump() for r in records]})
        return row

    async def gate(label, root, land, reasoning, mining, draft):
        async def chat(**kw):
            req = json.loads(kw['messages'][1]['content'])
            if req['task'] == 'generate_direction':
                return ChatResult(json.dumps({'direction': draft.model_dump(), 'abstention_reason': None}), 'stop', 'saved-draft-reuse')
            return await call(label, **kw)
        generator = DirectionGenerator(PaperStore(root), chat, provider=client.provider,
            model=client.default_model, settings=Settings(max_calls=6))
        result = await generator.run(land, reasoning, mining)
        save(out/'gated'/(label+'.json'), result.model_dump())
        saved = GenerationResult.model_validate_json((out/'gated'/(label+'.json')).read_text())
        return {'case': label, 'directions': len(saved.directions),
                'decisions': [r.report.decision if r.report else 'error' for r in saved.reviews],
                'diagnostics': [d.reason for d in saved.diagnostics], 'calls_including_saved_generation': saved.calls,
                'gate_integrity': all(d.correctness_review == 'passed' for d in saved.directions),
                'manual_review': 'pending'}

    try:
        root = out/'synthetic_corpus'
        land, reasoning, mining, _ = build_case(root, 'untested_interaction')
        good = control_draft(mining.opportunities[0])
        partial = good.model_copy(deep=True)
        partial.hypotheses[0].expected_effect = 'Mean latency is lower and cache locality improves when compression is enabled.'
        partial.hypotheses[0].falsification_criterion = 'No latency improvement or no locality improvement contradicts the corresponding component.'
        partial.experiments[0].observations = ['Mean request latency']
        partial.experiments[0].informative_outcomes = 'Lower latency supports only the latency component of H1; locality and the proposed mechanism remain unresolved. Equal or higher latency contradicts the latency component.'
        inversion = good.model_copy(deep=True)
        inversion.rationale[0].statement = 'The compression study reports an entropy increase from 2 bits to 4 bits with caching disabled.'
        polarity = good.model_copy(deep=True)
        polarity.experiments[0].informative_outcomes = 'Equal or higher latency supports H1; lower latency contradicts H1.'
        controls = [('sound', good, 'pass'), ('sound_partial_test', partial, 'pass'),
                    ('source_inversion', inversion, 'revise'), ('outcome_reversal', polarity, 'revise')]
        real = []
        for corpus, oid in [('kv_cache', 'op-000-003'), ('rag_grounding', 'op-000-002')]:
            _, l, r, m, _ = load_selected_inputs(args.inputs, [corpus+':'+oid])[0]
            if args.reviewed_drafts:
                saved = json.loads((Path(args.reviewed_drafts)/'gated'/(corpus+'.json')).read_text())
                draft = max((r for r in saved['reviews'] if r['opportunity_id'] == oid),
                            key=lambda r: r['round'])['proposal']
            else:
                saved = json.loads((Path(args.saved)/corpus/'directions.json').read_text())
                draft = next(d for d in saved['directions'] if d['opportunity_id'] == oid)['proposal']
            real.append((corpus, Path(args.root), l, r, m, DirectionDraft.model_validate(draft)))
        save(out/'controls.json', {'controls': [
            {'case': n, 'candidate': d.model_dump(), 'expected': e} for n, d, e in controls],
            'real': [{'case': n, 'candidate': d.model_dump(), 'expected': 'revise'} for n, _, _, _, _, d in real],
            'rubric': 'Catch the seeded source inversion and outcome reversal; pass sound and explicitly partial suggestions. Real KV: Jensen bias versus mixing-weight misstatement and equal-degradation contradiction. Real RAG: unknown baseline assertions and at-least-two versus one-model outcome. Inspect exact issues manually; a revise decision alone is not proof it caught the intended defect.'})
        rows = await asyncio.gather(
            *(review_only(n, root, land, reasoning, mining, d, e) for n, d, e in controls),
            *(review_only(n, root, l, r, m, d, 'revise') for n, root, l, r, m, d in real),
            *(gate(n, root, l, r, m, d) for n, root, l, r, m, d in ([] if args.review_only else real)))
        control_rows, gate_rows = rows[:6], rows[6:]
        summary = {'controls': control_rows, 'gated': gate_rows, 'api_attempts': len(events),
                   'peak_concurrency': peak, 'usage': {k: vars(v) for k, v in usage.snapshot().items()},
                   'review_only': args.review_only, 'reviewed_drafts': args.reviewed_drafts,
                   'note': 'Fresh reviews and any revisions are live; initial real generation is explicitly reused, not rerun. Human/assistant inspection required for semantics.'}
        save(out/'summary.json', summary)
        print(json.dumps(summary), flush=True)
        return all(r['expectation_met'] for r in control_rows) and all(r['gate_integrity'] for r in gate_rows)
    finally:
        await client.raw.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--inputs', default='/home/hema/research_runs/opportunity_boundaries_live')
    parser.add_argument('--saved', default='/home/hema/research_runs/direction_v2_explicit_comparisons_live')
    parser.add_argument('--root', default='/srv/research_finder/markdown')
    parser.add_argument('--max-api-calls', type=int, default=16)
    parser.add_argument('--review-only', action='store_true', help='Run controls and reviews only; no revision cycles.')
    parser.add_argument('--reviewed-drafts', help='Prior judge harness output; check its last reviewed drafts instead of original v2 drafts.')
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
