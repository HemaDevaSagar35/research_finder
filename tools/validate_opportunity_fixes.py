"""Re-review saved real candidates to verify corrected wording and paper roles.

Paid live calls, four concurrent, bounded to 24 attempts. Historical artifacts
are read as candidates only; their prior acceptance is not upgraded to v2.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time

from llm_client import AsyncLLMClient, usage
from landscape.schemas import Landscape
from opportunities.miner import OpportunityMiner, Settings, MiningFailure
from opportunities.schemas import Candidate
from reasoning.evidence import PaperStore
from reasoning.schemas import CrossPaperReasoning


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2)+'\n')


async def main(args):
    out=Path(args.out)
    if out.exists(): raise ValueError('Use a new output directory')
    out.mkdir(parents=True)
    base=Path(args.runs)
    real=Path(args.root)
    client=AsyncLLMClient('deepseek',concurrency=4,max_retries=0,timeout=240)
    if str(client.raw.base_url).rstrip('/')!='https://api.deepseek.com':
        await client.raw.close()
        raise RuntimeError('Expected the configured DeepSeek endpoint')
    semaphore=asyncio.Semaphore(4)
    events=[]
    async def call(case,**kw):
        async with semaphore:
            if len(events)>=24: raise RuntimeError('24-call fix-validation limit')
            event={'case':case,'id':len(events),'start':time.time()}
            events.append(event)
            try:
                response=await client.chat_result(**kw)
                event.update(finish_reason=response.finish_reason,model=response.model)
                save(out/'calls'/f"{event['id']:03d}.json",{'messages':kw['messages'],'response':response.text})
                return response
            finally:
                event['end']=time.time()
                save(out/'calls.json',events)

    async def check(name,folder,candidate,expected_supporting,expected_context):
        land=Landscape.model_validate_json((folder/'landscape.json').read_text())
        reasoning=CrossPaperReasoning.model_validate_json((folder/'reasoning.json').read_text())
        async def chat(**kw):return await call(name,**kw)
        miner=OpportunityMiner(PaperStore(real),chat,provider='deepseek',model=client.default_model,
            settings=Settings(max_calls=2,max_review_output_tokens=128000))
        _,_,sources,evidence=miner._inputs(land,reasoning)
        row={'case':name,'expected_supporting':expected_supporting,'expected_context':expected_context,
             'candidate':candidate.model_dump()}
        try:
            op=await miner._review(name,candidate,sources,evidence)
            row.update(outcome='accept',opportunity=op.model_dump(),passed=(
                set(op.supporting_paper_ids)==set(expected_supporting) and
                set(op.context_paper_ids)==set(expected_context)))
        except MiningFailure as exc:
            row.update(outcome=exc.reason,detail=exc.detail,review=exc.review,passed=False)
        save(out/'results'/f'{name}.json',row)
        print(name,row['outcome'],'PASS' if row['passed'] else 'FAIL',flush=True)
        return row

    old=json.loads((base/'opportunity_miner_validation/result.json').read_text())
    hardware=Candidate.model_validate(old['opportunities'][1]['candidate'])
    corrected=hardware.model_copy(update={'scope':hardware.scope.replace(
        'on 8xH100 and 4xH100 DGX systems with TP-4/TP-8',
        'on one 8xH100 DGX system, using four or eight of its GPUs for TP-4/TP-8')})
    assert corrected.scope!=hardware.scope,'Saved hardware phrasing changed'
    if args.fully_corrected_only:
        corrected=hardware.model_copy(update={
            'rationale': 'The reviewed TokenWeave setup page (p9) describes a single 8xH100 DGX, with TP-4 using four of its eight GPUs, and mentions additional 8xB200 DGX results in Appendix C. This reviewed page does not report inter-node experiments. It motivates a question about inter-node latency and topology; it does not establish an absence of such experiments elsewhere in the paper.',
            'scope': 'Only the inference settings and evidence on the reviewed TokenWeave page 9. The 8xH100 main setup includes four- and eight-GPU configurations on the same machine; the page also mentions 8xB200 appendix results, which are not inspected here. No whole-paper or literature-wide absence claim is made.',
            'uncertainties': hardware.uncertainties + ['The inference that inter-node behavior remains unresolved is limited to the reviewed page; unreviewed appendices or other work could already address it.'],
        })
    kv=base/'opportunity_broad_live/kv_cache'
    prior=json.loads((kv/'opportunities.json').read_text())
    shared=Candidate.model_validate(prior['opportunities'][0]['candidate'])
    context=Candidate.model_validate(prior['opportunities'][1]['candidate'])
    jobs=[]
    for i in range(3):
        if args.fully_corrected_only:
            jobs.append(check(f'fully_corrected_hardware_{i}',base/'limitation_attribution_validation',
                              corrected,['b02deed4ad77'],[]))
        else:
            jobs += [check(f'corrected_hardware_{i}',base/'limitation_attribution_validation',corrected,['b02deed4ad77'],[]),
                     check(f'context_role_{i}',kv,context,['712e704db09d'],['fc330d65cf4f']),
                     check(f'shared_support_{i}',kv,shared,['712e704db09d','fc330d65cf4f'],[])]
    try:
        rows=await asyncio.gather(*jobs)
        save(out/'summary.json',{'results':rows,'api_calls':len(events),
             'usage':{k:vars(v) for k,v in usage.snapshot().items()}})
    finally:
        await client.raw.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True)
    p.add_argument('--fully-corrected-only',action='store_true')
    p.add_argument('--runs',default='/home/hema/research_runs')
    p.add_argument('--root',default='/srv/research_finder/markdown')
    asyncio.run(main(p.parse_args()))
