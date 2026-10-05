"""Run complete saved-candidate revision, novelty revalidation and fresh critique.

Raw calls and per-stage artifacts live outside the repository. Successful identical
calls can be replayed on recovery; no target or source-page sampling is performed.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from hashlib import sha256
from pathlib import Path
import time
from critic.refinement_loop import RefinementLoop, RefinementResult
from critic.pipeline import Settings
from critic.schemas import CriticResult
from llm_client import AsyncLLMClient, ChatResult
from reasoning.evidence import PaperStore
from research.retrieval import LocalBackend, MultiQueryRetriever
from tools.validate_novelty_search import save


class LazyRetriever:
    """Load full configured hybrid index only if scientific changes require search."""
    def __init__(self,path,concurrency):
        self.path=Path(path);self.concurrency=concurrency
        self.lock=asyncio.Lock();self.backend=self.retriever=None
    async def retrieve(self,*args,**kw):
        async with self.lock:
            if self.retriever is None:
                self.backend=await asyncio.to_thread(LocalBackend,self.path)
                self.retriever=MultiQueryRetriever(self.backend,concurrency=self.concurrency)
        return await self.retriever.retrieve(*args,**kw)
    async def close(self):
        if self.retriever:await self.retriever.aclose()
        if self.backend:self.backend.close()


async def main(args):
    original=CriticResult.model_validate_json(Path(args.critic).read_text())
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    repo=Path(__file__).resolve().parent.parent
    paths=sorted(p for package in ('directions','novelty','critic') for p in (repo/package).glob('*.py'))
    save(out/'manifest.json',dict(arguments=vars(args),code_sha256={str(p.relative_to(repo)):sha256(p.read_bytes()).hexdigest() for p in paths}))
    client=AsyncLLMClient(args.provider,concurrency=args.concurrency,max_retries=0,timeout=900)
    events=[];replays=[]
    for directory in args.replay_dir:
        for path in sorted((Path(directory)/'calls').glob('*.request.json')):
            response=path.with_name(path.name.replace('.request.json','.response.json'))
            if response.exists():
                parsed=ChatResult(**json.loads(response.read_text()))
                if not parsed.truncated:replays.append((json.loads(path.read_text()),parsed,str(response)))
    async def chat(**kw):
        req=json.loads(kw['messages'][1]['content'])
        event=dict(id=len(events),task=req['task'],start=time.time());events.append(event)
        save(out/'calls'/f"{event['id']:03d}.request.json",kw)
        try:
            reused=next(((r,p) for q,r,p in replays if all(q.get(k)==kw.get(k) for k in ('messages','model','response_format'))),None)
            if reused:
                result,path=reused;event.update(checkpoint_replay=True,checkpoint_path=path)
            else:result=await client.chat_result(**kw)
            save(out/'calls'/f"{event['id']:03d}.response.json",asdict(result))
            event.update(model=result.model,finish_reason=result.finish_reason)
            return result
        except Exception as exc:
            event['error']=str(exc);raise
        finally:
            event['end']=time.time();save(out/'calls.json',events)
            print(json.dumps(event),flush=True)
    async def checkpoint(stage,value):
        save(out/(stage+'.json'),value.model_dump())
        print(json.dumps(dict(checkpoint=stage)),flush=True)
    retriever=LazyRetriever(args.index_dir,args.concurrency)
    try:
        result=await RefinementLoop(PaperStore(Path(args.root)),chat,review_chat=chat,
            provider=client.provider,model=client.default_model,
            settings=Settings(concurrency=args.concurrency,max_calls=args.max_calls,
                max_input_tokens=args.max_input_tokens,max_output_tokens=args.max_output_tokens),
            retriever=retriever,checkpoint=checkpoint,max_cycles=args.max_cycles).run(original,observations=json.loads(Path(args.observations).read_text()) if args.observations else None)
        save(out/'result.json',result.model_dump())
        saved=RefinementResult.model_validate_json((out/'result.json').read_text())
        summary=dict(status=saved.status,diagnostic=saved.diagnostic,handoff=saved.handoff(),
            cycles=len(saved.cycles),calls=saved.calls,usage=saved.usage,
            api_calls=sum(not e.get('checkpoint_replay',False) for e in events),
            checkpoint_replays=sum(bool(e.get('checkpoint_replay')) for e in events))
        save(out/'summary.json',summary);print(json.dumps(summary,indent=2),flush=True)
        return saved.status=='ready'
    finally:
        await retriever.close();await client.raw.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--critic',required=True);p.add_argument('--out',required=True)
    p.add_argument('--root',default='/srv/research_finder/markdown')
    p.add_argument('--index-dir',default='/srv/research_finder/index')
    p.add_argument('--provider',default='deepseek')
    p.add_argument('--observations',help='Located manual correctness observations; independently verified before edits.')
    p.add_argument('--replay-dir',action='append',default=[])
    p.add_argument('--max-cycles',type=int,choices=[1,2],default=2)
    p.add_argument('--max-calls',type=int,default=1000)
    p.add_argument('--concurrency',type=int,default=4)
    p.add_argument('--max-input-tokens',type=int,default=1000000)
    p.add_argument('--max-output-tokens',type=int,default=65536)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
