"""Paid boundary reviews and full-context mining of saved real upstream results.

Synthetic pages are fictional controls, not scientific evidence. Real runs reuse
saved, previously reviewed landscape/reasoning artifacts. Only the miner is rerun.
Run with uv run python -m tools.validate_opportunity_boundaries --out NEW_DIRECTORY.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from landscape.schemas import Landscape, limitation_origin
from llm_client import AsyncLLMClient, usage
from opportunities.miner import MiningFailure, OpportunityMiner, Settings, digest
from opportunities.schemas import MiningResult, Opportunity
from reasoning.evidence import PaperStore, candidates
from reasoning.schemas import CrossPaperReasoning
from tools.opportunity_validation_cases import CASES, fixture


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def controls(root):
    """Paired controls test evidence boundaries, not research value or novelty."""
    rows = []

    def add(name, category, case, expected, changes=None):
        corpus = root / name
        land, reasoning, candidate = fixture(corpus, category, case=case)
        if changes:
            candidate = candidate.model_copy(update=changes)
        rows.append((name, corpus, land, reasoning, candidate, expected))

    interaction = CASES['mechanistic_interaction']
    add('inferred_question', 'mechanistic_interaction', interaction, 'accept')
    add('invented_joint_benefit', 'mechanistic_interaction', interaction, 'withhold', {
        'question': 'How can the experimentally proven joint latency improvement from compression and caching be increased?',
        'rationale': 'The two reports demonstrate that combining compression and caching reduces latency.'})
    add('nominally_matched_discrepancy', 'contradiction', CASES['contradiction'], 'accept')
    different = {
        'pages': [
            'Cache A reduces mean latency from 20 ms to 10 ms for model M on GPU G1, short-prompt workload W1, batch 1. No other hardware or workload is evaluated. The contribution of hardware versus workload to the cache effect is not isolated.',
            'Cache A increases mean latency from 20 ms to 30 ms for model M on GPU G2, long-prompt workload W2, batch 64. No other hardware or workload is evaluated. The contribution of hardware versus workload to the cache effect is not isolated.'],
        'question': 'Which hardware or workload differences account for the different cache effects in these two evaluated settings?',
        'rationale': 'The two reports vary GPU, prompt workload and batch size together; they do not isolate which differences account for the latency effects.'}
    add('different_conditions_question', 'missing_evaluation', different, 'accept')
    add('different_conditions_false_contradiction', 'contradiction', different, 'withhold', {
        'question': 'Why does the identical cache experiment under identical hardware and workload conditions yield contradictory latency effects?',
        'rationale': 'The papers use identical GPU, workload and batch settings yet obtain opposite effects.'})
    referenced = {
        'pages': [
            'Method A reports median latency of 10 ms on workload W. Tail-latency measurements (p95 and p99) were also collected; their values and discussion are reported in the supplementary evaluation table, not on this page. This page supplies no tail values.',
            'Method B reports median latency of 8 ms on workload W. Its p95 and p99 measurements are reported in a supplementary evaluation table, not on this page. This page supplies no tail values.'],
        'question': 'Can the p99 ranking of methods A and B on workload W be established from these two reviewed pages?',
        'rationale': 'These reviewed pages omit the p99 values while explicitly pointing to measurements elsewhere; they do not establish which method has lower p99 latency.'}
    add('unavailable_results_never_measured', 'missing_evaluation', referenced, 'withhold', {
        'question': 'How do these methods perform on tail latency, which neither paper ever measured?',
        'rationale': 'Both papers entirely omit p95 and p99 experiments, establishing a missing-evaluation research gap.'})
    return rows


def audit(op, store, sources, evidence):
    """Check the saved v2 handoff against upstream IDs and original artifacts."""
    op = Opportunity.model_validate_json(op.model_dump_json())
    assert {s.source_id for s in op.sources} == set(op.candidate.source_ids)
    assert {e.evidence_id for e in op.evidence} == set(op.candidate.evidence_ids)
    for source in op.sources:
        assert source.kind == sources[source.source_id]['kind']
        assert source.refines == sources[source.source_id]['item'].refines
    for ev in op.evidence:
        owner, original = evidence[ev.evidence_id]
        assert owner in op.candidate.source_ids and ev == original
        paper = store.get(ev.paper_id)
        assert paper.status == 'loaded' and paper.sha256 == ev.artifact_sha256
        row = next(r for r in candidates(paper.paper, ev.paper_id) if r.value_path == ev.value_path)
        assert row.provenance_path == ev.provenance_path
        assert ev.source_value and row.source_value.startswith(ev.source_value)
        assert row.source_locations == [s.model_dump() for s in ev.source_locations]
        assert ev.origin == limitation_origin(ev.value_path)
    page_map = {}
    for src in op.review_sources:
        page = store.page(src.paper_id, src.page)
        assert page is not None and page.sha256 == src.sha256
        page_map[f'{src.paper_id}#p{src.page}'] = src.paper_id
    assert set(op.paper_ids) == {e.paper_id for e in op.evidence}
    assert set(op.supporting_paper_ids) | set(op.context_paper_ids) == set(op.paper_ids)
    assert not set(op.supporting_paper_ids) & set(op.context_paper_ids)
    assert op.supporting_paper_ids
    assert op.support == ('multiple_papers' if len(op.supporting_paper_ids) > 1 else 'single_paper')
    assert len(op.paper_assessments) == len(op.paper_ids)
    assert {a.paper_id for a in op.paper_assessments} == set(op.paper_ids)
    by_id = {e.evidence_id: e for e in op.evidence}
    for assessment in op.paper_assessments:
        assert all(by_id[e].paper_id == assessment.paper_id for e in assessment.evidence_ids)
        assert all(page_map[p] == assessment.paper_id for p in assessment.page_ids)
        assert assessment.paper_id in (op.supporting_paper_ids if assessment.role == 'supporting' else op.context_paper_ids)
    assert op.literature_novelty == 'not_assessed'
    return {'evidence_references': len(op.evidence), 'page_references': len(op.review_sources)}


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

    async def call(name, **kw):
        nonlocal active, peak
        async with sem:
            if len(events) >= args.max_api_calls:
                raise RuntimeError('Validation attempt limit reached')
            request = json.loads(kw['messages'][1]['content'])
            event = {'id': len(events), 'case': name, 'task': request['task'], 'start': time.time(),
                     'source_ids': [s['source_id'] for s in request['payload']['sources']]}
            events.append(event)
            active += 1
            peak = max(peak, active)
            try:
                response = await client.chat_result(**kw)
                event.update(finish_reason=response.finish_reason, model=response.model)
                save(out / 'calls' / f"{event['id']:03d}.json", {
                    'case': name, 'messages': kw['messages'], 'response': asdict(response)})
                return response
            except Exception as exc:
                event['error'] = type(exc).__name__
                raise
            finally:
                active -= 1
                event['end'] = time.time()
                save(out / 'calls.json', {'peak': peak, 'events': events})

    def miner(name, corpus):
        async def chat(**kw):
            return await call(name, **kw)
        return OpportunityMiner(PaperStore(corpus), chat, settings=Settings(),
                                provider='deepseek', model=client.default_model)

    async def review(name, corpus, land, reasoning, candidate, expected):
        m = miner(name, corpus)
        _, _, sources, evidence = m._inputs(land, reasoning)
        row = {'case': name, 'expected': expected, 'candidate': candidate.model_dump()}
        try:
            op = await m._review(name, candidate, sources, evidence)
            row.update(outcome='accept', opportunity=op.model_dump(),
                       audit=await asyncio.to_thread(audit, op, m.store, sources, evidence))
        except MiningFailure as exc:
            row.update(outcome=exc.reason, detail=exc.detail, review=exc.review)
        row['passed'] = row['outcome'] == 'accept' if expected == 'accept' else row['outcome'] in (
            'rejected', 'insufficient', 'requires_correction')
        save(out / 'results' / f'{name}.json', row)
        print(name, row['outcome'], 'PASS' if row['passed'] else 'FAIL', flush=True)
        return row

    async def run_real(name, folder):
        land = Landscape.model_validate_json((folder / 'landscape.json').read_text())
        reasoning = CrossPaperReasoning.model_validate_json((folder / 'reasoning.json').read_text())
        save(out / name / 'landscape.json', land.model_dump())
        save(out / name / 'reasoning.json', reasoning.model_dump())
        m = miner(name, Path(args.root))
        _, _, sources, evidence = m._inputs(land, reasoning)
        result = await m.run(land, reasoning)
        save(out / name / 'opportunities.json', result.model_dump())
        result = MiningResult.model_validate_json((out / name / 'opportunities.json').read_text())
        assert result.landscape_ref['sha256'] == digest(land.model_dump())
        assert result.reasoning_ref['sha256'] == digest(reasoning.model_dump())
        proposals = [e for e in events if e['case'] == name and e['task'] == 'proposal']
        assert proposals and all(set(e['source_ids']) == set(sources) for e in proposals)
        assert result.coverage['batches'] == 1 and result.run['settings']['batch_size'] == 0
        audits = await asyncio.to_thread(lambda: [audit(op, m.store, sources, evidence) for op in result.opportunities])
        technical = [d.reason for d in result.diagnostics if d.reason not in ('rejected', 'insufficient', 'requires_correction')]
        row = {'case': name, 'coverage': result.coverage, 'calls': result.calls,
               'diagnostics': [d.reason for d in result.diagnostics], 'audit': audits,
               'passed': not technical and bool(result.opportunities)}
        print(name, json.dumps(row), flush=True)
        return row

    jobs = []
    for name, corpus, land, reasoning, candidate, expected in controls(out / 'synthetic_corpus'):
        save(out / 'inputs' / f'{name}.json', {'landscape': land.model_dump(), 'reasoning': reasoning.model_dump(),
                                             'note': 'Hand-authored synthetic upstream fixture.'})
        for repeat in range(args.repeats):
            jobs.append(review(f'{name}_{repeat}', corpus, land, reasoning, candidate, expected))
    base = Path(args.runs)
    for name, folder in [('moe', base / 'limitation_attribution_validation'),
                         ('kv_cache', base / 'opportunity_broad_live/kv_cache'),
                         ('rag_grounding', base / 'opportunity_broad_live/rag_grounding')]:
        jobs.append(run_real(name, folder))
    try:
        rows = await asyncio.gather(*jobs, return_exceptions=True)
        rows = [r if not isinstance(r, BaseException) else {'error': repr(r), 'passed': False} for r in rows]
        summary = {'results': rows, 'passed': all(r['passed'] for r in rows), 'api_attempts': len(events),
                   'peak_concurrency': peak, 'usage': {k: vars(v) for k, v in usage.snapshot().items()},
                   'note': 'Only miner rerun. Synthetic expectations test evidence boundaries, not scientific merit. '
                           'Per-run usage may overlap; this process aggregate is authoritative.'}
        save(out / 'summary.json', summary)
        print(json.dumps({k: v for k, v in summary.items() if k != 'results'}), flush=True)
    finally:
        await client.raw.close()
    return summary['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--runs', default='/home/hema/research_runs')
    parser.add_argument('--root', default='/srv/research_finder/markdown')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--max-api-calls', type=int, default=48)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
