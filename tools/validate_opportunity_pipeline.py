"""Bounded paid live validation of two fresh local-corpus pipelines.

uv run python -m tools.validate_opportunity_pipeline --out /path/to/new/run
Retrieval is lexical to keep external payloads limited to the approved chat API.
"""
import argparse
import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time

os.environ['LLM_TIMEOUT']='240'
os.environ['LLM_MAX_RETRIES']='0'
from landscape.builder import build_landscape
from landscape.paper_context import PaperContext, _project
from landscape.schemas import Landscape
from llm_client import AsyncLLMClient, usage
from opportunities.miner import OpportunityMiner, Settings
from reasoning.cross_paper import CrossPaperReasoner, Budgets, schedule_threads
from reasoning.evidence import PaperStore
from research.query_planner import plan_queries
from research.retrieval import LocalBackend, MultiQueryRetriever, RetrievalFilters

STAGE=ContextVar('validation_stage',default='setup')


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')


def select_landscape(land):
    """Preserve real support/IDs; prioritize shared items and varied item kinds."""
    data=land.model_dump()
    selection={key:[] for key in ('relationships','aggregated_findings','recurring_limitations',
                                 'common_assumptions','contradictions','underexplored_regimes')}
    for key in ('aggregated_findings','recurring_limitations','common_assumptions'):
        selection[key]=sorted(data[key],key=lambda x:(-len(x['supporting_papers']),x['item_id']))[:2]
    covered=set()
    for item in sorted(data['relationships'],key=lambda x:(-len(x['supporting_papers']),x['item_id'])):
        if set(item['supporting_papers'])-covered:
            selection['relationships'].append(item)
            covered.update(item['supporting_papers'])
        if len(selection['relationships'])==2:
            break
    selection['contradictions']=data['contradictions'][:1]
    data.update(selection)
    return Landscape.model_validate(data)


async def main(args):
    out=Path(args.out)
    if out.exists():
        raise ValueError('Use a new directory to preserve previous runs')
    out.mkdir(parents=True)
    semaphore=asyncio.Semaphore(6)
    original=AsyncLLMClient.chat_result
    events=[]
    active=peak=0

    async def tracked(client,*a,**kw):
        nonlocal active,peak
        async with semaphore:
            if client.provider!='deepseek' or str(client.raw.base_url).rstrip('/')!='https://api.deepseek.com':
                raise RuntimeError('Live validation is approved only for api.deepseek.com')
            if len(events)>=120:
                raise RuntimeError('120-attempt pipeline limit')
            event={'id':len(events),'stage':STAGE.get(),'start':time.time(),'max_tokens':kw.get('max_tokens')}
            events.append(event)
            active+=1
            peak=max(peak,active)
            try:
                result=await original(client,*a,**kw)
                event.update(model=result.model,finish_reason=result.finish_reason)
                save(out/'responses'/f"{event['id']:03d}.json",{'event':event,'text':result.text})
                return result
            except Exception as exc:
                event['error']=type(exc).__name__
                raise
            finally:
                active-=1
                event['end']=time.time()
                save(out/'calls.json',{'peak':peak,'events':events})

    AsyncLLMClient.chat_result=tracked
    backend=LocalBackend(Path(args.index),use_vector=False)
    topics=[('kv_cache','KV cache compression and quantization for long-context language model inference'),
            ('rag_grounding','retrieval augmented generation factual grounding and robustness to conflicting evidence')]

    async def pipeline(slug,topic,retriever):
        folder=out/slug
        folder.mkdir()
        def stage(name):
            STAGE.set(slug+'/'+name)
            print(time.strftime('%H:%M:%S'),slug,name,flush=True)
        try:
            stage('retrieval')
            plan=await plan_queries(topic,max_queries=2,provider='deepseek')
            result=await retriever.retrieve(plan,paper_k=3,record_k=40,filters=RetrievalFilters())
            save(folder/'retrieval.json',result.model_dump())
            store=PaperStore(Path(args.root))
            async def context(p):
                loaded=await asyncio.to_thread(store.get,p.paper_id)
                if loaded.status!='loaded':
                    raise RuntimeError(f'{p.paper_id}: {loaded.status}')
                card,missing=_project(p.paper_id,loaded.paper)
                return p.paper_id,PaperContext(paper_id=p.paper_id,card=card,missing_fields=missing,
                    matched_records=[r.model_dump() for r in p.records])
            contexts=dict(await asyncio.gather(*(context(p) for p in result.papers)))
            save(folder/'inventory.json',[{'paper_id':pid,'title':c.card.title} for pid,c in contexts.items()])
            stage('landscape')
            full=await build_landscape(topic,contexts,extraction_provider='deepseek',normalize_provider='deepseek',aggregation_provider='deepseek')
            save(folder/'landscape_full.json',full.model_dump())
            land=select_landscape(full)
            save(folder/'landscape.json',land.model_dump())
            budgets=Budgets(max_threads=9,max_calls=36,max_draft_output_tokens=16000,max_review_pages=24)
            save(folder/'selection.json',[{'id':t.thread_id,'kind':t.kind,'papers':t.papers,'statement':t.statement}
                  for t in schedule_threads(land,budgets)])
            stage('reasoning')
            client=AsyncLLMClient('deepseek',concurrency=4,max_retries=0)
            try:
                reasoner=CrossPaperReasoner(store,client.chat_result,budgets=budgets,concurrency=4,
                    provider=client.provider,draft_model=client.default_model,review_model=client.default_model)
                reasoning=await reasoner.run(land)
            finally:
                await client.raw.close()
            save(folder/'reasoning.json',reasoning.model_dump())
            stage('opportunities')
            miner=OpportunityMiner(store,provider='deepseek',settings=Settings(concurrency=4,max_calls=24,batch_size=3,
                max_batches=4,max_candidates_per_batch=3,max_output_tokens=16000,max_review_pages=24))
            mined=await miner.run(land,reasoning)
            save(folder/'opportunities.json',mined.model_dump())
            summary={'topic':topic,'papers':list(contexts),'reasoning':{'findings':len(reasoning.findings),
                'observations':len(reasoning.observations),'tensions':len(reasoning.tensions)},
                'mining':mined.coverage,'calls':mined.calls,'categories':[o.candidate.category for o in mined.opportunities],
                'multiple_papers':sum(o.support=='multiple_papers' for o in mined.opportunities),
                'diagnostics':[d.reason for d in mined.diagnostics]}
            save(folder/'summary.json',summary)
            print(slug,json.dumps(summary),flush=True)
            return summary
        except Exception as exc:
            save(folder/'error.json',{'type':type(exc).__name__,'detail':str(exc)[:1000]})
            print(slug,'FAILED',type(exc).__name__,str(exc)[:200],flush=True)
            return {'topic':topic,'error':type(exc).__name__}
    try:
        async with MultiQueryRetriever(backend,concurrency=4) as retriever:
            results=await asyncio.gather(*(pipeline(slug,topic,retriever) for slug,topic in topics))
        save(out/'summary.json',{'results':results,'peak':peak,'attempts':len(events),
             'usage':{k:vars(v) for k,v in usage.snapshot().items()},
             'usage_note':'Process totals are isolated; concurrent component usage snapshots overlap.'})
    finally:
        backend.close()
        AsyncLLMClient.chat_result=original


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True)
    p.add_argument('--root',default='/srv/research_finder/markdown')
    p.add_argument('--index',default='/srv/research_finder/index')
    asyncio.run(main(p.parse_args()))
