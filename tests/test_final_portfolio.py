"""Final publication, selection and exact source preservation boundaries."""

import pytest
from critic.refinement_loop import RefinementResult
from opportunities.miner import digest
from portfolio import build_portfolio, FinalPortfolio, SelectionSettings
from portfolio.render import markdown
from portfolio.__main__ import parser, run as cli_run
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared, run as search_run
from tests.test_novelty_comparison import ready, run as compare_run
from tests.test_novelty_assessment import bundle, run as assess_run
from novelty.assessment_schemas import AssessmentInputs
from tests.test_research_critic import source, run, Chat


def test_exact_candidate_and_roundtrip(source):
    critic, _ = run(source)
    result = build_portfolio(critic)
    assert FinalPortfolio.model_validate_json(result.model_dump_json()) == result
    assert result.status == 'partial' and result.selection_shortfall == 2
    candidate = result.candidates[0]
    original = critic.novelty.inputs.generation.directions[0]
    assert [h.proposal for h in candidate.possible_hypotheses] == original.proposal.hypotheses
    assert candidate.suggested_initial_experiments == original.proposal.experiments
    assert candidate.supporting_evidence == original.opportunity.evidence
    assert candidate.why_this_direction_exists == original.proposal.rationale
    assert [p.model_dump() for p in candidate.closest_prior_work] == sorted(critic.candidates[0].packet['evidence'],
        key=lambda e: (e['paper_id'], e['target_id']))
    assert candidate.novelty_assessment.meaningful_difference
    assert candidate.title in markdown(result)
    assert build_portfolio(critic, SelectionSettings(min_directions=1)).status == 'ready'


@pytest.mark.parametrize('mode', ['failed', 'refine', 'narrow', 'discard', 'downrank'])
def test_routes_never_fill_quota_with_unapproved_candidates(source, mode):
    def mutate(out, req):
        if 'review_' in req['task']: return
        targets = out['targets']
        if mode == 'downrank': targets[1]['action'] = 'DOWNRANK'
        if mode in ('discard', 'narrow'):
            target = targets[0 if mode == 'discard' else 1]
            target['action'] = 'DISCARD'
            target['findings'][0]['assessment'] = 'concern'
        if mode == 'refine':
            targets[1]['action'] = 'REFINE'
            out['revisions'] = [dict(target_id=targets[1]['target_id'], field_path='/hypotheses/0/mechanism',
                required_change='Clarify confound.', reason='Mechanism is ambiguous.')]
    critic, _ = run(source, Chat(mutate, fail=mode == 'failed'))
    result = build_portfolio(critic)
    assert result.dispositions[0].disposition == ('selected' if mode == 'downrank' else
                                                 'discarded' if mode == 'discard' else 'pending')
    assert len(result.candidates) == (1 if mode == 'downrank' else 0)
    if mode == 'downrank': assert result.ranking[0].action == 'DOWNRANK'


@pytest.mark.parametrize('field', ['title', 'novelty', 'experiment', 'rank', 'disposition', 'hash', 'status'])
def test_saved_result_cannot_rewrite_reviewed_science(source, field):
    critic, _ = run(source)
    data = build_portfolio(critic).model_dump()
    if field == 'title': data['candidates'][0]['title'] = 'Invented title'
    if field == 'novelty': data['candidates'][0]['novelty_assessment']['finding'] = 'LOW_PRIOR_OVERLAP'
    if field == 'experiment': data['candidates'][0]['suggested_initial_experiments'][0]['objective'] = 'Different test'
    if field == 'rank': data['ranking'][0]['rank'] = 2
    if field == 'disposition': data['dispositions'][0]['disposition'] = 'discarded'
    if field == 'hash': data['source_sha256'] = 'stale'
    if field == 'status': data['status'] = 'ready'
    with pytest.raises(ValueError): FinalPortfolio.model_validate(data)


