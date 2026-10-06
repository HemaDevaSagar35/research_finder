"""Paid real-model controls; synthetic upstream fixtures are explicitly labelled.

uv run python -m tools.validate_opportunity_controls --out /path/to/new/run
No live calls occur on import. Baseline miner code/prompts are not changed.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from llm_client import AsyncLLMClient, usage
from landscape.schemas import Landscape
from opportunities.miner import MiningFailure, OpportunityMiner, Settings
from opportunities.schemas import Candidate
from reasoning.evidence import PaperStore
from reasoning.schemas import CrossPaperReasoning
from tools.opportunity_validation_cases import CASES, fixture


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False)+'\n')


async def main(args):
    out=Path(args.out)
    if out.exists():
        raise ValueError('Use a new directory to preserve earlier results')
    out.mkdir(parents=True)
    client=AsyncLLMClient('deepseek',concurrency=4,max_retries=0,timeout=240)
    if str(client.raw.base_url).rstrip('/')!='https://api.deepseek.com':
        await client.raw.close()
        raise RuntimeError('Live validation is approved only for api.deepseek.com')
    semaphore=asyncio.Semaphore(4)
    events=[]
    active=peak=0

    async def chat(case_id, **kw):
        nonlocal active,peak
        async with semaphore:
            if len(events)>=args.max_api_calls:
                raise RuntimeError('Live-control attempt limit reached')
            event={'id':len(events),'case':case_id,'start':time.time()}
            events.append(event)
            active+=1
            peak=max(peak,active)
            try:
                result=await client.chat_result(**kw)
                event.update(finish_reason=result.finish_reason,model=result.model)
                save(out/'calls'/f"{event['id']:03d}.json",{'case':case_id,'messages':kw['messages'],
                     'response':asdict(result)})
                return result
            except Exception as exc:
                event['error']=type(exc).__name__
                raise
            finally:
                active-=1
                event['end']=time.time()
                save(out/'calls.json',{'peak':peak,'events':events})

    def miner(case_id,root):
        async def call(**kw):return await chat(case_id,**kw)
        return OpportunityMiner(PaperStore(root),call,provider=client.provider,model=client.default_model,
            settings=Settings(max_calls=5,max_output_tokens=16000,max_review_output_tokens=128000,
                              max_candidates_per_batch=2,max_input_tokens=40000))

    async def review(case_id,root,land,reasoning,candidate,expected):
        m=miner(case_id,root)
        _,_,sources,evidence=m._inputs(land,reasoning)
        result={'case':case_id,'mode':'review_only','expected':expected,'candidate':candidate.model_dump()}
        try:
            op=await m._review(case_id,candidate,sources,evidence)
            result.update(outcome='accept',opportunity=op.model_dump())
        except MiningFailure as exc:
            result.update(outcome=exc.reason,detail=exc.detail)
        except Exception as exc:
            result.update(outcome='error',detail=type(exc).__name__+': '+str(exc)[:500])
        result['pass']=result['outcome']=='accept' if expected=='accept' else result['outcome'] in ('rejected','insufficient','requires_correction')
        save(out/'results'/f'{case_id}.json',result)
        print(case_id,result['outcome'],'PASS' if result['pass'] else 'FAIL',flush=True)
        return result

    async def generate(category,root,land,reasoning):
        case_id='generate_'+category
        m=miner(case_id,root)
        result=await m.run(land,reasoning)
        save(out/'results'/f'{case_id}.json',result.model_dump())
        row={'case':case_id,'mode':'unconstrained_proposal','target_category':category,
             'proposed':result.coverage['proposed'],'accepted':len(result.opportunities),
             'accepted_categories':[o.candidate.category for o in result.opportunities],
             'multiple_papers':sum(o.support=='multiple_papers' for o in result.opportunities),
             'diagnostics':[d.reason for d in result.diagnostics]}
        print(case_id,json.dumps(row),flush=True)
        return row

    jobs=[]
    controls={}
    for category in CASES:
        root=out/'synthetic_corpus'
        land,reasoning,candidate=fixture(root,category)
        controls[category]=(root,land,reasoning,candidate)
        save(out/'inputs'/f'{category}.json',{'landscape':land.model_dump(),'reasoning':reasoning.model_dump(),
             'note':'Hand-authored reviewed-source fixtures. Not a live upstream reasoning run.'})
        jobs.append(review('positive_'+category,root,land,reasoning,candidate,'accept'))
        if not args.review_only:
            jobs.append(generate(category,root,land,reasoning))

    def negative(name,category,question,rationale,scope=None):
        root,land,reasoning,candidate=controls[category]
        changes={'question':question,'rationale':rationale}
        if scope:changes['scope']=scope
        jobs.append(review(name,root,land,reasoning,candidate.model_copy(update=changes),'withhold'))
    negative('negative_answered','missing_evaluation',
        'Which method has the lower median latency on workload W?',
        'The supplied reports leave the median-latency comparison unresolved.')
    negative('negative_unsupported_gap','missing_evaluation',
        'Why does method A suffer a p99 latency regression relative to method B on workload W?',
        'The reviewed evidence establishes a p99 regression for A versus B.')
    negative('negative_hardware','missing_regime',
        'How do these methods generalize from separate four-GPU and eight-GPU machines to multiple nodes?',
        'Method A was evaluated on two physically distinct machines, one with four GPUs and one with eight GPUs.')
    negative('negative_global_claim','missing_regime',
        'Why has no prior research ever evaluated multi-node language-model inference?',
        'These two one-node evaluations prove a gap across all published literature.',
        'All language-model inference research worldwide.')
    negative('negative_causal_leap','mechanistic_interaction',
        'How can the proven joint latency improvement of compression plus caching be increased further?',
        'These studies directly demonstrate that combining compression and caching improves latency.')
    negative('negative_erased_conditions','contradiction',
        'Why do exact replications with fully identical implementations and measurement procedures yield opposite latency effects?',
        'Both papers document identical seeds, warmup, implementations and measurement procedures.')

    # The exact saved real-corpus error plus attribution and answered-question probes.
    src=Path(args.real_inputs)
    real_root=Path(args.root)
    land=Landscape.model_validate_json((src/'landscape.json').read_text())
    reasoning=CrossPaperReasoning.model_validate_json((src/'reasoning.json').read_text())
    previous=json.loads(Path(args.real_opportunities).read_text())
    hardware=Candidate.model_validate(previous['opportunities'][1]['candidate'])
    for i in range(3):
        jobs.append(review(f'real_hardware_repeat_{i}',real_root,land,reasoning,hardware,'withhold'))
    original=Candidate.model_validate(previous['opportunities'][0]['candidate'])
    bad=original.model_copy(update={'question':'How can SemMoE overcome the larger-model and top-k limitations explicitly admitted by its authors?',
        'rationale':'The original authors explicitly state that larger models and different top-k routing remain unsupported.',
        'uncertainties':[]})
    jobs.append(review('real_author_attribution',real_root,land,reasoning,bad,'withhold'))
    answered=original.model_copy(update={'question':'Does SemMoE improve inference performance on DeepSeek-V2-Lite and Qwen3-30B-A3B in the evaluated settings?',
        'rationale':'The reviewed pages leave whether SemMoE improves performance on these evaluated models unresolved.'})
    jobs.append(review('real_answered',real_root,land,reasoning,answered,'withhold'))
    try:
        rows=await asyncio.gather(*jobs,return_exceptions=True)
        summary={'results':[r if not isinstance(r,BaseException) else {'error':repr(r)} for r in rows],
                 'api_attempts':len(events),'peak':peak,'usage':{k:vars(v) for k,v in usage.snapshot().items()},
                 'note':'Synthetic cases test miner behavior, not scientific discovery; real review probes use saved original pages.'}
        save(out/'summary.json',summary)
    finally:
        await client.raw.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True)
    p.add_argument('--review-only',action='store_true')
    p.add_argument('--max-api-calls',type=int,default=60)
    p.add_argument('--root',default='/srv/research_finder/markdown')
    p.add_argument('--real-inputs',default='/home/hema/research_runs/limitation_attribution_validation')
    p.add_argument('--real-opportunities',default='/home/hema/research_runs/opportunity_miner_validation/result.json')
    asyncio.run(main(p.parse_args()))
