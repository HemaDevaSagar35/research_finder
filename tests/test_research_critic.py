"""Research Critic boundaries: evidence, actions, revisions, reviews and async routing."""
import asyncio
from copy import deepcopy
import json

import pytest
from critic.pipeline import ResearchCritic, Settings, transport, preserve_unaffected
from critic.schemas import CriticResult, CritiqueDraft, ReviewReport, PortfolioDraft, CRITERIA
from critic.evidence import validate_draft, validate_portfolio, portfolio_packet
from llm_client import ChatResult
from opportunities.miner import digest
from reasoning.evidence import PaperStore
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready, run as compare_run
from tests.test_novelty_assessment import bundle, run as assess_run


@pytest.fixture
def source(ready, bundle):
    novelty, _ = assess_run(bundle)
    return ready[0], novelty


def draft_for(packet):
    return dict(targets=[dict(target_id=t, action='KEEP', rationale='A testable interaction question remains.',
        findings=[dict(criterion=k, assessment='adequate', basis='proposal_analysis',
            reasoning='The proposed comparison can distinguish the interaction prediction.',
            passage_ids=[], proposal_paths=['/research_direction']) for k in CRITERIA],
        learning_if_negative='A resolved absence of interaction would limit the proposed mechanism.',
        recommended_next_step='Examine the suggested high-level comparison.')
        for t in [packet['direction_id'], *[h['hypothesis_id'] for h in packet['candidate']['hypotheses']]]],
        revisions=[], upstream_requests=[])


def review_for(packet, portfolio=False):
    ids = packet['eligible_direction_ids'] if portfolio else [packet['direction_id'], *[h['hypothesis_id'] for h in packet['candidate']['hypotheses']]]
    return dict(decision='pass', summary='Evidence and scientific reasoning checked.', issues=[],
        target_checks=[dict(target_id=t, reasoning='No unsupported judgment found.', defects=[]) for t in ids],
        upstream_requests=[], test_link_checks=[] if portfolio else [dict(hypothesis_id=h,
            experiment_id=e['experiment_id'], prediction='Fixture positive interaction.', equality_case='Zero contradicts a positive prediction.',
            decision='consistent', reasoning='Fixture outcomes match the prediction.')
            for e in packet['candidate']['experiments'] for h in e['hypothesis_ids']])


class Chat:
    def __init__(self, mutate=None, fail=False):
        self.calls, self.mutate, self.fail = [], mutate, fail
        self.active = self.peak = 0
    async def __call__(self, **kw):
        req = json.loads(kw['messages'][1]['content'])
        self.calls.append((req, kw))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.005)
        self.active -= 1
        if self.fail:
            raise RuntimeError('injected failure')
        packet = req['packet']
        if 'review_' in req['task']:
            out = review_for(packet, 'portfolio' in req['task'])
        elif 'portfolio' in req['task']:
            out = dict(considered_direction_ids=packet['eligible_direction_ids'], merges=[], rationale='Distinct scientific questions.')
        else:
            out = deepcopy(req.get('previous_draft') or draft_for(packet))
        if self.mutate:
            self.mutate(out, req)
        return ChatResult(json.dumps(out), 'stop', 'fixture')


def run(source, chat=None, review_chat=None, **settings):
    root, novelty = source
    chat = chat or Chat()
    result = asyncio.run(ResearchCritic(PaperStore(root), chat, review_chat=review_chat,
        settings=Settings(**settings)).run(novelty))
    return result, chat


def test_complete_roundtrip_and_original_support(source):
    result, chat = run(source)
    saved = CriticResult.model_validate_json(result.model_dump_json())
    c = saved.candidates[0]
    assert c.critique and len(chat.calls) == 2
    assert c.packet['support_context']['paper_artifacts']
    assert c.packet['opportunity'] == source[1].inputs.generation.directions[0].opportunity.model_dump()
    assert any(p['passage_id'].startswith('support:') for p in c.packet['source_passages'])
    assert len(c.critique.targets) == 3
    assert c.handoff()['next_stage'] == 'ranking'
    assert saved.handoff()['portfolio_complete']
    assert source[1].scientific_quality == 'not_assessed'


