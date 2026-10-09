"""Human-readable view of the same reviewed portfolio; no generated prose."""
from portfolio.schemas import FinalPortfolio


def markdown(result):
    result = FinalPortfolio.model_validate(result.model_dump())
    lines = [f'# Research portfolio: {result.topic}', '',
             f'Status: **{result.status}**. Selected: {result.counts["selected"]}; '
             f'pending: {result.counts["pending"]}; reserve: {result.counts["reserve"]}.', '',
             'Novelty is relative to the searched corpus. Proposals are model reviewed, not empirically validated.', '']
    lines.extend(f'- {d}' for d in result.diagnostics)
    lines.extend(['', 'Ranking uses reviewed actions, concern/uncertainty fractions, then corpus novelty; '
                  'candidate IDs break ties. Importance, technical depth, feasibility and impact are not scored.', ''])

    def bullets(label, items):
        lines.extend([f'### {label}', ''])
        lines.extend(f'- {item}' for item in items)
        lines.append('')

    for candidate in result.candidates:
        lines.extend([f'## {candidate.candidate_id}: {candidate.title}', '', candidate.research_direction, '',
                      f'**Unresolved gap:** {candidate.unresolved_gap}', '',
                      f'**Mechanism:** {candidate.proposed_mechanism}', '', f'**Scope:** {candidate.scope}', ''])
        bullets('Why this direction exists', [f'{r.statement} '
            f'(Evidence: {", ".join(r.evidence_ids)}; pages: {", ".join(r.page_ids)})'
            for r in candidate.why_this_direction_exists])
        bullets('Assumptions', candidate.assumptions)
        roles = {p.paper_id: p.role for p in candidate.evidence_paper_roles}
        bullets('Supporting and contextual evidence', [
            f'**{e.paper_id}** ({roles.get(e.paper_id, "unclassified")}), {e.evidence_id}: '
            f'{e.summary} — source path: `{e.value_path}`' for e in candidate.supporting_evidence])
        lines.extend(['### Hypotheses', ''])
        for h in candidate.possible_hypotheses:
            p = h.proposal
            lines.extend([f'#### {p.hypothesis_id} — {h.novelty.finding}', '',
                f'**Condition:** {p.condition}', '', f'**Intervention:** {p.intervention}', '',
                f'**Expected effect:** {p.expected_effect}', '', f'**Mechanism:** {p.mechanism}', '',
                f'**Assumptions:** {"; ".join(p.assumptions)}', '',
                f'**Falsification:** {p.falsification_criterion}', '',
                f'**Novelty:** {h.novelty.reasoning}', '',
                f'**Meaningful difference:** {h.novelty.meaningful_difference or "Unresolved"}', '',
                f'**Learning if negative:** {h.critique.learning_if_negative}', '',
                f'**Evidence:** {", ".join(p.evidence_ids)}', ''])
        lines.extend(['### Suggested experiments', ''])
        for e in candidate.suggested_initial_experiments:
            lines.extend([f'#### {e.experiment_id} ({e.stage}; hypotheses: {", ".join(e.hypothesis_ids)})', '',
                e.objective, '', f'**Comparison:** {e.comparison}', '',
                f'**Observe:** {"; ".join(e.observations)}', '', e.why_this_test, '', e.informative_outcomes, ''])
        lines.extend(['### Closest prior work', ''])
        for prior in candidate.closest_prior_work:
            lines.extend([f'**{prior.paper_id} / {prior.target_id}: {prior.comparison.classification or "UNRESOLVED"}**', '',
                prior.comparison.rationale, '', f'Relationship: {prior.relationship.explanation}', ''])
            for dimension in prior.comparison.dimensions:
                if dimension.relation in ('PARTIAL', 'DIFFERENT', 'UNKNOWN'):
                    lines.extend([f'- {dimension.dimension} ({dimension.relation}): {dimension.rationale} '
                                  f'(claims: {", ".join(dimension.claim_ids) or "none"})'])
            lines.append('')
            for claim in prior.claims:
                lines.extend([f'- {claim.claim_id}: {claim.text} (passages: {", ".join(claim.passage_ids)})'])
            lines.append('')
        lines.extend(['### Novelty assessment', '', candidate.novelty_assessment.finding, '',
                      candidate.novelty_assessment.reasoning, '',
                      candidate.novelty_assessment.meaningful_difference or 'Meaningful difference unresolved.', '',
                      'Coverage: ' + '; '.join(f'{c.target_id}: {"complete" if c.complete else "incomplete"}'
                                              for c in candidate.novelty_coverage), ''])
        bullets('Risks', candidate.risks)
        bullets('Uncertainties', candidate.uncertainties)
        bullets('Critic concerns and uncertainties', [f'{t.target_id} / {f.criterion}: {f.reasoning}'
            for t in [candidate.critique, *[h.critique for h in candidate.possible_hypotheses]]
            for f in t.findings if f.assessment in ('concern', 'uncertain')])
        lines.extend(['### Falsification and next step', '', candidate.what_would_falsify_it, '',
                      candidate.recommended_next_step, '',
                      f'Suggested experiment: {candidate.recommended_next_experiment_id}', ''])
    bullets('Ranking of all eligible candidates', [
        f'{r.rank}. {r.candidate_id}: {r.action}; concern fraction {r.concern_fraction:.3f}; '
        f'uncertain fraction {r.uncertain_fraction:.3f}; novelty priority {r.novelty_priority} '
        '(lower is preferred).' for r in result.ranking])
    bullets('Candidate dispositions and pending work', [
        f'{r.candidate_id}: **{r.disposition}**; next stage: {r.route["next_stage"]}. '
        f'{r.route.get("diagnostic") or r.route.get("refinement_diagnostic") or ""}'
        for r in result.dispositions])
    for plan in result.pending_merges:
        lines.extend(['### Pending merge', '', plan.combined_question, '', plan.rationale, '',
                      'Members: ' + ', '.join(m.direction_id for m in plan.members), '', plan.preserved_distinctions, ''])
    lines.extend([f'Source SHA-256: `{result.source_sha256}`', ''])
    return '\n'.join(lines)
