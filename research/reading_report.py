"""Offline, hypothesis-oriented reading files for Stage 16."""
import json
from pathlib import Path


def proposal_record(h):
    context=h['direction_context']
    opportunity=context['opportunity']
    return dict(
        hypothesis_id=h['hypothesis_id'], direction_id=h['direction_id'],
        direction_title=h['direction_title'], status=h['status'],
        problem=opportunity['rationale'], research_question=opportunity['question'],
        direction=context['research_direction'], hypothesis=h['proposal']['expected_effect'],
        hypothesis_details=h['proposal'],
        contribution_and_overlap=h['reading_context']['novelty_judgments'],
        scope=opportunity['scope'], limitations=opportunity['uncertainties'],
        review_diagnostic=h['diagnostic'],
        critic_judgments=[t for t in (h.get('critique') or {}).get('targets',[])
                         if t['target_id']==h['hypothesis_id']],
        experiments=h['linked_experiments'],
        coverage=h['reading_context']['coverage'],
        coverage_threshold=h['reading_context']['coverage_threshold'],
        evidence_file=f"../../evidence/{h['hypothesis_id']}.md",
        scientific_validation='model_reviewed_not_empirically_validated')


def proposal_markdown(p):
    lines=[f"# {p['hypothesis_id']} — {p['status'].upper()}",'',
        '[All hypotheses](../../summary.md) · '+f"[Evidence and papers]({p['evidence_file']}) · [JSON]({p['hypothesis_id']}.json)",'']
    for title,key in [('Problem','problem'),('Research question','research_question'),
                      ('Research direction','direction'),('Hypothesis','hypothesis')]:
        lines += [f'## {title}','']
        if key=='direction': lines += [f"**{p['direction_title']}** (`{p['direction_id']}`)",'']
        lines += [p[key],'']
    lines += ['## Contribution and relationship to prior work','']
    for j in p['contribution_and_overlap']:
        lines += [f"**Saved novelty judgment: {j['finding']}**",'',j['reasoning'],'']
        if j.get('meaningful_difference'): lines += ['**Remaining difference:** '+str(j['meaningful_difference']),'']
    if not p['contribution_and_overlap']:
        lines += ['No accepted novelty judgment is available. A contribution has not been established by the saved assessment.','']
    lines += ['## Proposed test','']
    for key,label in [('condition','Setting'),('intervention','What changes'),('mechanism','Proposed explanation'),
                      ('falsification_criterion','What would contradict the hypothesis')]:
        lines += [f"**{label}:** {p['hypothesis_details'][key]}",'']
    for e in p['experiments']:
        lines += [f"### Experiment {e['proposal']['experiment_id']}",'']
        for k in ('objective','comparison','observations','why_this_test','informative_outcomes'):
            v=e['proposal'][k]
            lines += [f"**{k.replace('_',' ').title()}:** "+('; '.join(v) if isinstance(v,list) else v),'']
        lines += ['**Hypothesis dependencies:** '+', '.join(f'{k}: {v}' for k,v in e['hypothesis_statuses'].items()),'']
    if not p['experiments']: lines += ['No linked experiment is recorded.','']
    lines += ['## Status and remaining work','',f"**{p['status'].upper()}**",'',
              p['review_diagnostic'] or 'See the saved scientific review below.','']
    for t in p['critic_judgments']: lines += [f"**{t['action']}:** {t['rationale']}",'']
    cov=p['coverage'];n=len(cov['papers']);valid=sum(r['status']=='reviewed' and not r['missing_referenced_pages'] for r in cov['papers'])
    lines += [f"Usable comparisons: {valid}/{n}; required coverage: {p['coverage_threshold']:.0%}. Coverage gate satisfied: {cov['complete']}.",'']
    for b in cov['blockers']: lines += ['- '+b]
    lines += ['','## Assumptions and limitations','',p['scope'],'']
    for item in p['hypothesis_details']['assumptions']+p['limitations']: lines += ['- '+item]
    lines += ['','Approval is a saved research-review decision, not experimental confirmation or proof of literature-wide novelty.','']
    return '\n'.join(lines)


def write_reading_report(out,result):
    from research.publication import markdown
    out=Path(out)
    for folder in ('proposals/approved','proposals/pending','evidence'):
        (out/folder).mkdir(parents=True,exist_ok=True)
    lines=['# Stage 16 — all hypotheses','',
        f"{len(result['hypotheses'])} hypotheses: {result['counts']['approved']} approved, {result['counts']['pending']} pending, {result['counts']['discarded']} discarded.",'',
        'Start here. Each entry links to a standalone proposal and its supporting evidence. Statuses are preserved from saved reviews; no new assessment or retrieval was performed.','']
    entries=[]
    for h in result['hypotheses']:
        p=proposal_record(h);hid=p['hypothesis_id'];status=p['status']
        folder=out/'proposals'/status;folder.mkdir(parents=True,exist_ok=True)
        (folder/f'{hid}.json').write_text(json.dumps(p,indent=2)+'\n')
        (folder/f'{hid}.md').write_text(proposal_markdown(p))
        # Reuse the provenance renderer, retaining exact saved passages and gaps.
        detail=markdown(dict(counts=result['counts'],hypotheses=[h]))
        evidence=detail.split('#### Supporting papers and evidence',1)[1].split('#### Proposed experiments',1)[0]
        (out/'evidence'/f'{hid}.md').write_text(
            f"# Papers and evidence — {hid}\n\n[All hypotheses](../summary.md) · [Proposal](../proposals/{status}/{hid}.md)\n\n"
            f"**Direction:** {p['direction_title']}\n\n**Hypothesis:** {p['hypothesis']}\n\n"
            'Supporting papers motivate the proposal. Prior-work comparisons explain overlap and differences; comparison completion does not mean the hypothesis is novel or proven. Expand evidence sections to read exact saved passages.\n\n'
            '## Supporting papers and evidence'+evidence.replace('#### Novelty and prior-work comparisons','## Novelty and prior-work comparisons'))
        entry=dict(hypothesis_id=hid,problem=p['research_question'],direction_id=p['direction_id'],
                   direction=p['direction_title'],hypothesis=p['hypothesis'],status=status,
                   proposal_md=f'proposals/{status}/{hid}.md',proposal_json=f'proposals/{status}/{hid}.json',evidence_md=f'evidence/{hid}.md')
        entries.append(entry)
        lines += [f"## {hid} — {status.upper()}",'',f"**Problem / research question:** {entry['problem']}",'',
                  f"**Direction:** {entry['direction']} (`{entry['direction_id']}`)",'',
                  f"**Hypothesis:** {entry['hypothesis']}",'',
                  f"[Read proposal]({entry['proposal_md']}) · [Proposal JSON]({entry['proposal_json']}) · [Papers and evidence]({entry['evidence_md']})",'']
    (out/'summary.md').write_text('\n'.join(lines))
    # Preserve any existing recovery counters alongside the reading index.
    path=out/'summary.json'
    summary=json.loads(path.read_text()) if path.exists() else {}
    summary.update(status=result['status'],hypothesis_counts=result['counts'],hypotheses=entries)
    path.write_text(json.dumps(summary,indent=2)+'\n')
    (out/'stage16.md').write_text('# Stage 16\n\nStart with [the hypothesis summary](summary.md). It links to every proposal and evidence file.\n')