def test_fresh_independent_reviewer_and_routing(source):
    author, reviewer = Chat(), Chat()
    result, _ = run(source, author, reviewer)
    assert result.candidates[0].critique
    assert len(author.calls) == len(reviewer.calls) == 1
    req = reviewer.calls[0][0]
    assert 'previous_draft' not in req and 'corrections' not in req
    assert 'draft' in req and req['packet'] == author.calls[0][0]['packet']


@pytest.mark.parametrize('mode', ['provider', 'budget', 'context', 'source'])
def test_processing_failures_are_never_discard(source, mode):
    chat, settings = Chat(fail=mode == 'provider'), {}
    if mode == 'budget': settings['max_calls'] = 0
    if mode == 'context': settings['max_input_tokens'] = 1
    if mode == 'source':
        d = source[1].inputs.generation.directions[0]
        p = d.context_pages[0]
        path = source[0]/p.paper_id/'01.md'
        path.write_text('Changed source')
    result, _ = run(source, chat, **settings)
    c = result.candidates[0]
    assert c.critique is None and c.diagnostic
    assert c.handoff()['scientific_action'] is None
    assert c.handoff()['next_stage'] == 'retry_critic'


def test_abstention_not_discard(source):
    def mutate(out, req):
        if 'review_' in req['task']:
            out['decision'] = 'abstain'
    result, _ = run(source, Chat(mutate))
    assert result.candidates[0].critique is None
    assert result.candidates[0].reviews[-1].report.decision == 'abstain'


@pytest.mark.parametrize('change', ['omit_target', 'duplicate_criterion', 'unknown_source', 'false_source_fact', 'bad_pointer', 'empty_refine', 'invalid_discard'])
def test_invalid_scientific_contract_withheld(source, change):
    def mutate(out, req):
        if 'review_' in req['task']: return
        t = out['targets'][0]
        if change == 'omit_target': out['targets'].pop()
        if change == 'duplicate_criterion': t['findings'][0]['criterion'] = t['findings'][1]['criterion']
        if change == 'unknown_source': t['findings'][0]['passage_ids'] = ['nonexistent']
        if change == 'false_source_fact': t['findings'][0]['basis'] = 'source_fact'
        if change == 'bad_pointer': t['findings'][0]['proposal_paths'] = ['/missing']
        if change == 'empty_refine': t['action'] = 'REFINE'
        if change == 'invalid_discard': t['action'] = 'DISCARD'
    result, chat = run(source, Chat(mutate))
    assert result.candidates[0].critique is None
    assert len(chat.calls) == 2
    assert 'invalid_research_critique' in result.candidates[0].diagnostic


def test_revisions_recheck_hypothesis_and_direction(source):
    def mutate(out, req):
        if 'review_' in req['task']: return
        out['targets'][1]['action'] = 'REFINE'
        out['revisions'] = [dict(target_id=out['targets'][1]['target_id'], field_path='/hypotheses/0/mechanism',
            required_change='Distinguish the proposed mechanism from a shared confound.', reason='Clarify alternative explanation.')]
    result, _ = run(source, Chat(mutate))
    c = result.candidates[0]
    route = c.handoff()
    assert route['next_stage'] == 'direction_revision_then_correctness_and_novelty'
    assert set(route['requires_novelty_recheck_target_ids']) == {c.direction_id, c.critique.targets[1].target_id}
    assert route['proposal_applied'] is False
    assert result.portfolio.packet['eligible_direction_ids'] == []


