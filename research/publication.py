"""Stage 16: publish saved hypothesis decisions without running models or retrieval."""
from copy import deepcopy
from novelty.assessment_evidence import build_packet
from opportunities.miner import digest
from critic.evidence import packet_for, validate_draft, validate_review
from critic.schemas import CritiqueDraft, ReviewRecord
from critic.hypotheses import eligibility


def reading_context(critic, direction, row, base, metadata, support):
    """Resolve saved evidence for reading, without changing scientific decisions."""
    hid=row['hypothesis_id']
    def paper(pid):
        return dict(paper_id=pid, **metadata.get(pid, {'title':'Title unavailable in saved metadata'}))
    passages={p['passage_id']:p for p in base['source_passages']}
    comparisons=[]
    for e in base['evidence']:
        if e['target_id']!=hid: continue
        entry=deepcopy(e)
        entry['paper']=paper(e['paper_id'])
        for claim in entry['claims']:
            claim['source_passages']=[deepcopy(passages[pid]) if pid in passages else
                dict(passage_id=pid,text='Passage unavailable in saved checkpoint') for pid in claim['passage_ids']]
        comparisons.append(entry)
    evidence=[]
    for e in direction.opportunity.evidence:
        if e.evidence_id not in row['proposal']['evidence_ids']: continue
        entry=e.model_dump();entry['paper']=paper(e.paper_id)
        page_ids={f'{e.paper_id}#p{loc.page}' for loc in e.source_locations if loc.page is not None}
        entry['source_pages']=[deepcopy(p) for p in support.get('pages',[]) if p['page_id'] in page_ids]
        evidence.append(entry)
    assessment=next(c for c in critic.novelty.candidates if c.direction_id==direction.direction_id)
    judgments=[]
    sources=[assessment.assessment]+[c.assessment for c in assessment.hypothesis_assessments or []]
    for a in sources:
        if a:
            for t in a.targets:
                if t.target_id==hid and t.model_dump() not in judgments: judgments.append(t.model_dump())
    return dict(supporting_evidence=evidence,
        missing_support_evidence_ids=sorted(set(row['proposal']['evidence_ids'])-{e['evidence_id'] for e in evidence}),
        prior_work=comparisons, novelty_judgments=judgments,
        coverage=next(c for c in base['coverage'] if c['target_id']==hid),
        coverage_threshold=base.get('coverage_threshold',1.0))


def saved_metadata(value, result):
    if isinstance(value,list):
        for v in value: saved_metadata(v,result)
    elif isinstance(value,dict):
        pid=value.get('paper_id')
        meta=value.get('paper',{}).get('paper_metadata',{}) if isinstance(value.get('paper'),dict) else {}
        meta=meta or value.get('metadata',{})
        title=meta.get('title') or value.get('title')
        if pid and title:
            result[pid]={k:v for k,v in (meta or value).items() if k in ('title','authors','year','venue','identifiers','url','doi')}
        for v in value.values(): saved_metadata(v,result)


