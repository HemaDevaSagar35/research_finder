"""Replay saved formatting failures once; measure accepted coverage without downstream runs."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from directions.schemas import GenerationResult
from llm_client import AsyncLLMClient, usage
from novelty import comparison_prompts as prompts
from novelty.comparison import page_numbers
from novelty.comparison_evidence import passages_for_pages, table_context_refs, attach_table_context, validate_comparison, validate_report
from novelty.comparison_records import EvidenceReport, EvidenceResponse, EvidenceReview
from novelty.comparison_repair import EvidencePatch, attach_review_citations, repair_scope, apply_evidence_patch, review_addresses
from novelty.comparison_schemas import NoveltyComparisonResult, ComparisonResponse, ComparisonReport, ComparisonReview
from novelty.comparison_workflow import validate_record, validate_evidence_report, eligible, assemble, accepted_targets, project, proposal_requirements
from novelty.assessment_schemas import AssessmentInputs
from novelty.assessment_evidence import build_packet
from extraction.research_extract import _parse_json
from novelty.schemas import NoveltySearchResult
from reasoning.evidence import PaperStore
from reasoning.budget import estimate_tokens


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def log(message):
    print(f'[{datetime.now(timezone.utc).isoformat()}] {message}', flush=True)


def payload_for(store, paper, candidate):
    artifact = store.get(paper.paper_id)
    if artifact.status != 'loaded' or artifact.sha256 != paper.expected_artifact_sha256:
        raise ValueError('Prior paper artifact changed or missing')
    pages = []
    for ref in paper.context_pages:
        page = store.page(ref.paper_id, ref.page)
        if page is None or page.sha256 != ref.sha256:
            raise ValueError('Prior source page changed or missing')
        pages.append(dict(page_id=f'{page.paper_id}#p{page.page}', sha256=page.sha256, text=page.text))
    passages = passages_for_pages(pages)
    if passages != paper.passages:
        raise ValueError('Prior passages differ from saved input')
    source_pages = []
    prior_ids = {p.page_id for p in passages}
    for ref in sorted(candidate.context_pages, key=lambda r: (r.paper_id, r.page)):
        page = store.page(ref.paper_id, ref.page)
        if page is None or page.sha256 != ref.sha256:
            raise ValueError('Candidate source page changed or missing')
        pid = f'{page.paper_id}#p{page.page}'
        if pid not in prior_ids:
            source_pages.append(dict(page_id=pid, text=page.text))
    _, referenced = page_numbers(store.root, paper.paper_id, artifact.paper)
    return dict(targets=[t.model_dump() for t in paper.targets], prior_paper_id=paper.paper_id,
        proposal_requirements={t.target_id: proposal_requirements(t) for t in paper.targets},
        prior_artifact_sha256=artifact.sha256,
        paper_navigation=dict(title=artifact.paper.get('title'), available_pages=[r.page for r in paper.context_pages], structured_referenced_pages=sorted(referenced)),
        candidate_source_passages=[p.model_dump() for p in passages_for_pages(source_pages)],
        prior_passages=[p.model_dump() for p in passages], table_context_refs=table_context_refs(passages),
        missing_referenced_pages=paper.missing_referenced_pages, evidence_scope=paper.evidence_scope)


def coverage(generation, search, comparisons):
    inputs = AssessmentInputs(generation=generation, search=search, comparisons=comparisons, comparison_coverage_threshold=.8)
    rows = []
    for candidate in comparisons.candidates:
        for row in build_packet(inputs, candidate.direction_id)['coverage']:
            usable = sum(p['status'] == 'reviewed' and not p['missing_referenced_pages'] for p in row['papers'])
            total = len(row['papers'])
            rows.append(dict(target_id=row['target_id'], level=row['level'], accepted=usable, total=total,
                percent=round(100*usable/total, 2) if total else None, passes_80_percent_gate=row['complete'],
                retrieval_status=row['retrieval_status']))
    return rows


async def main(args):
    started = time.monotonic()
    source, out = Path(args.source), Path(args.out)
    inventory = json.loads((out/'inventory.json').read_text())
    def load(name, model):
        return model.model_validate(json.loads((source/name).read_text())['result'])
    generation = load('directions.json', GenerationResult)
    search = load('novelty_search.json', NoveltySearchResult)
    original = load('comparison.json', NoveltyComparisonResult)
    updated = original.model_copy(deep=True)
    baseline = coverage(generation, search, original)
    save(out/'coverage_before.json', baseline)
    settings = json.loads(source.with_suffix('.settings.json').read_text())
    store = PaperStore(Path(settings['root']))  # Local only; never fetch or retrieve new papers.
    candidates = {c.direction_id: c for c in updated.candidates}
    searches = {c.direction_id: c for c in search.candidates}
    rows, prepared = [], []
    for item in inventory:
        paper = next(p for p in candidates[item['direction']].papers if p.paper_id == item['paper'])
        if paper.comparison is not None:
            raise ValueError('Selected failed job already has accepted comparisons; refusing to overwrite')
        try:
            payload = payload_for(store, paper, searches[item['direction']])
            prepared.append((item, paper, payload))
        except Exception as exc:
            rows.append(dict(**item, status='input_unavailable', format_resolved=None, error=str(exc)))
    save(out/'manifest.json', dict(source=str(source), jobs=len(inventory), prepared=len(prepared),
        hypothesis_entries=sum('-h' in t for i in inventory for t in i['targets']),
        policy='One attempt per stage; reuse saved evidence/drafts where available. No repairs, retrieval, assessment, critic, refinement or publication. Original artifacts untouched.',
        request_fidelity='Requests reconstructed using current prompts/schemas and hash-verified pages; failed repair raw inputs were not always saved, so replay starts at that phase before format repair.',
        concurrency=args.concurrency))
    log(f'Prepared {len(prepared)}/{len(inventory)} jobs; starting bounded replay at concurrency={args.concurrency}')
    client = AsyncLLMClient('deepseek', concurrency=args.concurrency, max_retries=0, timeout=600)
    sem = asyncio.Semaphore(args.concurrency)
    calls = []

    async def call(item, task, system, schema, body):
        model = original.run['review_model'] if task.startswith('review_') else original.run['model']
        messages = [dict(role='system', content=system+'\nReturn a JSON object matching the supplied schema.'),
                    dict(role='user', content=json.dumps(dict(task=task, **body), ensure_ascii=False, separators=(',', ':')))]
        definition = schema.model_json_schema()
        tokens = sum(estimate_tokens(m['content'])+32 for m in messages) + estimate_tokens(json.dumps(definition))
        if tokens > settings['comparison']['max_input_tokens']:
            raise ValueError('input_budget: reconstructed input exceeds saved allowance')
        event = dict(id=len(calls), direction=item['direction'], paper=item['paper'], task=task, estimated_input_tokens=tokens)
        calls.append(event)
        request = dict(messages=messages, schema=definition, model=model, max_tokens=settings['comparison']['max_output_tokens'])
        save(out/'calls'/f"{event['id']:03d}.json", dict(event=event, request=request, status='running'))
        log(f"call {event['id']} start {item['direction']}/{item['paper']} {task}")
        tick = time.monotonic()
        try:
            result = await client.structured_result(**request)
            event['elapsed_seconds'] = round(time.monotonic()-tick, 2)
            save(out/'calls'/f"{event['id']:03d}.json", dict(event=event, request=request, response=asdict(result)))
            if result.finish_reason not in ('completed', 'stop'):
                raise ValueError('incomplete response: '+str(result.finish_reason))
            parsed = schema.model_validate(_parse_json(result.text))
            log(f"call {event['id']} format PASS ({event['elapsed_seconds']}s)")
            return parsed, result.model
        except Exception as exc:
            event['error'] = str(exc)
            log(f"call {event['id']} failed: {str(exc)[:260]}")
            raise

    async def one(item, old, payload):
        async with sem:
            row = dict(**item, status='running', format_resolved=False, accepted_targets=[])
            paper = old.model_copy(deep=True)
            try:
                diagnostic = old.diagnostic or ''
                review_only = 'invalid_comparison_review_after_repair' in diagnostic
                if review_only:
                    evidence = paper.evidence_reviews[-1]
                    draft = paper.reviews[-1].draft
                else:
                    if 'invalid_evidence_review_after_repair' in diagnostic:
                        record = paper.evidence_reviews[-1].record
                    elif 'invalid_evidence_patch_after_repair' in diagnostic:
                        previous = paper.evidence_reviews[-1]
                        record = attach_review_citations(previous.record, previous.report, paper.passages)
                        scope = repair_scope(previous, None)
                        patch, _ = await call(item, 'patch_comparison_evidence', prompts.PATCH_EVIDENCE, EvidencePatch,
                            dict(payload=payload, record=record.model_dump(), repair_scope=scope,
                                 corrections=previous.report.model_dump() if previous.report else previous.error))
                        row['format_resolved'] = True
                        record = apply_evidence_patch(record, patch, scope).record
                    else:
                        response, _ = await call(item, 'extract_comparison_evidence', prompts.EXTRACT, EvidenceResponse, dict(payload=payload))
                        row['format_resolved'] = True
                        if response.record is None:
                            raise ValueError('evidence_abstained: '+str(response.abstention_reason))
                        record = response.record
                    record = attach_table_context(record, paper.passages)
                    validate_record(record, paper.targets, paper.passages, require_alignment=True, require_context=True)
                    report, model = await call(item, 'review_comparison_evidence', prompts.EVIDENCE_REVIEW, EvidenceReport,
                        dict(payload=payload, record=record.model_dump(), required_scope_claim_ids=[c.claim_id for c in record.claims if c.kind in ('result','evaluation','theoretical_result')]))
                    row['format_resolved'] = True
                    validate_evidence_report(report, record, paper.passages, require_scope=True)
                    evidence = EvidenceReview(version=len(paper.evidence_reviews), trigger='evidence_repair' if paper.evidence_reviews else 'initial',
                        record=record, revision_notes=[], format_repairs=[], model=model, report=report, error=None)
                    paper.evidence_reviews.append(evidence)
                    ids, claims = eligible(evidence)
                    row['evidence_eligible_targets'] = sorted(ids)
                    if not ids:
                        raise ValueError('Unresolved evidence review; no target eligible for comparison')
                    response, _ = await call(item, 'compare_prior_work', prompts.COMPARE, ComparisonResponse,
                        dict(payload=payload, eligible_target_ids=sorted(ids), reviewed_evidence=dict(claims=[c.model_dump() for c in claims],
                            relationships=[r.model_dump() for r in evidence.record.relationships if r.target_id in ids])))
                    if response.abstention_reason or response.evidence_requests:
                        raise ValueError('Comparison abstained or requested evidence reopening; replay does not loop')
                    draft = assemble(response, evidence, paper.targets)
                    validate_comparison(draft, {**payload, 'targets':[t.model_dump() for t in paper.targets if t.target_id in ids]})
                ids, _ = eligible(evidence)
                report, model = await call(item, 'review_comparison', prompts.REVIEW, ComparisonReport,
                    dict(payload=payload, eligible_target_ids=sorted(ids), withheld_target_ids=sorted(set(paper.target_ids)-ids),
                         relationships=[r.model_dump() for r in evidence.record.relationships if r.target_id in ids],
                         comparison=draft.model_dump(), valid_review_fields=review_addresses(draft)))
                row['format_resolved'] = True
                validate_report(report, draft, paper.passages)
                paper.reviews.append(ComparisonReview(round=0, evidence_version=evidence.version, draft=draft,
                    format_repairs=[], model=model, report=report, error=None))
                paper.comparison = project(draft, accepted_targets(report, draft, evidence))
                row['accepted_targets'] = [p.target_id for p in paper.comparison.pairs] if paper.comparison else []
                paper.status = 'complete' if len(row['accepted_targets']) == len(paper.target_ids) else 'partial' if row['accepted_targets'] else 'unresolved'
                paper.diagnostic = None if paper.status == 'complete' else 'Targeted replay: comparison review left targets unresolved.'
                row['status'] = paper.status
                row['review_decision'] = report.decision
            except Exception as exc:
                row['status'] = 'withheld' if row['format_resolved'] else 'failed'
                row['error'] = str(exc)
                # Retain original scientific state if no accepted comparison was produced.
                paper = old
            rows.append(row)
            if row['accepted_targets']:
                candidate = candidates[item['direction']]
                candidate.papers = [paper if p.paper_id == paper.paper_id else p for p in candidate.papers]
            save(out/'jobs'/f"{item['direction']}_{item['paper']}.json", dict(summary=row, replay_paper=paper.model_dump()))
            save(out/'progress.json', dict(completed=len(rows), total=len(inventory), calls=len(calls), jobs=rows))
            log(f"JOB {len(rows)}/{len(inventory)} {item['direction']}/{item['paper']} {row['status']} format_resolved={row['format_resolved']} accepted={len(row['accepted_targets'])}")

    try:
        await asyncio.gather(*(one(*job) for job in prepared))
    finally:
        await client.raw.close()
    after = coverage(generation, search, updated)
    save(out/'comparison.json', updated.model_dump())
    save(out/'coverage_after.json', after)
    changes = [dict(target_id=a['target_id'], before=b, after=a,
                    percentage_point_change=round(a['percent']-b['percent'],2) if a['percent'] is not None and b['percent'] is not None else None) for b,a in zip(baseline,after)]
    summary = dict(status='completed', elapsed_seconds=round(time.monotonic()-started,2), jobs=len(rows), calls=len(calls),
        format_resolved_jobs=sum(r['format_resolved'] is True for r in rows),
        format_resolved_hypothesis_entries=sum(sum('-h' in t for t in r['targets']) for r in rows if r['format_resolved'] is True),
        total_hypothesis_entries=sum(sum('-h' in t for t in r['targets']) for r in rows),
        coverage_changes=changes, results=rows, usage={k:vars(v) for k,v in usage.snapshot().items()},
        note='Format resolved refers to the originally failing phase. Coverage counts only independently accepted comparisons with no missing referenced pages. No downstream assessments or approvals were run.')
    save(out/'summary.json', summary)
    log(f"DONE format resolved {summary['format_resolved_hypothesis_entries']}/{summary['total_hypothesis_entries']} hypothesis entries; results: {out/'summary.json'}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--concurrency', type=int, default=20)
    asyncio.run(main(parser.parse_args()))
