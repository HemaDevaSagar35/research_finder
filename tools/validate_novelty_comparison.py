"""Live original-evidence comparison, with raw calls and explicit shortlist coverage."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from directions.schemas import GenerationResult
from llm_client import AsyncLLMClient, ChatResult
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
    reused={}
    if args.reuse_extraction_from:
        for path in sorted(Path(args.reuse_extraction_from).glob('*.json')):
            row=json.loads(path.read_text());request=json.loads(row['request']['messages'][1]['content'])
            if request['task']=='extract_comparison_evidence':
                from novelty.comparison_records import EvidenceResponse
                EvidenceResponse.model_validate_json(row['response']['text'])
                reused[request['payload']['prior_paper_id']]=(path,request,row['response'])
        if not reused:raise ValueError('No valid saved extractions')
    evidence_replays=[]
    if args.reuse_evidence_from:
        evidence_tasks={'extract_comparison_evidence','repair_comparison_evidence','revise_comparison_evidence','review_comparison_evidence','repair_evidence_review'}
        for path in sorted(Path(args.reuse_evidence_from).glob('*.json')):
            row=json.loads(path.read_text());request=json.loads(row['request']['messages'][1]['content'])
            if request['task'] in evidence_tasks:evidence_replays.append((path,request,row['response']))
        if not evidence_replays:raise ValueError('No saved evidence calls')
    events, active, peak = [], 0, 0
    client = AsyncLLMClient('deepseek', concurrency=args.concurrency, max_retries=0, timeout=600)
    async def chat(**kw):
        nonlocal active, peak
        req = json.loads(kw['messages'][1]['content'])
        event = {'id':len(events), 'task':req['task'], 'paper_id':req['payload']['prior_paper_id'], 'start':time.time()}
        events.append(event); active += 1; peak = max(peak,active)
        try:
            replay=next(((path,response) for path,original,response in evidence_replays if original==req),None)
            if replay:
                path,response=replay
                event['reused_evidence']=str(path)
                result=ChatResult(response['text'],response['finish_reason'],response['model'])
            elif req['task']=='extract_comparison_evidence' and req['payload']['prior_paper_id'] in reused:
                path,original,response=reused[req['payload']['prior_paper_id']]
                for key in ('prior_artifact_sha256','targets','prior_passages','candidate_source_passages'):
                    if req['payload'][key]!=original['payload'][key]:raise ValueError('Saved extraction context mismatch: '+key)
                event['reused_extraction']=str(path)
                result=ChatResult(response['text'],response['finish_reason'],response['model'])
            else:
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
        save(out/'evidence_views.json', [
            {'direction_id':c.direction_id, 'paper_id':p.paper_id, 'evidence':p.evidence_view(), 'outcomes':p.outcomes()}
            for c in saved.candidates for p in c.papers if p.status!='skipped'])
        summary = {'api_calls':sum(not ('reused_extraction' in e or 'reused_evidence' in e) for e in events), 'reused_extractions':[e['reused_extraction'] for e in events if 'reused_extraction' in e], 'reused_evidence_calls':[e['reused_evidence'] for e in events if 'reused_evidence' in e], 'peak':peak, 'calls':saved.calls, 'usage':saved.usage,
            'papers':[{'direction_id':c.direction_id,'paper_id':p.paper_id,'status':p.status,
                'pages':len(p.context_pages),'missing_referenced_pages':p.missing_referenced_pages,
                'review_decisions':[r.report.decision if r.report else 'error' for r in p.reviews],
                'evidence_versions':len(p.evidence_reviews),'outcomes':p.outcomes(),
                'evidence_checks':[{'version':e.version,'trigger':e.trigger,
                    'claim_decisions':[x.decision for x in e.report.claim_checks] if e.report else [],
                    'coverage_decisions':[x.decision for x in e.report.coverage_checks] if e.report else [],
                    'error':e.error} for e in p.evidence_reviews],
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
    p.add_argument('--reuse-evidence-from',help='Replay evidence-stage responses only when the entire structured request matches; interpretation and review remain fresh. Replays are reported separately.')
    p.add_argument('--reuse-extraction-from',help='Replay saved valid extraction responses with identical evidence/targets; all subsequent stages make fresh API calls. Reported separately from API calls.')
    p.add_argument('--max-calls',type=int,default=80)
    p.add_argument('--concurrency',type=int,default=4)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
