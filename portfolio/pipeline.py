"""Deterministic rendering/selection; no new scientific judgments or model calls."""
from critic.schemas import CriticResult
from critic.refinement_loop import RefinementResult
from novelty.assessment_evidence import build_packet
from opportunities.miner import digest
from portfolio.schemas import FinalCandidate, FinalHypothesis, RankingEntry, SelectionSettings, FinalPortfolio

NOVELTY_PRIORITY = {'LOW_PRIOR_OVERLAP': 0, 'PARTIAL_OVERLAP': 1,
                    'COMPONENTS_KNOWN': 2, 'ALREADY_STUDIED': 3}
OVERLAP_ORDER = {'SAME': 0, 'VERY_CLOSE': 1, 'PARTIAL_OVERLAP': 2, 'ADJACENT': 3, 'DIFFERENT': 4, None: 5}


def current_critic(source):
    if isinstance(source, RefinementResult):
        return next((c.critic for c in reversed(source.cycles) if c.critic), source.original)
    return source


def render_candidate(critic, direction, review):
    assessment = next(c for c in critic.novelty.candidates if c.direction_id == direction.direction_id)
    targets = {t.target_id: t for t in assessment.assessment.targets}
    coverage = {t.target_id: t for t in assessment.coverage}
    critiques = {t.target_id: t for t in review.critique.targets}
    proposal = direction.proposal
    packet = build_packet(critic.novelty.inputs, direction.direction_id)
    # Preserve complete reviewed per-paper comparisons and claims. Do not turn a
    # target-wide meaningful difference into a fabricated paper-specific finding.
    prior = sorted(packet['evidence'], key=lambda e: (
        OVERLAP_ORDER[e['comparison']['classification']], e['paper_id'], e['target_id']))
    return FinalCandidate(candidate_id=direction.direction_id,
        candidate_sha256=digest(direction.model_dump()), title=proposal.title,
        research_direction=proposal.research_direction, proposed_mechanism=proposal.proposed_mechanism,
        scope=proposal.scope, assumptions=proposal.assumptions,
        why_this_direction_exists=proposal.rationale, unresolved_gap=direction.opportunity.candidate.question,
        supporting_evidence=direction.opportunity.evidence,
        evidence_paper_roles=direction.opportunity.paper_assessments, evidence_pages=direction.context_pages,
        possible_hypotheses=[FinalHypothesis(proposal=h, novelty=targets[h.hypothesis_id],
            coverage=coverage[h.hypothesis_id], critique=critiques[h.hypothesis_id]) for h in proposal.hypotheses],
        suggested_initial_experiments=proposal.experiments, closest_prior_work=prior,
        novelty_assessment=targets[direction.direction_id], novelty_coverage=assessment.coverage,
        risks=proposal.risks, uncertainties=proposal.uncertainties,
        what_would_falsify_it=proposal.what_would_falsify_it,
        recommended_next_step=critiques[direction.direction_id].recommended_next_step,
        recommended_next_experiment_id=direction.recommended_next_experiment_id,
        critique=critiques[direction.direction_id])


def ranking_signals(candidate):
    findings = [f for t in [candidate.critique, *[h.critique for h in candidate.possible_hypotheses]]
                for f in t.findings if f.assessment != 'not_applicable']
    total = len(findings)
    # A DOWNRANK at either direction or hypothesis level is retained in selection.
    action = 'DOWNRANK' if any(t.action == 'DOWNRANK' for t in
        [candidate.critique, *[h.critique for h in candidate.possible_hypotheses]]) else 'KEEP'
    concerns = sum(f.assessment == 'concern' for f in findings) / total
    uncertain = sum(f.assessment == 'uncertain' for f in findings) / total
    novelty = NOVELTY_PRIORITY[candidate.novelty_assessment.finding]
    return action, concerns, uncertain, novelty


def project(source, settings):
    critic = current_critic(source)
    routes = {r['direction_id']: r for r in source.handoff()['candidates']}
    reviews = {c.direction_id: c for c in critic.candidates}
    eligible, dispositions = [], []
    for direction in critic.novelty.inputs.generation.directions:
        route = routes[direction.direction_id]
        if route['next_stage'] == 'ranking':
            eligible.append(render_candidate(critic, direction, reviews[direction.direction_id]))
        dispositions.append(dict(candidate_id=direction.direction_id,
            disposition='reserve' if route['next_stage'] == 'ranking' else
                        'discarded' if route['next_stage'] == 'stop' else 'pending', route=route))
    eligible.sort(key=lambda c: (ranking_signals(c)[0] == 'DOWNRANK', *ranking_signals(c)[1:], c.candidate_id))
    selected = eligible[:settings.max_directions]
    selected_ids = {c.candidate_id for c in selected}
    ranking = []
    for rank, candidate in enumerate(eligible, 1):
        action, concerns, uncertain, novelty = ranking_signals(candidate)
        ranking.append(RankingEntry(candidate_id=candidate.candidate_id, rank=rank, action=action,
            concern_fraction=concerns, uncertain_fraction=uncertain, novelty_priority=novelty,
            rationale='Ordered by reviewed KEEP before DOWNRANK, then fewer concern and uncertain findings '
                      '(fractions across applicable direction/hypothesis criteria), then direction novelty '
                      'within the searched corpus; ties use candidate ID.').model_dump())
    for row in dispositions:
        if row['candidate_id'] in selected_ids:
            row['disposition'] = 'selected'
    counts = {key: sum(r['disposition'] == key for r in dispositions)
              for key in ('selected', 'reserve', 'discarded', 'pending')}
    counts.update(generated=len(dispositions), eligible=len(eligible))
    shortfall = max(0, settings.min_directions - len(selected))
    diagnostics = []
    if shortfall:
        diagnostics.append(f'Only {len(selected)} eligible directions selected; desired minimum is {settings.min_directions}. '
                           'Generate and review additional candidates upstream; no pending or rejected candidate was promoted to fill the quota.')
    if counts['pending']:
        diagnostics.append('Pending candidates retain their upstream routes; publication does not resolve their scientific issues.')
    if isinstance(source, RefinementResult) and source.diagnostic:
        diagnostics.append(source.diagnostic)
    status = ('partial' if counts['pending'] or shortfall else 'ready') if selected else ('blocked' if counts['pending'] else 'empty')
    merges = critic.portfolio.assessment.merges if critic.portfolio.assessment else []
    return dict(topic=critic.novelty.inputs.generation.topic, candidates=[c.model_dump() for c in selected],
        ranking=ranking, dispositions=dispositions, pending_merges=[m.model_dump() for m in merges],
        status=status, selection_shortfall=shortfall, diagnostics=diagnostics, counts=counts,
        ranking_policy='reviewed_signals_v1',
        unassessed_ranking_dimensions=['importance', 'technical_depth', 'feasibility', 'potential_impact'],
        literature_wide_novelty='unverified')


def build_portfolio(source, settings=None):
    """Validate the complete saved lineage before publishing any candidate."""
    if not isinstance(source, (CriticResult, RefinementResult)):
        raise TypeError('expected CriticResult or RefinementResult')
    source = type(source).model_validate(source.model_dump())
    settings = SelectionSettings.model_validate((settings or SelectionSettings()).model_dump())
    return FinalPortfolio(source=source, source_sha256=digest(source.model_dump()), settings=settings,
                          **project(source, settings))
