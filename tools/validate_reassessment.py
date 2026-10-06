"""Repair a saved paper interpretation, then run complete synthesis and critique."""
import argparse,asyncio,json,time
from dataclasses import asdict
from pathlib import Path
from critic.pipeline import ResearchCritic,Settings
from critic.refinement_loop import RefinementLoop,requests_for
from novelty.reassessment import InterpretationConcern,InterpretationReassessor,ReassessmentResult,IncompleteComparisonRecovery,ComparisonRecoveryResult,merge_recovery
from novelty.assessment_schemas import NoveltyAssessmentResult
from novelty.assessment import NoveltyAssessor
from novelty.pipeline import NoveltySearcher, Settings as SearchSettings
from novelty.schemas import NoveltySearchResult
from novelty.comparison import NoveltyComparator
from reasoning.evidence import PaperStore
from llm_client import AsyncLLMClient,ChatResult
from opportunities.miner import digest
from tools.validate_novelty_search import save
from tools.validate_refinement_loop import LazyRetriever


async def main(args):
    source=NoveltyAssessmentResult.model_validate_json(Path(args.assessment).read_text())
    concerns=[InterpretationConcern.model_validate(c) for c in json.loads(Path(args.concerns).read_text())]
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    save(out/'manifest.json',dict(arguments=vars(args),parent_assessment_sha256=digest(source.model_dump())))
    events=[];replays=[]
    for directory in args.replay_dir:
        for p in sorted((Path(directory)/'calls').glob('*.request.json')):
            resp=p.with_name(p.name.replace('.request.json','.response.json'))
            if resp.exists():
                r=ChatResult(**json.loads(resp.read_text()))
                if not r.truncated:replays.append((json.loads(p.read_text()),r,str(resp)))
    client=AsyncLLMClient('deepseek',concurrency=args.concurrency,max_retries=0,timeout=900)
    async def chat(**kw):
        request=json.loads(kw['messages'][1]['content']);event=dict(id=len(events),task=request['task'],start=time.time());events.append(event)
        save(out/'calls'/f"{event['id']:03d}.request.json",kw)
        try:
            replay=next(((r,p) for q,r,p in replays if all(q.get(k)==kw.get(k) for k in ('messages','model','response_format'))),None)
            if replay:
                response,path=replay;event.update(checkpoint_replay=True,checkpoint_path=path)
            else:response=await client.chat_result(**kw)
            save(out/'calls'/f"{event['id']:03d}.response.json",asdict(response))
            event.update(finish_reason=response.finish_reason,model=response.model);return response
        except Exception as exc:event['error']=str(exc);raise
        finally:event['end']=time.time();save(out/'calls.json',events);print(json.dumps(event),flush=True)
    store=PaperStore(Path(args.root));retriever=LazyRetriever(args.index_dir,args.concurrency)
    settings=Settings(concurrency=args.concurrency,max_calls=args.max_calls,max_input_tokens=1000000,max_output_tokens=65536)
    options=dict(review_chat=chat,provider=client.provider,model=client.default_model,settings=settings)
    async def checkpoint(stage,value):save(out/(stage+'.json'),value.model_dump())
    try:
        recovery=repaired=None
        generation,search,comparisons=source.inputs.generation,source.inputs.search,source.inputs.comparisons
        if args.resume_search:
            parent_search=NoveltySearchResult.model_validate_json(Path(args.resume_search).read_text())
            searcher=NoveltySearcher(store,retriever,chat,review_chat=chat,provider=client.provider,model=client.default_model,
                settings=SearchSettings(concurrency=args.concurrency,max_calls=args.max_calls,max_input_tokens=1000000,max_output_tokens=65536),
                corpus_id=str(Path(args.index_dir).resolve()),retrieval_mode='hybrid')
            search=await searcher.run(generation,signature_recovery=parent_search)
            save(out/'fresh_search.json',search.model_dump())
            comparisons=await NoveltyComparator(store,chat,**options).run(generation,search)
            save(out/'fresh_comparisons.json',comparisons.model_dump())
        elif args.recover_incomplete:
            async def repair_interpretation():
                if args.recover_only:return None
                if args.resume_repair:
                    result=ReassessmentResult.model_validate_json(Path(args.resume_repair).read_text())
                    if result.parent!=source.inputs.comparisons or result.concerns!=concerns: raise ValueError('repair checkpoint mismatch')
                else:
                    runner=InterpretationReassessor(store,source.inputs.comparisons,concerns,chat,**options)
                    try:result=await runner.reassess(source.inputs.generation,source.inputs.search)
                    finally:
                        if hasattr(runner,'fresh_result'):save(out/'interpretation_attempt.json',runner.fresh_result.model_dump())
                save(out/'reassessment.json',result.model_dump());return result
            async def recover_papers():
                if args.resume_recovery:
                    result=ComparisonRecoveryResult.model_validate_json(Path(args.resume_recovery).read_text())
                    if result.parent!=source.inputs.comparisons: raise ValueError('recovery checkpoint mismatch')
                else:
                    pending={(c.direction_id,p.paper_id) for c in source.inputs.comparisons.candidates for p in c.papers if p.status!='complete'}
                    feedback=[c for c in concerns if (c.direction_id,c.paper_id) in pending] if args.recovery_concerns else []
                    result=await IncompleteComparisonRecovery(store,source.inputs.comparisons,chat,concerns=feedback,**options).recover(source.inputs.generation,source.inputs.search)
                save(out/'recovery_attempt.json',result.model_dump());return result
            results=await asyncio.gather(repair_interpretation(),recover_papers(),return_exceptions=True)
            for result in results:
                if isinstance(result,BaseException):raise result
            repaired,recovery=results
            recovery=ComparisonRecoveryResult(parent=repaired.merged if repaired else source.inputs.comparisons,fresh=recovery.fresh,
                merged=merge_recovery(repaired.merged if repaired else source.inputs.comparisons,recovery.fresh))
            save(out/'recovery.json',recovery.model_dump())
        elif args.resume_repair:
            repaired=ReassessmentResult.model_validate_json(Path(args.resume_repair).read_text())
            if repaired.parent!=source.inputs.comparisons or repaired.concerns!=concerns:raise ValueError('resume input differs from reviewed repair')
        else:
            runner=InterpretationReassessor(store,source.inputs.comparisons,concerns,chat,**options)
            try:repaired=await runner.reassess(source.inputs.generation,source.inputs.search)
            finally:
                if hasattr(runner,'fresh_result'):save(out/'fresh_comparison.json',runner.fresh_result.model_dump())
        if repaired:
            save(out/'reassessment.json',repaired.model_dump());print('Scoped comparison repair independently complete.',flush=True)
        previous=NoveltyAssessmentResult.model_validate_json(Path(args.reopen_assessment).read_text()) if args.reopen_assessment else None
        observations=json.loads(Path(args.observations).read_text()) if args.observations else None
        assessment=await NoveltyAssessor(chat,**options).run(generation,search,
            recovery.merged if recovery else repaired.merged if repaired else comparisons,
            previous=previous,observations=observations if previous else None,guidance=observations if not previous else None)
        save(out/'assessment.json',assessment.model_dump())
        critic=await ResearchCritic(store,chat,**options).run(assessment)
        save(out/'critic.json',critic.model_dump());final=critic.handoff()
        if not args.skip_refinement and any(any(requests_for(c)) for c in critic.candidates):
            result=await RefinementLoop(store,chat,retriever=retriever,checkpoint=checkpoint,max_cycles=args.max_cycles,**options).run(critic)
            save(out/'refinement.json',result.model_dump());final=result.handoff()
        summary=dict(handoff=final,api_calls=sum(not e.get('checkpoint_replay',False) for e in events),
            checkpoint_replays=sum(bool(e.get('checkpoint_replay')) for e in events),
            input_targets=sum(len(c.coverage) for c in source.candidates),
            repaired_targets=[c.model_dump() for c in concerns] if repaired else [],
            assessment_published=[bool(c.assessment) for c in assessment.candidates])
        save(out/'summary.json',summary);print(json.dumps(summary,indent=2),flush=True)
    finally:await retriever.close();await client.raw.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assessment',required=True);p.add_argument('--concerns',required=True);p.add_argument('--out',required=True)
    p.add_argument('--reopen-assessment');p.add_argument('--observations')
    p.add_argument('--resume-repair');p.add_argument('--replay-dir',action='append',default=[])
    p.add_argument('--root',default='/srv/research_finder/markdown');p.add_argument('--index-dir',default='/srv/research_finder/index')
    p.add_argument('--concurrency',type=int,choices=range(1,33),default=4)
    p.add_argument('--max-calls',type=int,default=1500)
    p.add_argument('--recover-incomplete',action='store_true')
    p.add_argument('--resume-recovery')
    p.add_argument('--resume-search',help='Resume a withheld signature using its saved revision feedback, then run full retrieval/comparison.')
    p.add_argument('--skip-refinement',action='store_true',help='Finish at the reviewed critic when resuming the final allowed scientific cycle.')
    p.add_argument('--recover-only',action='store_true')
    p.add_argument('--recovery-concerns',action='store_true',help='Verify saved source concerns while recovering incomplete papers.')
    p.add_argument('--max-cycles',type=int,choices=(1,2),default=2)
    asyncio.run(main(p.parse_args()))
