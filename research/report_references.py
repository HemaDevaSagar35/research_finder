"""Portable source records and links for the human-readable run report."""
import hashlib
import json
import os
import re
from pathlib import Path

TOKEN = re.compile(r'(?:observation|finding):t\d+-[a-z]\d+(?:/t\d+-e\d+)?|\bdir-\d+(?:-[he]\d+)?\b|\b[a-f0-9]{12}(?:#p\d+(?::[a-f0-9]+)?)?\b|(?<![\w/])/(?:claims_and_evidence|[a-z_]+)/(?:\d+)(?:/[\w-]+)*|\b(?:[Cc]\d+|[Dd]\d+|[Pp][Cc]\d+|igpo-c\d+)\b')
# Existing links and HTML tags must not be rewritten.
PROTECTED = re.compile(r'(!?\[[^\]]*\]\([^)]*\)|<[^>]*>)')


def anchor(token):
    return 'ref-'+hashlib.sha256(token.encode()).hexdigest()[:16]


def pointer(document, path):
    for part in path.strip('/').split('/'):
        part=part.replace('~1','/').replace('~0','~')
        document=document[int(part)] if isinstance(document,list) else document[part]
    return document


def export_references(out, result):
    """Do not infer claim ownership from prose: ambiguous IDs show all scoped records."""
    out=Path(out);sources=out/'evidence'/'sources';sources.mkdir(parents=True,exist_ok=True)
    artifacts={};global_records={}
    def add(reg,key,value):
        if not key:return
        records=reg.setdefault(key,[])
        if value not in records:records.append(value)
    for h in result['hypotheses']:
        for a in (h.get('packet') or {}).get('support_context',{}).get('paper_artifacts',[]):
            artifacts[a['paper_id']]=a
        add(global_records,h['hypothesis_id'],dict(kind='hypothesis',hypothesis=h['proposal'],status=h['status']))
        add(global_records,h['direction_id'],dict(kind='direction',title=h['direction_title'],**h['direction_context']))
        for e in h['linked_experiments']:
            add(global_records,e['proposal']['experiment_id'],dict(kind='experiment',**e))
    mappings={}
    for h in result['hypotheses']:
        hid=h['hypothesis_id'];reg={k:list(v) for k,v in global_records.items()}
        context=h['reading_context']
        for e in context['supporting_evidence']:
            pid=e['paper_id'];path=e['value_path']
            record=dict(kind='supporting evidence',paper=e['paper'],evidence_id=e['evidence_id'],source_path=path,
                        extracted_record=e)
            try:
                record['original_source_record']=pointer(artifacts[pid]['paper'],path)
                record['artifact_sha256']=artifacts[pid]['sha256']
            except (KeyError,IndexError,TypeError,ValueError):
                record['original_source_unavailable']='Original paper-analysis record unavailable; saved extracted evidence is retained.'
            for key in (e['evidence_id'],e['evidence_id'].split('/')[0],path):add(reg,key,record)
            add(reg,pid,dict(kind='paper',**e['paper']))
            for page in e.get('source_pages',[]):add(reg,page['page_id'],dict(kind='source page',**page))
        # Include direction-level provenance referenced in the introductory problem.
        for e in (h.get('packet') or {}).get('opportunity',{}).get('evidence',[]):
            key=e['evidence_id'].split('/')[0]
            add(reg,key,dict(kind='direction supporting evidence',**e))
        for e in context['prior_work']:
            pid=e['paper_id'];add(reg,pid,dict(kind='paper comparison',paper=e['paper'],comparison=e['comparison']))
            for c in e['claims']:
                add(reg,c['claim_id'],{**c,'kind':'paper claim','paper':e['paper'],'target_id':hid})
                for passage in c['source_passages']:
                    add(reg,passage['passage_id'],dict(kind='source passage',paper=e['paper'],**passage))
                    if passage.get('page_id'):add(reg,passage['page_id'],dict(kind='saved passage from page',paper=e['paper'],**passage))
        for row in context['coverage']['papers']:
            add(reg,row['paper_id'],dict(kind='comparison coverage',**row))
        mappings[hid]=reg
    # Resolve direction provenance even for hypotheses without a review packet.
    shared={}
    for reg in mappings.values():
        for key,records in reg.items():
            if key.startswith(('observation:','finding:')):
                for r in records:add(shared,key,r)
    for reg in mappings.values():
        for key,records in shared.items():
            if key not in reg:reg[key]=records
    paths=[out/'summary.md']+list((out/'proposals').glob('*/*.md'))+list((out/'evidence').glob('*.md'))
    seen_tokens={}
    for file in paths:
        hid=file.stem
        reg=mappings.get(hid,global_records)
        tokens=seen_tokens.setdefault(hid,set())
        def replace(part):
            def sub(match):
                token=match.group();tokens.add(token)
                target=sources/(hid+'.md')
                href=Path(os.path.relpath(target,file.parent)).as_posix()+'#'+anchor(token)
                return f'[{token}]({href})'
            # Backticks around linked references prevent Markdown links rendering.
            part=re.sub(r'`([^`\n]+)`',lambda m:m[1] if TOKEN.search(m[1]) else m[0],part)
            return TOKEN.sub(sub,part)
        text=file.read_text();chunks=PROTECTED.split(text)
        text=''.join(chunk if i%2 else replace(chunk) for i,chunk in enumerate(chunks))
        file.write_text(text)
        if not tokens:continue
        lines=['# Source reference index','',
               'References below resolve to saved records. A short claim ID can occur in multiple papers; all matching records for this hypothesis are listed with paper identity.','']
        for token in sorted(tokens):
            records=reg.get(token,[]);name=hid+'-'+anchor(token)+'.json'
            payload=dict(reference=token,records=records,availability='available' if records else 'source unavailable in saved export')
            (sources/name).write_text(json.dumps(payload,indent=2)+'\n')
            lines += [f'<a id="{anchor(token)}"></a>',f'## {token}','',f'[Saved source record (JSON)]({name})','']
            if not records:lines += ['**Source unavailable:** this reference could not be resolved from saved artifacts.','']
            for r in records:
                paper=r.get('paper',{})
                lines += [f"**Record type:** {r['kind']}",'']
                if paper.get('title'):lines += ['**Paper:** '+paper['title'],'']
                for key in ('text','source_value','original_source_unavailable'):
                    if r.get(key):lines += [str(r[key]),'']
                if r.get('extracted_record'):lines += [r['extracted_record']['source_value'],'']
                if r.get('original_source_record') is not None:
                    lines += ['Original structured record is included in the linked JSON, alongside the extraction and artifact hash.','']
        # Reference-index headings are the definitions of IDs; not dangling references.
        (sources/(hid+'.md')).write_text('\n'.join(lines))