@pytest.fixture
def multiple(prepared):
    root, generation = prepared
    template = generation.directions[0]
    generation.directions = [template.model_copy(update={'direction_id': f'D{i}'}, deep=True) for i in range(7)]
    search, _, _ = search_run((root, generation), max_calls=100)
    (root/'outside-paper'/'01.md').write_text('We evaluate caching under stable workloads. Compression is not evaluated in these experiments.')
    (root/'outside-paper'/'02.md').write_text('Future work could study compression-aware routing.')
    comparison, _ = compare_run((root, generation, search), max_calls=100)
    assessment, _ = assess_run(AssessmentInputs(generation=generation, search=search, comparisons=comparison), max_calls=100)
    return root, assessment


def test_multicandidate_ranking_reserves_and_deterministic_ties(multiple):
    def mutate(out, req):
        if 'review_' in req['task'] or 'portfolio' in req['task']: return
        did = req['packet']['direction_id']
        if did == 'D0': out['targets'][0]['action'] = 'DOWNRANK'
        if did == 'D1': out['targets'][0]['findings'][0]['assessment'] = 'concern'
        if did == 'D2': out['targets'][0]['findings'][0]['assessment'] = 'uncertain'
    critic, _ = run(multiple, Chat(mutate), max_calls=100)
    result = build_portfolio(critic)
    assert result.status == 'ready'
    assert [r.candidate_id for r in result.ranking] == ['D3','D4','D5','D6','D2','D1','D0']
    assert len(result.candidates) == 5 and result.counts['reserve'] == 2
    assert result.counts['generated'] == 7
    assert FinalPortfolio.model_validate_json(result.model_dump_json()) == result


def test_merge_requires_revalidation_not_cosmetic_combination(multiple):
    def mutate(out, req):
        if 'portfolio' not in req['task'] or 'review_' in req['task']: return
        packet = req['packet']
        members = packet['candidates'][:2]
        src = next(iter(next(iter(packet['source_passages'].values()))))
        out['merges'] = [dict(action='MERGE', members=[dict(direction_id=c['direction_id'],
            hypothesis_ids=[h['hypothesis_id'] for h in c['candidate']['hypotheses']]) for c in members],
            combined_question='A shared interaction question.', rationale='Complementary predictions.',
            preserved_distinctions='Retain both prediction sets.', passage_ids=[src])]
    critic, _ = run(multiple, Chat(mutate), max_calls=100)
    result = build_portfolio(critic)
    assert len(result.pending_merges) == 1
    assert result.counts['pending'] == 2 and result.counts['selected'] == 5
    assert result.status == 'partial'
    assert all(c.candidate_id not in {'D0','D1'} for c in result.candidates)


def test_pending_portfolio_review_blocks_selection(multiple):
    def mutate(out, req):
        if req['task'] == 'review_critic_portfolio': out['decision'] = 'abstain'
    critic, _ = run(multiple, Chat(mutate), max_calls=100)
    result = build_portfolio(critic)
    assert not result.candidates and result.counts['pending'] == 7


def test_refinement_source_and_cli(source, tmp_path):
    critic, _ = run(source)
    refinement = RefinementResult(original=critic, original_sha256=digest(critic.model_dump()),
        observations={}, cycles=[], status='ready', diagnostic=None, calls={}, usage={}, run={})
    result = build_portfolio(refinement, SelectionSettings(min_directions=1))
    assert result.status == 'ready'
    assert FinalPortfolio.model_validate_json(result.model_dump_json()) == result
    input_path = tmp_path/'refinement.json'
    input_path.write_text(refinement.model_dump_json())
    out, md = tmp_path/'final.json', tmp_path/'final.md'
    args = parser().parse_args(['--refinement', str(input_path), '--out', str(out), '--markdown', str(md), '--min-directions', '1'])
    assert cli_run(args)
    assert FinalPortfolio.model_validate_json(out.read_text()) == result
    assert md.read_text() == markdown(result)
    with pytest.raises(ValueError, match='already exists'): cli_run(args)