def test_discard_one_hypothesis_preserves_survivor(source):
    def mutate(out, req):
        if 'review_' in req['task']: return
        out['targets'][1]['action'] = 'DISCARD'
        out['targets'][1]['findings'][0]['assessment'] = 'concern'
    result, _ = run(source, Chat(mutate))
    c = result.candidates[0]
    assert c.handoff()['next_stage'] == 'narrow_direction_and_recheck_scope'
    assert c.handoff()['retained_hypothesis_ids'] == [c.critique.targets[2].target_id]
    assert c.handoff()['requires_novelty_recheck_target_ids'] == [c.direction_id]
    links = c.handoff()['retained_experiment_links']
    assert links and all(c.critique.targets[1].target_id not in ids for ids in links.values())


def test_upstream_source_defect_blocks_publication(source):
    def mutate(out, req):
        if 'review_' in req['task']:
            out['decision'] = 'revise'
            src = next(iter(req['packet']['source_passages'].values()))[0]['passage_id']
            out['upstream_requests'] = [dict(stage='novelty_comparison', target_id=req['packet']['direction_id'],
                passage_ids=[src], reason='Scoped result does not establish that premise.')]
    result, chat = run(source, Chat(mutate))
    c = result.candidates[0]
    assert c.critique is None and len(chat.calls) == 2
    assert c.handoff()['next_stage'] == 'resolve_upstream'
    assert c.handoff()['upstream_requests']


def test_revision_preserves_unaffected_and_fresh_review(source):
    def mutate(out, req):
        if req['task'] == 'review_research_critique' and req['draft']['targets'][0]['rationale'] != 'Corrected scientific distinction.':
            out['decision'] = 'revise'
            out['issues'] = [dict(field_path='/targets/0/rationale', explanation='Clarify distinction.', required_change='Explain the interaction.', passage_ids=[])]
        if req['task'] == 'revise_research_critique':
            out['targets'][0]['rationale'] = 'Corrected scientific distinction.'
    result, chat = run(source, Chat(mutate))
    c = result.candidates[0]
    assert c.critique and len(chat.calls) == 4
    assert c.reviews[0].draft.targets[1:] == c.reviews[1].draft.targets[1:]
    assert all('corrections' not in r for r, _ in chat.calls if r['task'].startswith('review_'))


def test_unaffected_revision_rejected(source):
    result, _ = run(source)
    c = result.candidates[0]
    previous = c.critique
    updated = previous.model_copy(deep=True)
    updated.targets[1].rationale = 'Unrequested change'
    review = ReviewReport.model_validate(review_for(c.packet))
    review.target_checks[0].defects = ['Only direction issue']
    with pytest.raises(ValueError, match='unaffected'):
        preserve_unaffected(updated, previous, review)


@pytest.mark.parametrize('field', ['novelty', 'proposal', 'page', 'coverage', 'verdict', 'review', 'portfolio'])
def test_saved_output_tampering_rejected(source, field):
    result, _ = run(source)
    data = result.model_dump()
    c = data['candidates'][0]
    if field == 'novelty': data['novelty_sha256'] = 'stale'
    if field == 'proposal': c['packet']['candidate']['scope'] = 'Changed'
    if field == 'page': c['packet']['support_context']['pages'][0]['text'] += ' Invented'
    if field == 'coverage': c['packet']['coverage'][0]['complete'] = False
    if field == 'verdict': c['critique']['targets'][0]['rationale'] = 'Not reviewed'
    if field == 'review': c['reviews'] = []
    if field == 'portfolio': data['portfolio']['assessment']['considered_direction_ids'] = ['unknown']
    with pytest.raises(ValueError): CriticResult.model_validate(data)


def test_lossless_transport(source):
    result, _ = run(source)
    packet = result.candidates[0].packet
    original = deepcopy(packet)
    packed = transport(packet)
    assert packet == original
    all_text = [p['text'] for entries in packed['source_passages'].values() for p in entries]
    assert sorted(all_text) == sorted(p['text'] for p in packet['source_passages'])
    for e in packet['evidence']:
        assert all(packed['accepted_claims_by_paper'][e['paper_id']][c['claim_id']]['text'] == c['text'] for c in e['claims'])


