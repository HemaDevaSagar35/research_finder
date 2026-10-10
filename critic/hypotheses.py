"""Independently review evidence-ready hypotheses without approving their direction."""
from critic.evidence import packet_for
from critic.pipeline import ResearchCritic
from llm_client.progress import gather
from opportunities.miner import digest


def eligibility(candidate, direction, hid):
    independent = next((h for h in candidate.hypothesis_assessments or [] if h.coverage[0].target_id == hid), None)
    if candidate.assessment is None:
        if independent is None or independent.assessment is None:
            return (independent.diagnostic if independent else candidate.diagnostic) or 'No independently accepted novelty assessment.'
        candidate = independent
    outcome = next(o for o in candidate.outcomes() if o['target_id'] == hid)
    if not outcome['coverage_complete'] or outcome['novelty_status'] == 'unresolved':
        coverage = next(c for c in candidate.coverage if c.target_id == hid)
        return 'Incomplete novelty evidence: ' + '; '.join(coverage.blockers)
    handoff = candidate.refinement_handoff(direction)
    if hid in handoff['requires_novelty_recheck_target_ids']:
        return 'Proposed scientific changes require a fresh novelty check.'
    if hid in candidate.assessment.refinement.removed_hypothesis_ids:
        return 'Novelty assessment requests removal; not eligible for independent approval.'
    return None


async def review_hypotheses(novelty, store, *, existing=None, critic_factory=ResearchCritic, **kwargs):
    critic = critic_factory(store, **kwargs)
    async def direction_work(direction):
        assessment = next(c for c in novelty.candidates if c.direction_id == direction.direction_id)
        support = None
        rows = []
        for hypothesis in direction.proposal.hypotheses:
            hid = hypothesis.hypothesis_id
            reason = eligibility(assessment, direction, hid)
            row = dict(direction_id=direction.direction_id, hypothesis_id=hid,
                       proposal=hypothesis.model_dump(), status='pending', diagnostic=reason,
                       critique=None, reviews=[], packet=None)
            published = next((c for c in existing.candidates if c.direction_id == direction.direction_id and c.critique is not None),None) if existing else None
            if reason is None and published is not None:
                target = next(t for t in published.critique.targets if t.target_id == hid)
                row.update(critique=published.critique.model_dump(),reviews=[r.model_dump() for r in published.reviews],
                           packet=published.packet,status='approved' if target.action in ('KEEP','DOWNRANK') else 'discarded' if target.action=='DISCARD' else 'pending',
                           diagnostic=None if target.action in ('KEEP','DOWNRANK','DISCARD') else 'Critic requests '+target.action)
                rows.append(row)
                continue
            if reason is None:
                try:
                    if support is None:
                        raw, _ = await critic.io._context(direction, {})
                        support = {k:raw[k] for k in ('paper_artifacts','pages')}
                    packet = packet_for(novelty, direction, support, hypothesis_id=hid)
                    packet['review_target_ids'] = [hid]
                    packet['review_scope'] = ('Review only the named hypothesis. The full original direction is context; '
                        'do not approve the direction or other hypotheses. Unrelated unresolved targets are not vetoes, '
                        'but shared source defects and experiment dependencies still require upstream requests.')
                    draft,reviews,repairs,diagnostic = await critic._reviewed(packet)
                    row.update(packet=packet,critique=draft.model_dump() if draft else None,
                               reviews=[r.model_dump() for r in reviews],diagnostic=diagnostic)
                    if draft:
                        action = draft.targets[0].action
                        row['status'] = 'approved' if action in ('KEEP','DOWNRANK') else 'discarded' if action == 'DISCARD' else 'pending'
                        if row['status']=='pending': row['diagnostic']='Critic requests '+action
                except Exception as exc:
                    row['diagnostic']=f'{type(exc).__name__}: {exc}'
            rows.append(row)
        return rows
    try:
        groups=await gather(*(direction_work(d) for d in novelty.inputs.generation.directions), label='hypothesis_reviews')
    finally:
        if critic.io.client: await critic.io.client.raw.close()
    return dict(schema_version='independent_hypothesis_reviews_v1',
                assessment_sha256=digest(novelty.model_dump()), hypotheses=[h for group in groups for h in group])


def markdown(result):
    lines=['## Independent hypothesis reviews','',
           'These decisions apply to individual unchanged hypotheses, not approval of their parent directions.','']
    for h in result['hypotheses']:
        lines.extend([f"### {h['hypothesis_id']}: {h['status']}",'',h['diagnostic'] or 'Passed independent scientific review.',''])
    return '\n'.join(lines)