def test_invalid_selection_bounds():
    with pytest.raises(ValueError): SelectionSettings(min_directions=5, max_directions=3)


def test_mixed_portfolio_accounting_and_sample_export(multiple, tmp_path):
    def mutate(out, req):
        if 'portfolio' in req['task'] or 'review_' in req['task']:
            return
        did = req['packet']['direction_id']
        if did == 'D0':
            out['targets'][0]['action'] = 'DISCARD'
            out['targets'][0]['findings'][0]['assessment'] = 'concern'
        elif did == 'D1':
            target = out['targets'][1]
            target['action'] = 'REFINE'
            out['revisions'] = [dict(target_id=target['target_id'], field_path='/hypotheses/0/mechanism',
                required_change='Clarify the causal mechanism.', reason='A confound remains.')]
        elif did == 'D2':
            out['targets'][0]['action'] = 'DOWNRANK'
    critic, _ = run(multiple, Chat(mutate), max_calls=100)
    before = critic.model_dump_json()
    result = build_portfolio(critic, SelectionSettings(min_directions=3, max_directions=3))
    assert result.status == 'partial' and result.selection_shortfall == 0
    assert result.counts == dict(selected=3, reserve=2, discarded=1, pending=1, generated=7, eligible=5)
    assert [c.candidate_id for c in result.candidates] == ['D3', 'D4', 'D5']
    assert result.ranking[-1].candidate_id == 'D2'
    assert critic.model_dump_json() == before
    assert sum(result.counts[k] for k in ('selected','reserve','discarded','pending')) == 7
    text = markdown(result)
    for candidate in result.candidates:
        assert candidate.what_would_falsify_it in text
        for hypothesis in candidate.possible_hypotheses:
            assert hypothesis.proposal.falsification_criterion in text
        for prior in candidate.closest_prior_work:
            for claim in prior.claims:
                assert claim.text in text
                assert all(p in text for p in claim.passage_ids)
    (tmp_path/'synthetic_portfolio.json').write_text(result.model_dump_json(indent=2))
    (tmp_path/'synthetic_portfolio.md').write_text(text)


@pytest.mark.parametrize('maximum', [1, 3, 10])
def test_selection_limit_does_not_change_ranking(multiple, maximum):
    critic, _ = run(multiple, max_calls=100)
    result = build_portfolio(critic, SelectionSettings(min_directions=1, max_directions=maximum))
    assert result.status == 'ready'
    assert len(result.candidates) == min(maximum, 7)
    assert result.counts['reserve'] == max(0, 7 - maximum)
    assert [r.candidate_id for r in result.ranking] == [f'D{i}' for i in range(7)]


@pytest.mark.parametrize('mode', [None, 'reject'])
def test_completed_and_failed_refinement_cycles(source, mode):
    from tests.test_refinement_loop import loop
    refinement, _ = loop(source, mode)
    result = build_portfolio(refinement, SelectionSettings(min_directions=1))
    assert FinalPortfolio.model_validate_json(result.model_dump_json()) == result
    if mode == 'reject':
        assert result.status == 'blocked' and not result.candidates
        assert result.dispositions[0].disposition == 'pending'
    else:
        assert result.status == 'ready' and len(result.candidates) == 1
        final = refinement.cycles[-1].critic.novelty.inputs.generation.directions[0]
        assert result.candidates[0].suggested_initial_experiments == final.proposal.experiments
        assert final.proposal != refinement.original.novelty.inputs.generation.directions[0].proposal


def test_cli_rejects_colliding_outputs_before_writing(source, tmp_path):
    critic, _ = run(source)
    path = tmp_path/'critic.json'
    path.write_text(critic.model_dump_json())
    out = tmp_path/'same.json'
    args = parser().parse_args(['--critic', str(path), '--out', str(out), '--markdown', str(out)])
    with pytest.raises(ValueError, match='must differ'):
        cli_run(args)
    assert not out.exists()