def merge_fixture(source):
    result, _ = run(source)
    a = result.candidates[0]
    b = a.model_copy(deep=True)
    b.direction_id = 'another-direction'
    b.critique.targets[0].target_id = b.direction_id
    b.packet['direction_id'] = b.direction_id
    packet = portfolio_packet([a, b])
    draft = PortfolioDraft.model_validate(dict(considered_direction_ids=packet['eligible_direction_ids'],
        merges=[dict(action='MERGE', members=[dict(direction_id=c['direction_id'],
            hypothesis_ids=[h['hypothesis_id'] for h in c['candidate']['hypotheses']]) for c in packet['candidates']],
            combined_question='Study the common interaction with distinct boundary conditions.',
            rationale='One scientific question with complementary predictions.', preserved_distinctions='Keep both sets of predictions.',
            passage_ids=[packet['source_passages'][0]['passage_id']])], rationale='Complementary questions.') )
    return packet, draft


@pytest.mark.parametrize('bad', [None, 'drop_hypothesis', 'unknown_member', 'overlap', 'same_member'])
def test_merge_identity_and_disjointness(source, bad):
    packet, draft = merge_fixture(source)
    if bad == 'drop_hypothesis': draft.merges[0].members[0].hypothesis_ids.pop()
    if bad == 'unknown_member': draft.merges[0].members[0].direction_id = 'missing'
    if bad == 'overlap': draft.merges.append(draft.merges[0].model_copy(deep=True))
    if bad == 'same_member': draft.merges[0].members[1] = draft.merges[0].members[0].model_copy(deep=True)
    if bad:
        with pytest.raises(ValueError): validate_portfolio(draft, packet)
    else:
        validate_portfolio(draft, packet)


def test_portfolio_independent_review(source):
    packet, _ = merge_fixture(source)
    chat, reviewer = Chat(), Chat()
    critic = ResearchCritic(None, chat, review_chat=reviewer)
    accepted, reviews, repairs, diagnostic = asyncio.run(critic._reviewed(packet, True))
    assert accepted and len(reviews) == 1 and not diagnostic
    assert len(chat.calls) == len(reviewer.calls) == 1


def test_candidate_calls_concurrent(source):
    root, novelty = source
    chat = Chat()
    critic = ResearchCritic(PaperStore(root), chat)
    d = novelty.inputs.generation.directions[0]
    async def execute():
        return await asyncio.gather(critic._candidate(novelty, d), critic._candidate(novelty, d))
    results = asyncio.run(execute())
    assert all(c.critique for c in results) and chat.peak == 2


def two_candidate_source(source):
    from directions.schemas import GenerationResult
    from novelty.schemas import NoveltySearchResult
    from novelty.comparison_schemas import NoveltyComparisonResult
    from novelty.assessment_schemas import AssessmentInputs
    root, novelty = source
    gen = novelty.inputs.generation.model_dump()
    original = gen['directions'][0]['direction_id']
    second = 'direction-second'
    gen['directions'].append(deepcopy(gen['directions'][0]))
    gen['directions'][1]['direction_id'] = second
    generation = GenerationResult.model_validate(gen)
    gen_ref = dict(schema_version=generation.schema_version, sha256=digest(generation.model_dump()))
    def rename(value):
        if isinstance(value, str): return second if value == original else value
        if isinstance(value, list): return [rename(x) for x in value]
        if isinstance(value, dict): return {second if k == original else k: rename(v) for k,v in value.items()}
        return value
    search = novelty.inputs.search.model_dump()
    search['directions_ref'] = gen_ref
    search['candidates'].append(rename(search['candidates'][0]))
    search['candidates'][1]['candidate_sha256'] = digest(gen['directions'][1])
    search = NoveltySearchResult.model_validate(search)
    comp = novelty.inputs.comparisons.model_dump()
    comp['directions_ref'] = gen_ref
    comp['search_ref'] = dict(schema_version=search.schema_version, sha256=digest(search.model_dump()))
    comp['candidates'].append(rename(comp['candidates'][0]))
    comp['candidates'][1]['candidate_sha256'] = digest(gen['directions'][1])
    comparison = NoveltyComparisonResult.model_validate(comp)
    inp = AssessmentInputs(generation=generation, search=search, comparisons=comparison)
    assessment, _ = assess_run(inp)
    return root, assessment


