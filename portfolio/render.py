"""Human-readable view of the same reviewed portfolio; no generated prose."""
from portfolio.schemas import FinalPortfolio
from portfolio.pipeline import current_critic
from portfolio.references import bibliography


def escape(text):
    text = str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    for character in ('\\', '*', '_', '[', ']', '`'):
        text = text.replace(character, '\\' + character)
    return text


def reference_label(reference, fallback):
    if reference is None or reference.title is None:
        return 'Title unavailable (' + escape(fallback) + ')'
    title = escape(reference.title)
    if reference.url:
        url = reference.url.replace('<', '%3C').replace('>', '%3E')
        return f'[{title}](<{url}>)'
    return title



def markdown(result):
    result = FinalPortfolio.model_validate(result.model_dump())
    references = result.references if result.schema_version == 'final_portfolio_v2' else bibliography(current_critic(result.source), [])
    refs = {r.paper_id: r for r in references}
    def label(pid):
        return reference_label(refs.get(pid), pid)

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
        evidence_by_id = {e.evidence_id: e for e in candidate.supporting_evidence}
        def evidence_labels(ids):
            pids = dict.fromkeys(evidence_by_id[eid].paper_id for eid in ids if eid in evidence_by_id)
            return '; '.join(label(pid) for pid in pids)
        bullets('Why this direction exists', [f'{r.statement} '
            f'(Sources: {evidence_labels(r.evidence_ids)}; pages: {", ".join(r.page_ids)})'
            for r in candidate.why_this_direction_exists])
        bullets('Assumptions', candidate.assumptions)
        roles = {p.paper_id: p.role for p in candidate.evidence_paper_roles}
        bullets('Supporting and contextual evidence', [
            f'{label(e.paper_id)} ({roles.get(e.paper_id, "unclassified")}), {e.evidence_id}: '
            f'{e.summary} — pages: {", ".join(str(p.page) for p in candidate.evidence_pages if p.paper_id == e.paper_id) or "unavailable"}; source path: `{e.value_path}`' for e in candidate.supporting_evidence])
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
                f'**Evidence:** {evidence_labels(p.evidence_ids)}', '',
                '**Novelty comparison sources:** ' + '; '.join(label(e.paper_id) for e in h.novelty.evidence), ''])
        lines.extend(['### Suggested experiments', ''])
        for e in candidate.suggested_initial_experiments:
            lines.extend([f'#### {e.experiment_id} ({e.stage}; hypotheses: {", ".join(e.hypothesis_ids)})', '',
                e.objective, '', f'**Comparison:** {e.comparison}', '',
                f'**Observe:** {"; ".join(e.observations)}', '', e.why_this_test, '', e.informative_outcomes, ''])
        lines.extend(['### Closest prior work', ''])
        for prior in candidate.closest_prior_work:
            lines.extend([f'{label(prior.paper_id)} — **{prior.target_id}: {prior.comparison.classification or "UNRESOLVED"}**', '',
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
    critic = current_critic(result.source)
    selected = {c.candidate_id for c in result.candidates}
    assessments = {c.direction_id:c for c in critic.novelty.candidates}
    critiques = {c.direction_id:c for c in critic.candidates}
    for direction in critic.novelty.inputs.generation.directions:
        if direction.direction_id in selected:
            continue
        assessment = assessments[direction.direction_id]
        lines.extend([f'## Unselected proposal: {direction.proposal.title}', '',
                      'Not approved for the selected direction portfolio.', '',
                      f'**Direction blocker:** {critiques[direction.direction_id].diagnostic or "See reviewed disposition."}', ''])
        outcomes = {o['target_id']:o for o in assessment.outcomes()}
        for h in direction.proposal.hypotheses:
            outcome = outcomes.get(h.hypothesis_id, {})
            coverage = next((c for c in assessment.coverage if c.target_id == h.hypothesis_id), None)
            lines.extend([f'### {h.hypothesis_id}', '',
                f'**Condition:** {h.condition}', '', f'**Intervention:** {h.intervention}', '',
                f'**Expected effect:** {h.expected_effect}', '', f'**Mechanism:** {h.mechanism}', '',
                f'**Falsification:** {h.falsification_criterion}', '',
                f'**Novelty finding:** {outcome.get("finding", "UNRESOLVED")}', '',
                f'**Evidence status:** {outcome.get("novelty_status", "unresolved")}', '',
                '**Blockers:** ' + ('; '.join(coverage.blockers) if coverage and coverage.blockers else assessment.diagnostic or 'No target coverage blocker; parent direction or scientific review may remain pending.'), '',
                '**Motivating papers:** ' + '; '.join(label(e.paper_id) for e in direction.opportunity.evidence if e.evidence_id in h.evidence_ids), ''])
        lines.extend(['### Proposed experiments', ''])
        for e in direction.proposal.experiments:
            lines.extend([f'- {e.experiment_id} ({", ".join(e.hypothesis_ids)}): {e.objective}; comparison: {e.comparison}', ''])
    lines.extend(['## Paper references', '',
        'References cover motivating evidence and prior-work comparisons, including pending candidates. '
        'A reference here does not imply that its candidate passed review.', ''])
    for reference in references:
        lines.extend([f'- {label(reference.paper_id)}',
            '  - Authors: ' + ('; '.join(escape(a) for a in reference.authors) or 'unavailable'),
            f'  - Year: {reference.year or "unavailable"}; venue: {escape(reference.venue or "unavailable")}',
            f'  - Role: {", ".join(reference.roles)}; candidates: {", ".join(reference.candidate_ids)}',
            f'  - Cited/loaded pages: {", ".join(map(str, reference.pages)) or "unavailable"}',
            f'  - Paper ID: `{escape(reference.paper_id)}`'])
        if reference.pdf_url:
            url = reference.pdf_url.replace('<', '%3C').replace('>', '%3E')
            lines.append(f'  - [PDF](<{url}>)')
        if reference.url is None:
            lines.append('  - Public paper link unavailable in saved metadata.')
        if reference.conflicts:
            lines.append('  - Metadata sources disagree on: ' + ', '.join(reference.conflicts) + '; alternatives retained in JSON.')
    lines.extend(['', f'Source SHA-256: `{result.source_sha256}`', ''])
    return '\n'.join(lines)
