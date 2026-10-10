"""Recover failed downstream work from immutable saved research artifacts."""
import argparse
import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone
from research.pipeline import PipelineConfig, StageRunner, STAGES, atomic_json, run_lock
from research.__main__ import StageProgress, automatic_output
from llm_client.progress import stage_progress
from opportunities.miner import digest
from novelty.pipeline import NoveltySearcher
from novelty.comparison import NoveltyComparator
from novelty.assessment import NoveltyAssessor
from critic.hypotheses import review_hypotheses, markdown as hypothesis_markdown
from portfolio.render import markdown


def load_source(root):
    manifest=json.loads((root/'manifest.json').read_text())
    parent=digest(manifest)
    state={}
    for stage, schema in STAGES.items():
        path=root/f'{stage}.json'
        if not path.exists(): break
        payload=json.loads(path.read_text())
        if payload['stage']!=stage or payload['parent_sha256']!=parent or payload['result_sha256']!=digest(payload['result']):
            raise ValueError('Invalid source checkpoint: '+stage)
        state[stage]=schema.model_validate(payload['result'])
        parent=digest(payload)
    for required in ('directions','novelty_search','comparison'):
        if required not in state: raise ValueError('Recovery needs saved '+required)
    return PipelineConfig.model_validate(manifest['config']),state,manifest


async def recover(source,out,rounds=2):
    config,state,manifest=load_source(source)
    out.mkdir(parents=True,exist_ok=False)
    progress=StageProgress()
    with run_lock(out):
        atomic_json(out/'recovery_manifest.json',dict(source=str(source.resolve()),
            source_manifest_sha256=digest(manifest),config=config.model_dump(mode='json'),rounds=rounds,
            started=datetime.now(timezone.utc).isoformat()))
        runner=StageRunner(config)
        common=dict(provider=config.provider,model=config.model,review_model=config.review_model)
        async def save_stage(name,call):
            progress(name,'running')
            try:
                async with stage_progress(name,progress): value=await call
                data=value.model_dump(mode='json') if hasattr(value,'model_dump') else value
                atomic_json(out/f'{name}.json',dict(result=data,result_sha256=digest(data)))
                progress(name,'completed')
                return value
            except BaseException:
                progress(name,'failed')
                raise
        try:
            original_assessment=state.get('assessment')
            for i in range(rounds):
                searcher=NoveltySearcher(runner.store,runner.get_retriever(),settings=config.novelty_search,**common)
                state['novelty_search']=await save_stage(f'novelty_search_recovery_{i+1}',
                    searcher.run(state['directions'],recovery=state['novelty_search']))
                comparator=NoveltyComparator(runner.store,settings=config.comparison,**common)
                state['comparison']=await save_stage(f'comparison_recovery_{i+1}',
                    comparator.run(state['directions'],state['novelty_search'],recovery=state['comparison']))
                if all(c.signature is not None and all(s.status=='complete' for s in c.searches) for c in state['novelty_search'].candidates) and all(p.status=='complete' for c in state['comparison'].candidates for p in c.papers):
                    break
            state['assessment']=await save_stage('assessment',NoveltyAssessor(settings=config.assessment,**common).run(
                state['directions'],state['novelty_search'],state['comparison'],recovery=original_assessment))
            state['critic']=await save_stage('critic',runner.execute('critic',state))
            hypotheses=runner.hypothesis_reviews
            atomic_json(out/'hypothesis_reviews.json',hypotheses)
            if config.max_refinement_cycles:
                state['refinement']=await save_stage('refinement',runner.execute('refinement',state))
            result=await save_stage('portfolio',runner.execute('portfolio',state))
            atomic_json(out/'final_portfolio.json',result.model_dump(mode='json'))
            (out/'final_portfolio.md').write_text(markdown(result)+'\n'+hypothesis_markdown(hypotheses))
            summary=dict(status=result.status,counts=result.counts,
                         hypothesis_counts={s:sum(h['status']==s for h in hypotheses['hypotheses']) for s in ('approved','pending','discarded')},
                         diagnostics=result.diagnostics,portfolio='final_portfolio.json',source=str(source))
            atomic_json(out/'summary.json',summary)
            return summary
        except BaseException as exc:
            atomic_json(out/'summary.json',dict(status='failed',error=f'{type(exc).__name__}: {exc}'))
            raise
        finally:
            await runner.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--from-run',type=Path,required=True)
    p.add_argument('--out',type=Path)
    p.add_argument('--rounds',type=int,choices=(1,2),default=2)
    args=p.parse_args()
    config,_,_=load_source(args.from_run)
    out=args.out or automatic_output(config.query+' recovery')
    print('Recovery output directory:',out,flush=True)
    print(json.dumps(asyncio.run(recover(args.from_run,out,args.rounds)),indent=2),flush=True)

if __name__=='__main__': main()