def test_complete_multi_candidate_merge_handoff(source):
    multi = two_candidate_source(source)
    def mutate(out, req):
        if req['task'] == 'critic_portfolio':
            packet = req['packet']
            out['merges'] = [dict(action='MERGE', members=[dict(direction_id=c['direction_id'],
                hypothesis_ids=[h['hypothesis_id'] for h in c['candidate']['hypotheses']]) for c in packet['candidates']],
                combined_question='One interaction question with complementary subquestions.',
                rationale='Complementary hypotheses within the same question.',
                preserved_distinctions='Keep every original hypothesis and its conditions.',
                passage_ids=[next(iter(packet['source_passages'].values()))[0]['passage_id']])]
    result, chat = run(multi, Chat(mutate))
    saved = CriticResult.model_validate_json(result.model_dump_json())
    assert len(saved.candidates) == 2 and all(c.critique for c in saved.candidates)
    assert len(saved.portfolio.assessment.merges) == 1
    assert len(chat.calls) == 6 and chat.peak == 2
    for c in saved.handoff()['candidates']:
        assert c['next_stage'] == 'merge_revision_then_correctness_and_novelty'
        assert len(c['requires_novelty_recheck_target_ids']) == 3


def test_unreviewed_portfolio_blocks_ranking(source):
    multi = two_candidate_source(source)
    def mutate(out, req):
        if req['task'] == 'review_critic_portfolio': out['decision'] = 'abstain'
    result, _ = run(multi, Chat(mutate))
    assert all(c.critique for c in result.candidates)
    assert result.portfolio.assessment is None
    assert all(c['next_stage'] == 'complete_portfolio_review' for c in result.handoff()['candidates'])


def test_upstream_unresolved_never_calls_critic(source):
    from novelty.assessment_schemas import NoveltyAssessmentResult
    root, novelty = source
    data = novelty.model_dump()
    data['candidates'][0]['assessment'] = None
    data['candidates'][0]['diagnostic'] = 'Awaiting source assessment'
    blocked = NoveltyAssessmentResult.model_validate(data)
    result, chat = run((root, blocked))
    assert not chat.calls
    assert result.candidates[0].blocked_at == 'upstream'
    assert result.handoff()['candidates'][0]['next_stage'] == 'resolve_upstream'


def test_usage_scopes_isolate_concurrent_requests():
    from types import SimpleNamespace
    from llm_client import usage
    async def work(n):
        with usage.scoped() as totals:
            await asyncio.sleep(.001)
            usage.record(SimpleNamespace(model='fixture-usage', choices=[], usage=SimpleNamespace(prompt_tokens=n, completion_tokens=2)))
            await asyncio.sleep(.001)
            return totals['fixture-usage']
    async def execute():
        return await asyncio.gather(work(11), work(23))
    a, b = asyncio.run(execute())
    assert (a.prompt, b.prompt) == (11, 23)
    assert a.calls == b.calls == 1


def test_review_cannot_pass_with_defects(source):
    def mutate(out, req):
        if 'review_' in req['task']:
            out['target_checks'][0]['defects'] = ['Unsupported source condition.']
    result, _ = run(source, Chat(mutate))
    assert result.candidates[0].critique is None
    assert result.candidates[0].reviews[-1].error