def stage16(critic, report):
    if report['assessment_sha256'] != digest(critic.novelty.model_dump()):
        raise ValueError('Hypothesis report belongs to a different assessment')
    directions={d.direction_id:d for d in critic.novelty.inputs.generation.directions}
    expected={(d.direction_id,h.hypothesis_id) for d in directions.values() for h in d.proposal.hypotheses}
    rows=report['hypotheses']
    if len(rows)!=len(expected) or {(h['direction_id'],h['hypothesis_id']) for h in rows}!=expected:
        raise ValueError('Hypothesis report must preserve every original hypothesis')
    output=[]
    metadata={}
    saved_metadata(critic.novelty.inputs.search.model_dump(),metadata)
    supports={}
    for r in rows:
        packet=r.get('packet')
        if packet:
            supports[r['direction_id']]=packet.get('support_context',{})
            saved_metadata(packet.get('support_context',{}),metadata)
    for c in critic.candidates:
        if c.packet:
            supports.setdefault(c.direction_id,c.packet.get('support_context',{}))
            saved_metadata(c.packet.get('support_context',{}),metadata)
    bases={did:build_packet(critic.novelty.inputs,did) for did in directions}
    statuses={(h['direction_id'],h['hypothesis_id']):h['status'] for h in rows}
    for row in rows:
        did,hid=row['direction_id'],row['hypothesis_id']
        direction=directions[did]
        hypothesis=next(h for h in direction.proposal.hypotheses if h.hypothesis_id==hid)
        if row['proposal']!=hypothesis.model_dump(): raise ValueError('Hypothesis proposal changed')
        if row['status'] not in ('approved','pending','discarded'): raise ValueError('Unknown hypothesis status')
        if row['status'] in ('approved','discarded'):
            assessment=next(c for c in critic.novelty.candidates if c.direction_id==did)
            if eligibility(assessment,direction,hid): raise ValueError('Ineligible hypothesis cannot be published as reviewed')
            published=next(c for c in critic.candidates if c.direction_id==did)
            packet=row['packet']
            if published.critique and packet==published.packet:
                if row['critique']!=published.critique.model_dump() or row['reviews']!=[r.model_dump() for r in published.reviews]:
                    raise ValueError('Hypothesis differs from accepted parent critique')
            else:
                reconstructed=packet_for(critic.novelty,direction,packet['support_context'],hypothesis_id=hid)
                if packet.get('review_target_ids')!=[hid] or {k:v for k,v in packet.items() if k not in ('review_target_ids','review_scope')}!=reconstructed:
                    raise ValueError('Independent review packet differs from original evidence')
            draft=CritiqueDraft.model_validate(row['critique'])
            history=[ReviewRecord.model_validate(r) for r in row['reviews']]
            if not history or history[-1].draft!=draft or not history[-1].report or history[-1].report.decision!='pass':
                raise ValueError('Publication requires independent acceptance of the exact critique')
            for review in history:
                validate_draft(review.draft,packet)
                if review.report: validate_review(review.report,review.draft,packet)
                if review.draft.upstream_requests or (review.report and review.report.upstream_requests):
                    raise ValueError('Unresolved source request cannot be published as approved')
            action=next(t.action for t in draft.targets if t.target_id==hid)
            if row['status']!=('approved' if action in ('KEEP','DOWNRANK') else 'discarded' if action=='DISCARD' else 'pending'):
                raise ValueError('Hypothesis status differs from reviewed action')
        item=deepcopy(row)
        item['direction_title']=direction.proposal.title
        item['direction_context']=dict(research_direction=direction.proposal.research_direction,
            rationale=[r.model_dump() for r in direction.proposal.rationale],
            opportunity=direction.opportunity.candidate.model_dump())
        item['reading_context']=reading_context(critic,direction,row,bases[did],metadata,supports.get(did,{}))
        item['linked_experiments']=[dict(proposal=e.model_dump(),hypothesis_statuses={h:statuses[(did,h)] for h in e.hypothesis_ids}) for e in direction.proposal.experiments if hid in e.hypothesis_ids]
        output.append(item)
    counts={s:sum(h['status']==s for h in output) for s in ('approved','pending','discarded')}
    return dict(schema_version='stage16_hypotheses_v1',status='partial' if counts['approved'] and counts['pending'] else 'ready' if counts['approved'] else 'blocked' if counts['pending'] else 'empty',counts=counts,hypotheses=output,critic_sha256=digest(critic.model_dump()),hypothesis_reviews_sha256=digest(report),scientific_validation='model_reviewed_not_empirically_validated',automatic_revision=False)


