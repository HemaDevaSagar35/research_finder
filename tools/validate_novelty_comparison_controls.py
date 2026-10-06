"""Paired live review controls: actual passed comparisons vs unsupported 'tested' claims."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from novelty.comparison_evidence import validate_report
from llm_client import AsyncLLMClient
from novelty import comparison_prompts as prompts
from novelty.comparison import validate_comparison
from novelty.comparison_legacy_v2 import ComparisonDraft
from novelty.comparison_schemas import ComparisonReport, Passage
from novelty.pipeline import NoveltySearcher, Settings
from tools.validate_novelty_search import save


async def main(args):
    out = Path(args.out); out.mkdir(parents=True,exist_ok=False)
    cases = []
    for topic, filename, target, claim, page_number, quote in (
        ('rag',args.rag_review,'dir-000-h01',
         'The prior paper experimentally compared prompting-only mandatory answer/refuse tags against matched prompts without the tag, finding increased correct refusals on at least two of three models.',
         8,'Both comparators reach usable answer content but neither acquires the answer/refuse decision.'),
        ('kv',args.kv_review,'dir-000-h03',
         'The prior paper stratified examples into low- and high-sigma groups and experimentally confirmed that the first-order-minus-zeroth-order accuracy advantage is smaller in the high-sigma group.',
         9,'more than triples the gain by capturing query-dependent directional variation.')):
        raw = json.loads(Path(filename).read_text())
        original = json.loads(raw['request']['messages'][1]['content'])
        if json.loads(raw['response']['text'])['decision'] != 'pass':
            raise ValueError('positive fixture must be an actual passing review')
        payload = original['payload']
        baseline = ComparisonDraft.model_validate(original['comparison'])
        validate_comparison(baseline,payload)
        changed = baseline.model_copy(deep=True)
        index = next(i for i,p in enumerate(changed.pairs) if p.target_id == target)
        pair = changed.pairs[index]
        dimension_index = next(i for i,d in enumerate(pair.dimensions) if d.dimension == 'hypothesis')
        page_id = f"{payload['prior_paper_id']}#p{page_number}"
        passage = next(p for p in payload['prior_passages'] if p['page_id']==page_id and quote in p['text'])
        data = changed.model_dump()
        claim_id='injected_false_test'
        data['claims'].append({'claim_id':claim_id,'text':claim,'kind':'result','passage_ids':[passage['passage_id']]})
        data['pairs'][index]['dimensions'][dimension_index].update(relation='SAME',claim_ids=[claim_id],rationale='The referenced result establishes this exact experimental comparison.')
        data['pairs'][index].update(hypothesis_tested='tested',hypothesis_claim_ids=[claim_id])
        used={c for p in data['pairs'] for d in p['dimensions'] for c in d['claim_ids']} | {c for p in data['pairs'] for c in p['hypothesis_claim_ids']}
        data['claims']=[c for c in data['claims'] if c['claim_id'] in used]
        changed = ComparisonDraft.model_validate(data)
        # Existing passage IDs/valid structure cannot establish semantic entailment.
        validate_comparison(changed,payload)
        claim_index=next(i for i,c in enumerate(changed.claims) if c.claim_id==claim_id)
        paths = [f'/claims/{claim_index}',f'/pairs/{index}/dimensions/{dimension_index}',f'/pairs/{index}/hypothesis_tested',f'/pairs/{index}/hypothesis_claim_ids']
        cases += [{'name':topic+'_original','system':raw['request']['messages'][0]['content'],'payload':payload,'draft':baseline,'expected':'pass','issue_paths':[]},
                  {'name':topic+'_false_tested','system':raw['request']['messages'][0]['content'],'payload':payload,'draft':changed,'expected':'revise','issue_paths':paths}]
    save(out/'fixtures.json',[{**c,'draft':c['draft'].model_dump()} for c in cases])
    events=[]
    client=AsyncLLMClient('deepseek',concurrency=4,max_retries=0,timeout=600)
    async def chat(**kw):
        event={'id':len(events),'start':time.time()};events.append(event)
        try:
            response=await client.chat_result(**kw)
            save(out/'calls'/f"{event['id']:03d}.json",{'request':kw,'response':asdict(response)})
            return response
        finally:
            event['end']=time.time();save(out/'calls.json',events)
    runner=NoveltySearcher(None,None,chat,provider=client.provider,model=client.default_model,settings=Settings(max_calls=4,concurrency=4))
    async def one(c):
        try:
            report,_=await runner._call('review_comparison',c['system'],ComparisonReport,
                {'payload':c['payload'],'comparison':c['draft'].model_dump()})
            validate_report(report,c['draft'],[Passage.model_validate(p) for p in c['payload']['prior_passages']])
            caught=not c['issue_paths'] or any(any(i.field_path == p or i.field_path.startswith(p+'/') for p in c['issue_paths']) for i in report.issues)
            return {'name':c['name'],'expected':c['expected'],'matched':report.decision==c['expected'] and caught,'report':report.model_dump()}
        except Exception as exc:
            return {'name':c['name'],'matched':False,'error':str(exc)}
    try:
        results=await asyncio.gather(*(one(c) for c in cases))
        summary={'cases':results,'calls':runner.budget.snapshot(),'note':'Two real passing controls and two deliberately false experimental-attribution claims with valid original passage references; not a statistical accuracy estimate.'}
        save(out/'summary.json',summary);print(json.dumps(summary),flush=True)
        return all(c['matched'] for c in results)
    finally:await client.raw.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('rag-review','kv-review','out'):p.add_argument('--'+name,required=True)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
