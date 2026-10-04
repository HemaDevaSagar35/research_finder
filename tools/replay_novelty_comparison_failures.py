"""Offline replay of saved v1 evidence through v2 deterministic source/candidate handling.

This verifies representation and reference integrity, not semantic acceptance of old claims.
"""
import argparse
import hashlib
import json
from pathlib import Path
from pydantic import ValidationError
from novelty.comparison_evidence import passages_for_pages, candidate_view
from novelty.comparison_schemas import DimensionComparison
from novelty.schemas import FACETS, TargetSignature
from tools.validate_novelty_search import save


def replay(paths):
    seen=set(); rows=[]; invalid_quotes=[]; rejected_legacy=0
    for path in sorted(paths):
        raw=json.loads(path.read_text())
        request=json.loads(raw['request']['messages'][1]['content'])
        payload=request.get('payload',{})
        if 'prior_pages' not in payload: continue
        key=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            passages=passages_for_pages(payload['prior_pages'])
            for page in payload['prior_pages']:
                refs=[p for p in passages if p.page_id==page['page_id']]
                assert all(page['text'][p.start:p.end]==p.text for p in refs)
                covered={i for p in refs for i in range(p.start,p.end)}
                assert all(i in covered for i,char in enumerate(page['text']) if not char.isspace())
            unknowns=0
            for obj in payload['targets']:
                target=TargetSignature.model_validate(obj); view=candidate_view(target)
                assert view['target']==obj
                expected=[f.model_dump() for k in FACETS for f in getattr(target,k) if f.basis=='unknown']
                assert [u['facet'] for u in view['unknowns']]==expected
                unknowns+=len(expected)
            rows.append({'file':str(path),'paper_id':payload['prior_paper_id'],'pages':len(payload['prior_pages']),
                'passages':len(passages),'targets':len(payload['targets']),'unknown_facets_preserved':unknowns})
        # Examine returned comparison/review text when it is parseable; retain old errors as evidence.
        try: response=json.loads(raw['response']['text'])
        except (ValueError,KeyError): continue
        pages={p['page_id']:p['text'] for p in payload['prior_pages']+payload['source_context']['pages']}
        def walk(node,path_in=''):
            if isinstance(node,dict):
                if 'page_id' in node and 'quote' in node:
                    if node['quote'] not in pages.get(node['page_id'],''):
                        invalid_quotes.append({'file':str(path),'field':path_in,'page_id':node['page_id']})
                for k,v in node.items():walk(v,path_in+'/'+k)
            elif isinstance(node,list):
                for i,v in enumerate(node):walk(v,path_in+'/'+str(i))
        walk(response)
        for pair in (response.get('comparison') or {}).get('pairs',[]):
            for dimension in pair['dimensions']:
                try:DimensionComparison.model_validate(dimension)
                except ValidationError: rejected_legacy+=1
                else:raise AssertionError('v1 candidate paraphrase/quote unexpectedly accepted by v2')
    assert rows, 'No historical v1 payloads found'
    return {'payloads':rows,'unique_payload_count':len(rows),
        'historical_inexact_quote_occurrences':invalid_quotes,
        'legacy_dimension_outputs_rejected':rejected_legacy,
        'source_character_coverage':'all nonwhitespace characters retained',
        'candidate_preservation':'all target fields and unknown facets retained exactly',
        'semantic_acceptance':'not_assessed; original failed claims have not been promoted or silently migrated'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='append',required=True)
    p.add_argument('--out',required=True)
    args=p.parse_args();out=Path(args.out)
    if out.exists():raise ValueError('Choose a new output file')
    report=replay([f for root in args.run for f in (Path(root)/'calls').glob('*.json')])
    save(out,report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('payloads','historical_inexact_quote_occurrences')},indent=2))
    print('Historical inexact quote occurrences:',len(report['historical_inexact_quote_occurrences']))


if __name__=='__main__':main()
