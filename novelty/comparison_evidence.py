"""Deterministic source addressing and exact candidate/evidence rendering, no LLM."""
import hashlib
import re
from novelty.schemas import FACETS, TargetSignature
from novelty.comparison_schemas import Passage, check_references
from novelty.pipeline import pointer

DIMENSION_FACETS = {
    'problem':('problem',), 'method':('intervention',), 'mechanism':('mechanism',),
    'signal':('decision_signal',), 'regime':('regime',),
    'evaluation':('comparison','regime','decision_signal'),
    'scientific_question':('problem','intervention','expected_effect'),
    'hypothesis':('intervention','regime','comparison','expected_effect'),
}


def passages_for_pages(pages):
    result=[]
    for page in pages:
        page_id=page['page_id']; text=page['text']
        sha=page.get('sha256') or hashlib.sha256(text.encode()).hexdigest()
        # Blank-line blocks keep prose, equations and complete tables intact.
        # Every non-whitespace source character remains accessible; no relevance pruning.
        for block in re.finditer(r'\S.*?(?=\n[ \t]*\n|\Z)',text,re.S):
            start=block.start(); value=block.group().rstrip(); end=start+len(value)
            result.append(Passage(passage_id=Passage.make_id(page_id,sha,start,end,value),
                page_id=page_id,page_sha256=sha,start=start,end=end,text=value))
    return result


def candidate_view(target):
    fields={key:[{'path':f'/{key}/{i}','facet':f.model_dump()} for i,f in enumerate(getattr(target,key))] for key in FACETS}
    return {'target':target.model_dump(),
        'dimensions':{d:[item for key in keys for item in fields[key]] for d,keys in DIMENSION_FACETS.items()},
        'unknowns':[item for key in FACETS for item in fields[key] if item['facet']['basis']=='unknown']}


def render_comparison(draft,targets,passages):
    check_references(draft,targets,passages)
    by_id={p.passage_id:p for p in passages}
    claims={c.claim_id:{**c.model_dump(),'sources':[by_id[p].model_dump() for p in c.passage_ids]} for c in draft.claims}
    by_target={t.target_id:t for t in targets}
    return {'claims':claims,'pairs':[{'candidate':candidate_view(by_target[p.target_id]),
        'assessment':p.model_dump()} for p in draft.pairs]}


def validate_comparison(draft,payload):
    targets=[TargetSignature.model_validate(t) for t in payload['targets']]
    passages=[Passage.model_validate(p) for p in payload['prior_passages']]
    check_references(draft,targets,passages)


def validate_report(report,draft,passages):
    known={p.passage_id for p in passages}
    document=draft.model_dump()
    errors=[]
    for i,issue in enumerate(report.issues):
        try: pointer(document,issue.field_path)
        except (ValueError,KeyError,IndexError): errors.append(f'/issues/{i}/field_path: unknown comparison field {issue.field_path!r}')
        missing=set(issue.passage_ids)-known
        if missing: errors.append(f'/issues/{i}/passage_ids: unknown/wrong-paper IDs {sorted(missing)}')
    if errors: raise ValueError('; '.join(errors))


def table_context_refs(passages):
    """Bind explicit preceding captions and short setup labels to their next table.

    A caption may be followed by e.g. 'Baseline: SnapKV.' before the table.
    Never borrow the next table's caption or infer cross-page/remote associations.
    IDs and offsets remain those of the original separate source passages.
    """
    caption=re.compile(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?Table\s+(?:\d+|[A-Z]\d*|[IVX]+)\s*[:.]',re.I)
    setup=re.compile(r'^\s*(?:\*\*)?[A-Za-z][A-Za-z0-9 /()_-]{0,40}\s*:')
    note=re.compile(r'^\s*(?:\*\*)?(?:Notes?\s*:|\[\^[^]]+\]:)',re.I)
    def is_table(p):
        return bool(re.search(r'^\s*\|?\s*:?-{2,}:?\s*\|',p.text,re.M))
    contexts={}
    for i,p in enumerate(passages):
        if not caption.match(p.text):continue
        refs=[p.passage_id]
        for q in passages[i+1:]:
            if q.page_id!=p.page_id:break
            if is_table(q):
                contexts[q.passage_id]=refs
                break
            if caption.match(q.text) or len(q.text)>400 or not setup.match(q.text):break
            refs.append(q.passage_id)
    for i,p in enumerate(passages[:-1]):
        q=passages[i+1]
        if is_table(p) and q.page_id==p.page_id and note.match(q.text):
            contexts.setdefault(p.passage_id,[]).append(q.passage_id)
    return contexts


def attach_table_context(record,passages):
    contexts=table_context_refs(passages)
    copy=record.model_copy(deep=True)
    for claim in copy.claims:
        claim.passage_ids=list(dict.fromkeys(claim.passage_ids+[
            ref for pid in claim.passage_ids for ref in contexts.get(pid,[])]))
    return copy