def test_reviewer_receives_matching_aliases(source):
    def mutate(out, req):
        if req['task'] == 'research_critique':
            out['targets'][0]['findings'][0]['basis'] = 'source_fact'
            out['targets'][0]['findings'][0]['passage_ids'] = [next(iter(req['packet']['source_passages'].values()))[0]['passage_id']]
    result, chat = run(source, Chat(mutate))
    review = chat.calls[1][0]
    ids = {p['passage_id'] for entries in review['packet']['source_passages'].values() for p in entries}
    assert set(review['draft']['targets'][0]['findings'][0]['passage_ids']) <= ids
    assert not result.candidates[0].critique.targets[0].findings[0].passage_ids[0].startswith('src')


def test_saved_correction_cannot_rewrite_unaffected_target(source):
    def mutate(out, req):
        if req['task'] == 'review_research_critique' and req['draft']['targets'][0]['rationale'] != 'Corrected':
            out['decision'] = 'revise'
            out['issues'] = [dict(field_path='/targets/0/rationale', explanation='Clarify.', required_change='Clarify.', passage_ids=[])]
        if req['task'] == 'revise_research_critique': out['targets'][0]['rationale'] = 'Corrected'
    result, _ = run(source, Chat(mutate))
    data = result.model_dump()
    data['candidates'][0]['reviews'][0]['draft']['targets'][1]['rationale'] = 'Different earlier judgment'
    with pytest.raises(ValueError, match='unaffected'): CriticResult.model_validate(data)


def test_second_use_rejected(source):
    root, novelty = source
    critic = ResearchCritic(PaperStore(root), Chat())
    asyncio.run(critic.run(novelty))
    with pytest.raises(ValueError, match='new critic'):
        asyncio.run(critic.run(novelty))


def test_downrank_is_rankable_without_novelty_change(source):
    def mutate(out, req):
        if 'review_' not in req['task']:
            out['targets'][0]['action'] = 'DOWNRANK'
            out['targets'][0]['rationale'] = 'Useful question, narrower information gain.'
    result, _ = run(source, Chat(mutate))
    route = result.handoff()['candidates'][0]
    assert route['scientific_action'] == 'DOWNRANK' and route['next_stage'] == 'ranking'
    assert route['requires_novelty_recheck_target_ids'] == []


@pytest.mark.parametrize('bad', ['missing', 'duplicate', 'unknown_hypothesis', 'hidden_contradiction'])
def test_test_link_audit_is_mandatory(source, bad):
    def mutate(out, req):
        if 'review_' not in req['task']: return
        if bad == 'missing': out['test_link_checks'] = []
        if bad == 'duplicate': out['test_link_checks'].append(deepcopy(out['test_link_checks'][0]))
        if bad == 'unknown_hypothesis': out['test_link_checks'][0]['hypothesis_id'] = 'unknown'
        if bad == 'hidden_contradiction': out['test_link_checks'][0]['decision'] = 'contradiction'
    result, _ = run(source, Chat(mutate))
    assert result.candidates[0].critique is None
    assert result.candidates[0].reviews[-1].error


def test_internal_test_contradiction_routes_upstream_without_fake_source_citation(source):
    def mutate(out, req):
        if 'review_' not in req['task']: return
        check = out['test_link_checks'][0]
        check['decision'] = 'contradiction'
        check['reasoning'] = 'The test labels the same measured zero interaction both falsifying and unresolved.'
        out['decision'] = 'revise'
        out['upstream_requests'] = [dict(stage='direction_correctness', target_id=check['hypothesis_id'],
            basis='proposal_consistency', field_paths=['/hypotheses/0/expected_effect', '/experiments/0/informative_outcomes'],
            passage_ids=[], reason=check['reasoning'])]
    result, chat = run(source, Chat(mutate))
    route = result.handoff()['candidates'][0]
    assert route['next_stage'] == 'resolve_upstream' and route['scientific_action'] is None
    assert route['upstream_requests'][0]['passage_ids'] == []
    assert len(chat.calls) == 2


