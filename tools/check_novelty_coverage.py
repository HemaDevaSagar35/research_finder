"""Report predeclared related-paper coverage, without making novelty labels."""
import argparse
import hashlib
import json
from pathlib import Path

from novelty.schemas import NoveltySearchResult
from tools.validate_novelty_search import save


def coverage(result, controls):
    rows=[]
    for control in controls:
        matches=[]
        for candidate in result.candidates:
            for search in candidate.searches:
                pool=[p.paper_id for p in search.retrieval.papers] if search.retrieval else []
                ranked=[p.paper_id for p in search.ranked]
                pid=control['paper_id']
                matches.append({'direction_id':candidate.direction_id,'target_id':search.target_id,
                    'status':search.status,'pool_rank':pool.index(pid)+1 if pid in pool else None,
                    'shortlist_rank':ranked.index(pid)+1 if pid in ranked else None})
        rows.append({**control,'targets':matches,
            'found_in_any_pool':any(r['pool_rank'] is not None for r in matches),
            'found_in_any_shortlist':any(r['shortlist_rank'] is not None for r in matches)})
    return {'controls':rows,'controls_in_pools':sum(r['found_in_any_pool'] for r in rows),
        'controls_in_shortlists':sum(r['found_in_any_shortlist'] for r in rows),
        'control_count':len(rows),
        'complete_targets':sum(s.status=='complete' for c in result.candidates for s in c.searches),
        'note':'Targeted related-paper coverage, not a representative recall estimate or evidence of novelty. Distinguish failure to retrieve from exclusion during relevance ranking.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result',required=True);parser.add_argument('--controls',required=True)
    parser.add_argument('--topic',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--root',default='/srv/research_finder/markdown')
    args=parser.parse_args()
    result=NoveltySearchResult.model_validate_json(Path(args.result).read_text())
    controls=json.loads(Path(args.controls).read_text())['controls'][args.topic]
    for c in controls:
        raw=(Path(args.root)/c['paper_id']/f"{c['page']:02d}.md").read_bytes()
        if hashlib.sha256(raw).hexdigest()!=c['sha256']:raise ValueError('control source changed')
    report=coverage(result,controls)
    save(Path(args.out),report)
    print(json.dumps(report))


if __name__=='__main__':main()