def markdown(result):
    lines=['# Stage 16 — reviewed hypotheses','',f"Approved: {result['counts']['approved']}; pending: {result['counts']['pending']}; discarded: {result['counts']['discarded']}.",'','Approval applies to the individual hypothesis, not its entire direction or empirical truth. Linked experiment dependencies are preserved. REFINE remains a proposed next action; no automatic revision or new search ran.','']
    for did in dict.fromkeys(h['direction_id'] for h in result['hypotheses']):
        group=[h for h in result['hypotheses'] if h['direction_id']==did]
        first=group[0]
        lines += [f"## {did}: {first['direction_title']}",'',first['direction_context']['research_direction'],'',
            '**Research gap:** '+first['direction_context']['opportunity']['question'],'']
        for h in group:
            p=h['proposal'];ctx=h['reading_context']
            lines += [f"### {h['hypothesis_id']} — {h['status'].upper()}",'']
            for field in ('condition','intervention','expected_effect','mechanism','assumptions','falsification_criterion'):
                value=p[field]
                lines += [f"**{field.replace('_',' ').title()}:** "+('; '.join(value) if isinstance(value,list) else value),'']
            lines += ['**Review outcome:** '+(h['diagnostic'] or 'Accepted independent scientific review.'),'']
            for t in (h.get('critique') or {}).get('targets',[]):
                if t['target_id']==h['hypothesis_id']: lines += [f"**{t['action']}:** {t['rationale']}",'']
            lines += ['#### Supporting papers and evidence','']
            for e in ctx['supporting_evidence']:
                lines += [f"**{e['paper']['title']}** (`{e['paper_id']}`)",'',
                    f"Evidence `{e['evidence_id']}`; source record `{e['value_path']}`.",'',
                    e['source_value'],'', 'Source locations: '+str(e['source_locations']),'']
                for page in e['source_pages']:
                    lines += ['<details>',f"<summary>Original saved page {page['page_id']}</summary>",'',page['text'],'','</details>','']
                if not e['source_pages']: lines += ['Original page text unavailable in this export; the saved evidence record and source locations are shown above.','']
            if not ctx['supporting_evidence']: lines += ['No supporting evidence record could be resolved from the saved opportunity.','']
            if ctx['missing_support_evidence_ids']: lines += ['Unresolved evidence references: '+', '.join(ctx['missing_support_evidence_ids']),'']
            lines += ['#### Novelty and prior-work comparisons','']
            cov=ctx['coverage'];total=len(cov['papers']);valid=sum(p['status']=='reviewed' and not p['missing_referenced_pages'] for p in cov['papers'])
            lines += [f"Comparison coverage: **{valid}/{total}**; required threshold: **{ctx['coverage_threshold']:.0%}**. Coverage gate satisfied: **{cov['complete']}**. This is evidence coverage, not a novelty percentage.",'']
            for j in ctx['novelty_judgments']: lines += [f"**{j['finding']}:** {j['reasoning']}",'']
            if not ctx['novelty_judgments']: lines += ['No accepted novelty judgment is available for this hypothesis.','']
            for e in ctx['prior_work']:
                lines += [f"**{e['paper']['title']}** (`{e['paper_id']}`) — {e['comparison']['classification']}",'',e['comparison']['rationale'],'',
                    '<details>','<summary>Evidence claims and exact saved passages</summary>','']
                for c in e['claims']:
                    lines += [f"**Claim {c['claim_id']}:** {c['text']}",'']
                    for passage in c['source_passages']:
                        lines += [f"Source `{passage['passage_id']}`:",'', '\n'.join('> '+line for line in passage['text'].splitlines()),'']
                lines += ['</details>','']
            for p in cov['papers']:
                if p['status']!='reviewed' or p['missing_referenced_pages']:
                    lines += [f"- Missing/incomplete comparison `{p['paper_id']}`: {p['status']}; "+'; '.join(p['reasons'])+f"; missing pages: {p['missing_referenced_pages']}"]
            lines += ['','#### Proposed experiments','']
            for e in h['linked_experiments']:
                ep=e['proposal'];lines += [f"**{ep['experiment_id']}**",'']
                for k in ('objective','comparison','observations','why_this_test','informative_outcomes'):
                    v=ep[k];lines += [f"**{k.replace('_',' ').title()}:** "+('; '.join(v) if isinstance(v,list) else v),'']
                lines += ['Dependencies: '+', '.join(f'{k}: {v}' for k,v in e['hypothesis_statuses'].items()),'']
    return '\n'.join(lines)


def write_stage16(out,critic,report):
    from research.pipeline import atomic_json
    result=stage16(critic,report)
    atomic_json(out/'stage16.json',result)
    from research.reading_report import write_reading_report
    write_reading_report(out,result)
    return result