def test_internal_request_requires_existing_candidate_fields(source):
    def mutate(out, req):
        if 'review_' not in req['task']: return
        out['decision'] = 'revise'
        out['upstream_requests'] = [dict(stage='direction_correctness', target_id=req['packet']['direction_id'],
            basis='proposal_consistency', field_paths=['/invented'], passage_ids=[], reason='Wrong field.')]
    result, _ = run(source, Chat(mutate))
    assert result.candidates[0].reviews[-1].error


def test_unknown_link_consistency_cannot_publish_pass(source):
    def mutate(out, req):
        if 'review_' in req['task']: out['test_link_checks'][0]['decision'] = 'uncertain'
    result, _ = run(source, Chat(mutate))
    assert result.candidates[0].critique is None


def test_v1_readable_but_v2_requires_test_audit(source):
    result, _ = run(source)
    data = result.model_dump()
    data['candidates'][0]['reviews'][0]['report'].pop('test_link_checks')
    with pytest.raises(ValueError, match='every hypothesis-experiment link'):
        CriticResult.model_validate(data)
    data['schema_version'] = 'research_critic_v1'
    assert CriticResult.model_validate(data).candidates[0].critique


def test_shared_experiment_refinement_rechecks_every_linked_hypothesis(source):
    def mutate(out, req):
        if 'review_' in req['task']: return
        out['targets'][1]['action'] = 'REFINE'
        out['revisions'] = [dict(target_id=out['targets'][1]['target_id'], field_path='/experiments/0/informative_outcomes',
            required_change='Distinguish measured equality from unavailable data.', reason='Clarify the boundary interpretation.')]
    result, _ = run(source, Chat(mutate))
    c = result.candidates[0]
    assert c.critique
    assert set(c.handoff()['requires_novelty_recheck_target_ids']) == {c.direction_id, *c.packet['candidate']['experiments'][0]['hypothesis_ids']}


def test_experiment_revision_cannot_name_unlinked_hypothesis(source):
    result, _ = run(source)
    c = result.candidates[0]
    draft = c.critique.model_dump()
    draft['targets'][2]['action'] = 'REFINE'
    draft['revisions'] = [dict(target_id=draft['targets'][2]['target_id'], field_path='/experiments/0/informative_outcomes',
        required_change='Clarify.', reason='Incorrectly associated test.')]
    packet = deepcopy(c.packet)
    packet['candidate']['experiments'][0]['hypothesis_ids'] = [draft['targets'][1]['target_id']]
    with pytest.raises(ValueError, match='linked hypothesis'):
        validate_draft(CritiqueDraft.model_validate(draft), packet)


def test_known_revision_collection_path_normalization_preserves_review(source):
    from critic.evidence import normalize_review_paths
    result, _ = run(source)
    c = result.candidates[0]
    data = c.critique.model_dump()
    data['targets'][1]['action'] = 'REFINE'
    data['revisions'] = [dict(target_id=data['targets'][1]['target_id'], field_path='/hypotheses/0/condition',
        required_change='Clarify the tested condition.', reason='Needed for comparison.')]
    draft = CritiqueDraft.model_validate(data)
    report = ReviewReport.model_validate({**review_for(c.packet), 'decision':'revise',
        'issues':[dict(field_path='/targets/1/revisions', explanation='Revision needs clearer scope.',
            required_change='Clarify the requested scientific scope.', passage_ids=[])]})
    normalized, changes = normalize_review_paths(report, draft)
    assert normalized.issues[0].field_path == '/revisions'
    assert changes == [dict(from_path='/targets/1/revisions', to_path='/revisions', target_id=data['targets'][1]['target_id'])]
    restored = normalized.model_dump()
    restored['issues'][0]['field_path'] = report.issues[0].field_path
    assert restored == report.model_dump()
    # Unknown target/field and a target with no revisions remain invalid.
    for path in ('/targets/0/revisions', '/targets/999/revisions', '/targets/1/invented', '/targets/1/revisions/0'):
        report.issues[0].field_path = path
        unchanged, changes = normalize_review_paths(report, draft)
        assert unchanged == report and changes == []
