"""Paired live signature-review controls for unsupported implementation premises."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path

from directions.judge import validate_report
from directions.schemas import GenerationResult, CorrectnessResponse
from llm_client import AsyncLLMClient
from novelty import prompts
from novelty.pipeline import NoveltySearcher, Settings, validate_signature
from novelty.schemas import NoveltySearchResult
from reasoning.evidence import PaperStore
from tools.validate_novelty_search import save


async def main(args):
    generation = GenerationResult.model_validate_json(Path(args.directions).read_text())
    prior = NoveltySearchResult.model_validate_json(Path(args.signatures).read_text())
    direction = generation.directions[0]
    original = prior.candidates[0].signature
    # Named, predeclared historical failure, not a runtime text-matching rule.
    index = next(i for i,f in enumerate(original.targets[0].mechanism)
                 if f.text == 'Because the decision is left implicit, Chain-of-Note defaults to answer-everything.')
    positive = original.model_copy(deep=True)
    # Remove two additional defects discovered in the first positive-control run.
    # These are fixture edits only; production signatures are revised by the model.
    open_problem = positive.targets[0].problem[4]
    if 'do not resolve whether' not in open_problem.text:
        raise ValueError('historical control layout changed')
    open_problem.basis = 'unknown'
    allocation = positive.targets[0].mechanism[1]
    allocation.basis = 'unknown'
    allocation.text = 'Whether generating per-document notes allocates an explicit answer/refuse decision step in the original comparator is unknown from the supplied pages.'
    unknown = positive.targets[0].mechanism[index]
    unknown.basis = 'unknown'
    unknown.text = 'Whether the original Chain-of-Note prompt explicitly specifies an answer/refuse decision is unknown from the supplied material.'
    conditional = unknown.model_copy(update={'basis':'candidate_proposal',
        'text':'If an explicit decision step is absent or ineffective, that might explain the observed answer-everything behavior; a constructed with/without-decision-step comparison could test this explanation.'},deep=True)
    positive.targets[0].mechanism.append(conditional)
    supported = positive.model_copy(deep=True)
    supported.targets[0].mechanism[index] = next(f.model_copy(deep=True) for f in original.targets[0].mechanism
        if f.basis == 'source_fact' and 'writes per-document reading notes' in f.text)
    controls = [('unsupported_premise',original,'revise'),('unknown_and_conditional',positive,'pass'),('source_supported_detail',supported,'pass')]
    out = Path(args.out);out.mkdir(parents=True,exist_ok=False)
    save(out/'controls.json',{'prompt':prompts.REVIEW_VERSION,'cases':[
        {'name':n,'signature':s.model_dump(),'expected':e} for n,s,e in controls],
        'rubric':'Reject categorical unsupported original implementation premise even under proposal label; allow explicit unknown plus conditional explanation; allow details actually supported by original text.'})
    client = AsyncLLMClient('deepseek',concurrency=3,max_retries=0,timeout=300)
    async def execute(name,signature,expected):
        async def chat(**kw):
            response = await client.chat_result(**kw)
            save(out/'calls'/(name+'.json'),{'request':kw,'response':asdict(response)})
            return response
        runner = NoveltySearcher(PaperStore(Path(args.root)),None,chat,provider=client.provider,
            model=client.default_model,settings=Settings(max_calls=1,max_queries=2))
        payload,_ = await runner._context(direction,{})
        validate_signature(signature,direction,payload,2)
        try:
            response,model = await runner._call('review_signature',prompts.REVIEW,CorrectnessResponse,
                {'payload':payload,'signature':signature.model_dump()})
            validate_report(response,signature,payload)
            row={'case':name,'expected':expected,'decision':response.decision,'matched':response.decision==expected,
                'report':response.model_dump(),'model':model}
        except Exception as exc:
            row={'case':name,'expected':expected,'matched':False,'error':str(exc)}
        save(out/(name+'.json'),row);return row
    try:
        rows=await asyncio.gather(*(execute(*c) for c in controls))
        save(out/'summary.json',{'controls':rows,'review_prompt':prompts.REVIEW_VERSION})
        print(json.dumps(rows));return all(r['matched'] for r in rows)
    finally:await client.raw.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directions',required=True);parser.add_argument('--signatures',required=True)
    parser.add_argument('--out',required=True);parser.add_argument('--root',default='/srv/research_finder/markdown')
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
