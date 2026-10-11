"""Sections 13–14: coverage asymmetry, provenance, refinement and independent review."""
import asyncio
from copy import deepcopy
import json

import pytest

from llm_client import ChatResult
from novelty.assessment import NoveltyAssessor, Settings
from novelty.assessment_schemas import (AssessmentDraft, AssessmentInputs, AssessmentReport,
    Invalidations, Invalidation, NoveltyAssessmentResult, CandidateAssessment, ScientificEdit)
from novelty.assessment_evidence import build_packet, validate_draft, validate_report
from opportunities.miner import digest
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready, run as compare_run


@pytest.fixture
def bundle(ready):
    comparison, _ = compare_run(ready)
    return AssessmentInputs(generation=ready[1], search=ready[2], comparisons=comparison)


def proposal(packet):
    targets = []
    for row in packet['coverage']:
        evidence = [e for e in packet['evidence'] if e['target_id'] == row['target_id']]
        targets.append(dict(target_id=row['target_id'],
            finding='PARTIAL_OVERLAP' if evidence else 'UNRESOLVED',
            reasoning='Related caching work; the proposed interaction is not established.',
            evidence=[dict(paper_id=e['paper_id'], target_id=e['target_id'],
                claim_ids=[c['claim_id'] for c in e['claims']]) for e in evidence],
            meaningful_difference='Joint compression and cache interaction remains unestablished.' if evidence else None))
    return dict(targets=targets, refinement=dict(
        action='retain' if all(c['complete'] for c in packet['coverage']) else 'defer',
        rationale='Keep the original proposed relationships subject to the recorded scope.',
        retained_hypothesis_ids=[h['hypothesis_id'] for h in packet['candidate']['hypotheses']],
        removed_hypothesis_ids=[], scientific_edits=[]))


class Chat:
    def __init__(self, mutate=None, decisions=None, fail=None):
        self.mutate, self.decisions, self.fail = mutate, decisions or ['pass'], fail
        self.calls, self.reviews = [], 0
        self.active = self.peak = 0

    async def __call__(self, **kw):
        req = json.loads(kw['messages'][1]['content'])
        self.calls.append((req, kw))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.01)
        self.active -= 1
        if req['task'] == self.fail:
            raise RuntimeError('injected provider failure')
        if 'review_novelty_assessment' in req['task']:
            decision = self.decisions[min(self.reviews, len(self.decisions)-1)]
            self.reviews += 1
            out = dict(decision=decision, summary='Independent fixture review.',
                issues=['Explain the scientific distinction.'] if decision == 'revise' else [],
                reassessment_requests=[], scope_checks=[dict(target_id=c['target_id'], decision='pass', summary='Scoped to cited claims.', scope_defects=[]) for c in req['packet']['coverage']])
        else:
            out = proposal(req['packet'])
            if req['task'] == 'revise_novelty_assessment':
                out['targets'][0]['reasoning'] = 'Revised scope of the original prediction.'
        if self.mutate:
            self.mutate(out, req)
        if req['packet'].get('assessment_contract') == 'isolated_v1' and 'review_novelty_assessment' not in req['task']:
            if len(req['packet']['coverage']) == 1:
                out=out['targets'][0]
            else:
                out=dict(direction=out['targets'][0],refinement=out['refinement'])
        return ChatResult(json.dumps(out), 'stop', 'fixture')


def run(bundle, chat=None, reviewer=None, **settings):
    chat = chat or Chat()
    result = asyncio.run(NoveltyAssessor(chat, review_chat=reviewer,
        settings=Settings(**settings)).run(bundle.generation, bundle.search, bundle.comparisons,
            invalidations=bundle.invalidations))
    return result, chat


def packet_for(bundle):
    return build_packet(bundle, bundle.generation.directions[0].direction_id)


def test_full_joint_context_and_reviewed_round_trip(bundle):
    result, chat = run(bundle)
    restored = NoveltyAssessmentResult.model_validate_json(result.model_dump_json())
    c = restored.candidates[0]
    assert c.assessment and all(t.complete for t in c.coverage)
    assert len(chat.calls) == 2
    packet = chat.calls[0][0]['packet']
    assert len(packet['evidence']) == 3 and len(packet['source_passages']) == 2
    assert packet['candidate'] == bundle.generation.directions[0].proposal.model_dump()
    assert c.refinement_handoff(bundle.generation.directions[0])['next_stage'] == 'research_critic'
    assert all(r['literature_wide_novelty'] == 'unverified' for r in c.outcomes())


@pytest.mark.parametrize('field', ['coverage', 'candidate', 'assessment', 'inputs', 'review'])
def test_saved_artifact_rejects_tampering(bundle, field):
    result, _ = run(bundle)
    data = result.model_dump()
    c = data['candidates'][0]
    if field == 'coverage':
        c['coverage'][0]['complete'] = False
    elif field == 'candidate':
        c['candidate_sha256'] = 'changed'
    elif field == 'assessment':
        c['assessment']['targets'][0]['reasoning'] = 'Not independently reviewed'
    elif field == 'inputs':
        data['inputs']['comparisons']['calls']['total'] += 1
    else:
        c['reviews'][-1]['report']['decision'] = 'abstain'
    with pytest.raises(ValueError):
        NoveltyAssessmentResult.model_validate(data)


@pytest.mark.parametrize('which', ['generation_ref', 'search_ref', 'candidate', 'shortlist', 'retrieval', 'prior_hash'])
def test_cross_stage_mismatch_rejected_before_calls(bundle, which):
    b = bundle.model_copy(deep=True)
    c = b.comparisons.candidates[0]
    if which == 'generation_ref': b.comparisons.directions_ref['sha256'] = 'stale'
    elif which == 'search_ref': b.comparisons.search_ref['sha256'] = 'stale'
    elif which == 'candidate': c.candidate_sha256 = 'stale'
    elif which == 'shortlist': c.shortlist[next(iter(c.shortlist))] = []
    elif which == 'retrieval': c.retrieval_status[next(iter(c.retrieval_status))] = 'failed'
    else: c.papers[0].expected_artifact_sha256 = 'stale'
    chat = Chat()
    with pytest.raises(ValueError):
        run(b, chat)
    assert not chat.calls


def invalidate(bundle, targets=None, claims=None):
    b = bundle.model_copy(deep=True)
    c = b.comparisons.candidates[0]
    b.invalidations = Invalidations(comparison_sha256=digest(b.comparisons.model_dump()),
        entries=[Invalidation(direction_id=c.direction_id, paper_id=c.papers[0].paper_id,
            target_ids=targets or [], claim_ids=claims or [], reason='Manually rejected source attribution.')])
    return b


def test_entire_paper_invalidation_has_zero_calls_and_no_false_novelty(bundle):
    result, chat = run(invalidate(bundle))
    c = result.candidates[0]
    assert not chat.calls and c.assessment is None
    assert all(t.papers[0].status == 'invalidated' and not t.complete for t in c.coverage)
    assert all(o['novelty_status'] == 'unresolved' for o in c.outcomes())


def test_local_invalidation_retains_other_hypotheses(bundle):
    tid = bundle.generation.directions[0].proposal.hypotheses[0].hypothesis_id
    result, chat = run(invalidate(bundle, [tid], ['c1']))
    c = result.candidates[0]
    assert c.assessment and c.assessment.refinement.action == 'defer'
    assert next(t for t in c.assessment.targets if t.target_id == tid).finding == 'UNRESOLVED'
    assert len(chat.calls[0][0]['packet']['evidence']) == 2


@pytest.mark.parametrize('bad', ['hash', 'claim', 'target', 'paper'])
def test_invalidation_must_be_exact_and_resolvable(bundle, bad):
    b = invalidate(bundle)
    if bad == 'hash': b.invalidations.comparison_sha256 = 'wrong'
    elif bad == 'claim': b.invalidations.entries[0].claim_ids = ['unknown']
    elif bad == 'target': b.invalidations.entries[0].target_ids = ['unknown']
    else: b.invalidations.entries[0].paper_id = 'unknown'
    with pytest.raises(ValueError):
        run(b)


@pytest.mark.parametrize('state', ['skipped', 'failed', 'withheld', 'invalidated', 'missing_pages', 'retrieval_partial', 'empty'])
def test_incomplete_coverage_cannot_be_low_overlap(bundle, state):
    p = packet_for(bundle)
    row = p['coverage'][0]
    row['complete'] = False
    row['blockers'] = [state]
    d = AssessmentDraft.model_validate(proposal(p))
    d.targets[0].finding = 'LOW_PRIOR_OVERLAP'
    with pytest.raises(ValueError, match='complete'):
        validate_draft(d, p)


def test_low_overlap_allowed_only_with_scientific_difference(bundle):
    p = packet_for(bundle)
    d = AssessmentDraft.model_validate(proposal(p))
    d.targets[0].finding = 'LOW_PRIOR_OVERLAP'
    validate_draft(d, p)
    d.targets[0].meaningful_difference = None
    with pytest.raises(ValueError, match='scientific distinction'):
        validate_draft(d, p)


@pytest.mark.parametrize('prior', ['SAME', 'VERY_CLOSE', 'direct_empirical', 'direct_theoretical', 'inferred_implication'])
def test_matching_or_inferred_counterevidence_blocks_low_overlap(bundle, prior):
    p = packet_for(bundle)
    e = p['evidence'][0]
    if prior in ('SAME', 'VERY_CLOSE'): e['comparison']['classification'] = prior
    else: e['relationship']['coverage'] = prior
    d = AssessmentDraft.model_validate(proposal(p))
    d.targets[0].finding = 'LOW_PRIOR_OVERLAP'
    with pytest.raises(ValueError, match='prior coverage'):
        validate_draft(d, p)


@pytest.mark.parametrize('result', ['supports_prediction', 'contradicts_prediction', 'mixed', 'inconclusive'])
def test_direct_investigation_is_known_even_with_negative_result_and_gaps(bundle, result):
    p = packet_for(bundle)
    tid = p['coverage'][1]['target_id']
    row = p['coverage'][1]
    row['complete'], row['blockers'] = False, ['Another shortlisted paper was skipped.']
    e = next(e for e in p['evidence'] if e['target_id'] == tid)
    e['relationship'].update(coverage='direct_empirical', result=result)
    d = AssessmentDraft.model_validate(proposal(p))
    d.targets[1].finding = 'ALREADY_STUDIED'
    validate_draft(d, p)
    c = CandidateAssessment(direction_id=p['direction_id'], candidate_sha256=p['candidate_sha256'],
        coverage=p['coverage'], assessment=d, reviews=[], format_repairs=[], diagnostic=None)
    assert c.outcomes()[1]['novelty_status'] == 'overlap_found'


def test_one_matching_paper_is_not_outvoted(bundle):
    p = packet_for(bundle)
    p['evidence'][0]['comparison']['classification'] = 'SAME'
    for i in range(20):
        adjacent = deepcopy(p['evidence'][0])
        adjacent['paper_id'] = f'adjacent-{i}'
        adjacent['comparison']['classification'] = 'ADJACENT'
        p['evidence'].append(adjacent)
    d = AssessmentDraft.model_validate(proposal(p))
    d.targets[0].finding = 'ALREADY_STUDIED'
    validate_draft(d, p)


@pytest.mark.parametrize('coverage', ['related', 'discussed', 'inferred_implication', 'no_match_found'])
def test_separate_components_or_inferences_are_not_tested_relationship(bundle, coverage):
    p = packet_for(bundle)
    p['evidence'][1]['relationship']['coverage'] = coverage
    d = AssessmentDraft.model_validate(proposal(p))
    d.targets[1].finding = 'ALREADY_STUDIED'
    with pytest.raises(ValueError, match='direct target evidence'):
        validate_draft(d, p)
    d.targets[1].finding = 'COMPONENTS_KNOWN'
    validate_draft(d, p)


def test_known_hypothesis_removal_preserves_other_original_hypothesis_and_test_links(bundle):
    p = packet_for(bundle)
    d = AssessmentDraft.model_validate(proposal(p))
    known, surviving = p['candidate']['hypotheses']
    p['evidence'][1]['relationship']['coverage'] = 'direct_empirical'
    d.targets[1].finding = 'ALREADY_STUDIED'
    d.refinement.action = 'narrow'
    d.refinement.removed_hypothesis_ids = [known['hypothesis_id']]
    d.refinement.retained_hypothesis_ids = [surviving['hypothesis_id']]
    validate_draft(d, p)
    original = bundle.generation.directions[0].model_dump()
    c = CandidateAssessment(direction_id=p['direction_id'], candidate_sha256=p['candidate_sha256'],
        coverage=p['coverage'], assessment=d, reviews=[], format_repairs=[], diagnostic=None)
    handoff = c.refinement_handoff(bundle.generation.directions[0])
    assert handoff['requires_novelty_recheck_target_ids'] == [p['direction_id']]
    assert all(ids == [surviving['hypothesis_id']] for ids in handoff['retained_experiment_links'].values())
    assert bundle.generation.directions[0].model_dump() == original


def test_scientific_edits_require_recheck_and_do_not_mutate_original(bundle):
    p = packet_for(bundle)
    data = proposal(p)
    data['refinement'].update(action='reframe', scientific_edits=[dict(
        field_path='/hypotheses/0/condition', proposed_value='Under abrupt routing distribution shifts.',
        reason='Make the mechanism-relevant boundary explicit.')])
    d = AssessmentDraft.model_validate(data)
    validate_draft(d, p)
    c = CandidateAssessment(direction_id=p['direction_id'], candidate_sha256=p['candidate_sha256'],
        coverage=p['coverage'], assessment=d, reviews=[], format_repairs=[], diagnostic=None)
    out = c.refinement_handoff(bundle.generation.directions[0])
    assert out['proposal_applied'] is False
    assert set(out['requires_novelty_recheck_target_ids']) == {p['direction_id'], p['candidate']['hypotheses'][0]['hypothesis_id']}
    assert p['candidate']['hypotheses'][0]['condition'] != d.refinement.scientific_edits[0].proposed_value


@pytest.mark.parametrize('path', ['/title', '/hypotheses/0/evidence_ids', '/hypotheses/0/hypothesis_id', '/hypotheses/99/condition', '/experiments/0/objective'])
def test_scientific_plan_cannot_edit_evidence_ids_or_unlinked_protocols(bundle, path):
    p = packet_for(bundle)
    d = proposal(p)
    d['refinement'].update(action='reframe', scientific_edits=[dict(field_path=path, proposed_value='Changed.', reason='Test.')])
    with pytest.raises(ValueError):
        validate_draft(AssessmentDraft.model_validate(d), p)


def test_separate_reviewer_and_bounded_correction(bundle):
    author, reviewer = Chat(), Chat(decisions=['revise', 'pass'])
    result, _ = run(bundle, author, reviewer)
    c = result.candidates[0]
    assert c.assessment and len(c.reviews) == 2
    assert c.assessment.targets[0].reasoning.startswith('Revised')
    assert len(author.calls) == len(reviewer.calls) == 2
    for req, kw in reviewer.calls:
        assert len(kw['messages']) == 2 and 'corrections' not in req and 'previous_assessment' not in req


@pytest.mark.parametrize('decisions', [['abstain'], ['revise', 'revise']])
def test_no_unreviewed_fallback_on_review_failure(bundle, decisions):
    result, _ = run(bundle, reviewer=Chat(decisions=decisions))
    assert result.candidates[0].assessment is None
    assert all(r['novelty_status'] == 'unresolved' for r in result.candidates[0].outcomes())


def test_source_reassessment_stops_synthesis_without_changing_evidence(bundle):
    def mutate(out, req):
        e = req['packet']['evidence'][0]
        out.update(decision='revise', issues=['Claim qualifier needs rechecking.'],
            reassessment_requests=[dict(paper_id=e['paper_id'], target_ids=[e['target_id']],
                claim_ids=['c1'], passage_ids=[e['claims'][0]['passage_ids'][0]], reason='Material qualifier missing.')])
    before = bundle.comparisons.model_dump()
    result, _ = run(bundle, reviewer=Chat(mutate))
    c = result.candidates[0]
    assert c.assessment is None and len(c.reviews) == 1
    assert c.reviews[0].report.reassessment_requests
    assert result.inputs.comparisons.model_dump() == before


@pytest.mark.parametrize('fault', ['source', 'target', 'claim'])
def test_reassessment_requests_cannot_invent_references(bundle, fault):
    p = packet_for(bundle)
    e = p['evidence'][0]
    request = dict(paper_id=e['paper_id'], target_ids=[e['target_id']], claim_ids=['c1'],
        passage_ids=[e['claims'][0]['passage_ids'][0]], reason='Inspect scope.')
    if fault == 'source': request['passage_ids'] = ['nonexistent']
    elif fault == 'target': request['target_ids'] = ['nonexistent']
    else: request['claim_ids'] = ['nonexistent']
    report = AssessmentReport(decision='revise', summary='Source review.', issues=[], reassessment_requests=[request], scope_checks=[dict(target_id=c['target_id'], decision='pass', summary='Scoped.', scope_defects=[]) for c in p['coverage']])
    with pytest.raises(ValueError):
        validate_report(report, p)


@pytest.mark.parametrize('fault', ['claim', 'target', 'missing_target', 'missing_hypothesis', 'low_overlap'])
def test_invalid_output_is_bounded_and_never_published(bundle, fault):
    def mutate(out, req):
        if 'review' in req['task']: return
        if fault == 'claim': out['targets'][0]['evidence'][0]['claim_ids'] = ['invented']
        elif fault == 'target': out['targets'][0]['target_id'] = 'invented'
        elif fault == 'missing_target': out['targets'].pop()
        elif fault == 'missing_hypothesis': out['refinement']['retained_hypothesis_ids'].pop()
        else:
            out['targets'][0]['finding'] = 'LOW_PRIOR_OVERLAP'
            out['targets'][0]['meaningful_difference'] = None
    result, chat = run(bundle, Chat(mutate))
    assert result.candidates[0].assessment is None and len(chat.calls) == 2
    assert len(result.candidates[0].format_repairs) == 2


def test_format_repair_of_reviewer_routes_to_reviewer(bundle):
    def mutate(out, req):
        if req['task'] == 'review_novelty_assessment': out['unexpected'] = True
    author, reviewer = Chat(), Chat(mutate)
    result, _ = run(bundle, author, reviewer)
    assert result.candidates[0].assessment
    assert len(author.calls) == 1 and len(reviewer.calls) == 2
    assert reviewer.calls[-1][0]['task'] == 'repair_review_novelty_assessment'


@pytest.mark.parametrize('budget,expected', [(0, 0), (1, 1)])
def test_attempt_budget_prevents_unreviewed_promotion(bundle, budget, expected):
    result, chat = run(bundle, max_calls=budget)
    assert result.calls['total'] == len(chat.calls) == expected
    assert result.candidates[0].assessment is None


def test_full_input_budget_fails_without_truncation_or_api_call(bundle):
    result, chat = run(bundle, max_input_tokens=1)
    assert not chat.calls
    assert 'input_budget' in result.candidates[0].diagnostic



def test_input_budget_counts_message_text_without_losing_source_content(bundle):
    from novelty import assessment_prompts as prompts
    from novelty.pipeline import NoveltySearcher, Settings as CallSettings
    from reasoning.budget import estimate_tokens
    packet = packet_for(bundle)
    # Dense source quotes and Unicode must survive exactly; wire escaping is
    # not extra model context and must not cause a false budget rejection.
    packet['source_passages'][0]['text'] = ('"quoted" \n σ ≤ 1.8 ' * 2000)
    chat = Chat()
    async def work():
        io = NoveltySearcher(None, None, chat, settings=CallSettings())
        await io._call('assess_novelty', prompts.ASSESS, AssessmentDraft, {'packet': packet})
        messages = chat.calls[-1][1]['messages']
        allowance = sum(estimate_tokens(m['content']) + 32 for m in messages)
        assert estimate_tokens(json.dumps(messages)) > allowance
        io.settings.max_input_tokens = allowance
        await io._call('assess_novelty', prompts.ASSESS, AssessmentDraft, {'packet': packet})
        assert chat.calls[-1][0]['packet'] == packet
        io.settings.max_input_tokens = allowance - 1
        with pytest.raises(ValueError, match='input_budget'):
            await io._call('assess_novelty', prompts.ASSESS, AssessmentDraft, {'packet': packet})
        assert len(chat.calls) == 2
    asyncio.run(work())

def test_provider_failure_is_explicit(bundle):
    result, _ = run(bundle, reviewer=Chat(fail='review_novelty_assessment'))
    c = result.candidates[0]
    assert c.assessment is None and c.reviews[0].error
    NoveltyAssessmentResult.model_validate_json(result.model_dump_json())


def test_direction_calls_overlap_with_shared_budget(bundle):
    # Independent candidate workers use the production shared semaphore and counter.
    async def work():
        chat = Chat()
        assessor = NoveltyAssessor(chat, settings=Settings(concurrency=2, max_calls=6))
        packet = packet_for(bundle)
        results = await asyncio.gather(*(assessor._candidate(deepcopy(packet)) for _ in range(3)))
        return results, chat, assessor.io.budget.snapshot()
    results, chat, calls = asyncio.run(work())
    assert all(c.assessment for c in results)
    assert chat.peak == 2 and calls['total'] == 6


def test_run_instance_cannot_be_reused(bundle):
    async def work():
        assessor = NoveltyAssessor(Chat())
        await assessor.run(bundle.generation, bundle.search, bundle.comparisons)
        with pytest.raises(ValueError, match='new novelty assessor'):
            await assessor.run(bundle.generation, bundle.search, bundle.comparisons)
    asyncio.run(work())

@pytest.mark.parametrize('status', ['skipped', 'failed', 'unresolved'])
def test_ledger_uses_actual_paper_outcomes(bundle, status):
    b = bundle.model_copy(deep=True)
    p = b.comparisons.candidates[0].papers[0]
    p.status, p.comparison, p.diagnostic = status, None, 'Intentionally incomplete fixture.'
    p.reviews, p.evidence_reviews = [], []
    p.passages, p.context_pages = [], []
    result, chat = run(b)
    c = result.candidates[0]
    expected = 'withheld' if status == 'unresolved' else status
    assert all(t.papers[0].status == expected and not t.complete for t in c.coverage)
    assert not chat.calls


def test_missing_referenced_page_retains_positive_evidence_but_blocks_novelty(bundle):
    b = bundle.model_copy(deep=True)
    b.comparisons.candidates[0].papers[0].missing_referenced_pages = [99]
    result, chat = run(b)
    c = result.candidates[0]
    assert c.assessment.refinement.action == 'defer'
    assert all(not t.complete and t.papers[0].status == 'reviewed' for t in c.coverage)
    assert chat.calls[0][0]['packet']['evidence']
    assert all(o['novelty_status'] == 'unresolved' for o in c.outcomes())


def test_partial_retrieval_is_not_hidden_by_completed_comparisons(bundle):
    b = bundle.model_copy(deep=True)
    r = b.search.candidates[0].searches[0]
    r.status, r.diagnostic = 'partial', 'One query failed.'
    b.comparisons.search_ref['sha256'] = digest(b.search.model_dump())
    b.comparisons.candidates[0].retrieval_status[r.target_id] = 'partial'
    result, _ = run(b)
    c = result.candidates[0]
    assert not c.coverage[0].complete and c.assessment.refinement.action == 'defer'


def test_no_shortlist_never_becomes_low_overlap(bundle):
    b = bundle.model_copy(deep=True)
    for s in b.search.candidates[0].searches:
        s.ranked = []
    b.comparisons.search_ref['sha256'] = digest(b.search.model_dump())
    c = b.comparisons.candidates[0]
    c.shortlist = {t: [] for t in c.shortlist}
    c.papers = []
    result, chat = run(b)
    assert not chat.calls and all(not t.complete for t in result.candidates[0].coverage)


def test_absent_signature_preserves_all_original_targets(bundle):
    b = bundle.model_copy(deep=True)
    s = b.search.candidates[0]
    s.signature, s.searches, s.diagnostic = None, [], 'Signature review failed.'
    c = b.comparisons.candidates[0]
    c.signature, c.shortlist, c.retrieval_status, c.papers, c.diagnostic = None, {}, {}, [], s.diagnostic
    b.comparisons.search_ref['sha256'] = digest(b.search.model_dump())
    result, chat = run(b)
    assert not chat.calls and len(result.candidates[0].coverage) == 3
    assert all(t.retrieval_status == 'not_run' for t in result.candidates[0].coverage)


def test_reject_cannot_hide_a_reframe(bundle):
    p = packet_for(bundle)
    d = AssessmentDraft.model_validate(proposal(p))
    d.refinement.action = 'reject'
    d.refinement.removed_hypothesis_ids = d.refinement.retained_hypothesis_ids
    d.refinement.retained_hypothesis_ids = []
    d.refinement.scientific_edits = [ScientificEdit(field_path='/scope', proposed_value='New regime.', reason='New question.')]
    d = AssessmentDraft.model_validate(d.model_dump())
    with pytest.raises(ValueError, match='reject'):
        validate_draft(d, p)

def test_upstream_reassessment_blocks_even_when_synthesis_is_correct(bundle):
    def mutate(out, req):
        e = req['packet']['evidence'][0]
        out['reassessment_requests'] = [dict(paper_id=e['paper_id'], target_ids=[e['target_id']],
            claim_ids=['c1'], passage_ids=e['claims'][0]['passage_ids'], reason='Upstream source scope requires rechecking.')]
    result, chat = run(bundle, reviewer=Chat(mutate))
    c = result.candidates[0]
    assert c.assessment is None and len(c.reviews) == 1
    assert c.reviews[0].report.decision == 'pass'
    assert c.reviews[0].report.reassessment_requests


@pytest.mark.parametrize('problem', ['missing_target', 'hidden_scope_defect', 'unresolved_check'])
def test_scope_review_cannot_hide_or_omit_target_defects(bundle, problem):
    def mutate(out, req):
        if problem == 'missing_target': out['scope_checks'].pop()
        elif problem == 'hidden_scope_defect':
            out['scope_checks'][0]['scope_defects'] = ['Result wrongly attributed to both models.']
        else:
            out['scope_checks'][0]['decision'] = 'abstain'
    result, _ = run(bundle, reviewer=Chat(mutate))
    assert result.candidates[0].assessment is None


def test_model_packet_preserves_every_source_and_comparison(bundle):
    from novelty.assessment_evidence import model_packet, source_aliases, map_source_refs
    packet = packet_for(bundle)
    original = deepcopy(packet)
    packed = model_packet(packet)
    assert packet == original
    reverse = {v:k for k,v in source_aliases(packet).items()}
    sources = [dict(passage_id=pid,text=text) for page in packed['source_passages'].values() for pid,text in page.items()]
    assert len(sources) == len(packet['source_passages'])
    assert [(reverse[p['passage_id']], p['text']) for p in sources] == [
        (p['passage_id'], p['text']) for p in packet['source_passages']]
    decoded = map_source_refs(packed, reverse)
    for key in set(packet) - {'source_passages'}:
        assert decoded[key] == packet[key]
    result, chat = run(bundle)
    assert result.candidates[0].assessment
    for request, _ in chat.calls:
        assert request['packet'] == packed
    assert result.inputs.comparisons == bundle.comparisons


def test_aliased_reassessment_sources_restore_exact_original_ids(bundle):
    packet = packet_for(bundle)
    source = packet['source_passages'][0]
    row = packet['evidence'][0]
    def mutate(out, req):
        if req['task']=='review_novelty_assessment':
            out['reassessment_requests']=[dict(paper_id=row['paper_id'],target_ids=[row['target_id']],
                claim_ids=[row['claims'][0]['claim_id']],passage_ids=['src0'],reason='Source scope needs rechecking.')]
    result,_ = run(bundle,Chat(mutate))
    candidate=result.candidates[0]
    assert candidate.assessment is None
    assert candidate.reviews[-1].report.reassessment_requests[0].passage_ids == [source['passage_id']]
    NoveltyAssessmentResult.model_validate_json(result.model_dump_json())


def test_unknown_source_alias_cannot_pass_review(bundle):
    packet = packet_for(bundle);row=packet['evidence'][0]
    def mutate(out, req):
        if req['task'] in ('review_novelty_assessment','repair_review_novelty_assessment'):
            out['reassessment_requests']=[dict(paper_id=row['paper_id'],target_ids=[row['target_id']],
                claim_ids=[],passage_ids=['src999999999'],reason='Unknown source.')]
    result,_ = run(bundle,Chat(mutate))
    assert result.candidates[0].assessment is None
    assert result.candidates[0].reviews[-1].error


def test_reopen_published_synthesis_with_observation_gets_fresh_independent_review(bundle):
    parent,_=run(bundle)
    did=parent.candidates[0].direction_id
    chat=Chat()
    result=asyncio.run(NoveltyAssessor(chat).run(bundle.generation,bundle.search,bundle.comparisons,
        previous=parent,observations={did:['Keep the unspecified measurement scope explicit.']}))
    assert result.inputs==parent.inputs
    assert result.candidates[0].assessment!=parent.candidates[0].assessment
    assert [r['task'] for r,_ in chat.calls]==['revise_novelty_assessment','review_novelty_assessment']
    assert 'review_observations' in chat.calls[0][0]
    assert 'review_observations' not in chat.calls[1][0]
    assert result.run['reassessment_parent_sha256']==digest(parent.model_dump())
    assert NoveltyAssessmentResult.model_validate_json(result.model_dump_json())==result


def test_reopen_synthesis_cannot_publish_rejected_revision(bundle):
    parent,_=run(bundle);did=parent.candidates[0].direction_id
    chat=Chat(decisions=['revise'])
    result=asyncio.run(NoveltyAssessor(chat).run(bundle.generation,bundle.search,bundle.comparisons,
        previous=parent,observations={did:['Check source scope.']}))
    assert result.candidates[0].assessment is None
    assert parent.candidates[0].assessment is not None


@pytest.mark.parametrize('change',['inputs','unknown','empty','missing_parent'])
def test_invalid_synthesis_reopening_rejected_before_calls(bundle,change):
    parent,_=run(bundle);did=parent.candidates[0].direction_id
    observations={did:['Check scope.']};comparison=bundle.comparisons.model_copy(deep=True)
    if change=='inputs':comparison.run['reopening_test']='changed'
    elif change=='unknown':observations={'unknown':['Check scope.']}
    elif change=='empty':observations={did:[]}
    else:parent=None
    chat=Chat()
    with pytest.raises(ValueError):asyncio.run(NoveltyAssessor(chat).run(bundle.generation,bundle.search,comparison,
        previous=parent,observations=observations))
    assert not chat.calls


def test_source_scope_guidance_requires_fresh_independent_review(bundle):
    did=bundle.generation.directions[0].direction_id;chat=Chat(decisions=['abstain'])
    result=asyncio.run(NoveltyAssessor(chat).run(bundle.generation,bundle.search,bundle.comparisons,
        guidance={did:['Check unspecified measurement conditions against the original source.']}))
    assert result.candidates[0].assessment is None
    assert 'review_observations' in chat.calls[0][0]
    assert 'review_observations' not in chat.calls[1][0]


def test_citation_error_reports_every_invalid_tuple_with_allowed_claim_ids(bundle):
    packet=packet_for(bundle);draft=AssessmentDraft.model_validate(proposal(packet))
    sibling=next(e for e in packet['evidence'] if e['target_id']==draft.targets[1].target_id)
    sibling['claims'].append({**sibling['claims'][0],'claim_id':'sibling_only'})
    draft.targets[0].evidence[0].claim_ids.append('sibling_only')
    draft.targets[1].evidence[0].paper_id='unknown-paper'
    with pytest.raises(ValueError,match='assessment cites unreviewed') as exc:
        validate_draft(draft,packet)
    errors=json.loads(str(exc.value).split(': ',1)[1])
    assert [e['field_path'] for e in errors]==['/targets/0/evidence/0','/targets/1/evidence/0']
    assert errors[0]['invalid_claim_ids']==['sibling_only']
    assert errors[0]['allowed_claim_ids']==['c1']
    assert errors[1]['allowed_claim_ids']==[]
    assert errors[1]['error']=='unavailable paper/target evidence'


def test_precise_citation_repair_still_needs_fresh_independent_review(bundle):
    def mutate(out,req):
        if req['task']=='assess_novelty':
            out['targets'][0]['evidence'][0]['claim_ids'].append('bad-0')
            out['targets'][1]['evidence'][0]['claim_ids'].append('bad-1')
        if req['task']=='repair_assess_novelty':
            errors=json.loads(req['validation_errors'].split(': ',1)[1])
            assert [x['invalid_claim_ids'] for x in errors]==[['bad-0'],['bad-1']]
    result,chat=run(bundle,Chat(mutate))
    assert result.candidates[0].assessment
    assert [q['task'] for q,_ in chat.calls]==['assess_novelty','repair_assess_novelty','review_novelty_assessment']
    review=chat.calls[-1][0]
    assert 'invalid_output' not in review and 'validation_errors' not in review


@pytest.mark.parametrize('decision',['pass','revise'])
def test_resume_withheld_synthesis_uses_exact_draft_and_review_feedback(bundle,decision):
    parent,_=run(bundle,Chat(decisions=['revise']));candidate=parent.candidates[0]
    assert candidate.assessment is None
    chat=Chat(decisions=[decision])
    result=asyncio.run(NoveltyAssessor(chat).run(bundle.generation,bundle.search,bundle.comparisons,
        previous=parent,observations={candidate.direction_id:['Verify the saved scope corrections.']}))
    request=chat.calls[0][0]
    assert request['previous_assessment']==candidate.reviews[-1].draft.model_dump()
    assert request['corrections']==candidate.reviews[-1].report.model_dump()
    assert (result.candidates[0].assessment is not None)==(decision=='pass')
    assert parent.candidates[0].assessment is None
    review=chat.calls[1][0]
    assert 'corrections' not in review and 'previous_assessment' not in review
    assert NoveltyAssessmentResult.model_validate_json(result.model_dump_json())==result


@pytest.mark.parametrize('blocker',['upstream','abstain','error'])
def test_withheld_synthesis_recovery_cannot_bypass_upstream_or_missing_review(bundle,blocker):
    def mutate(out,req):
        if 'review_novelty_assessment' not in req['task'] or blocker!='upstream':return
        e=req['packet']['evidence'][0]
        out.update(decision='revise',issues=[],reassessment_requests=[dict(paper_id=e['paper_id'],
            target_ids=[e['target_id']],claim_ids=['c1'],passage_ids=[e['claims'][0]['passage_ids'][0]],reason='Original claim needs source review.')])
    parent,_=run(bundle,Chat(mutate,decisions=['abstain'] if blocker=='abstain' else ['pass'],
        fail='review_novelty_assessment' if blocker=='error' else None))
    assert parent.candidates[0].assessment is None
    chat=Chat()
    with pytest.raises(ValueError,match='synthesis reopening requires'):
        asyncio.run(NoveltyAssessor(chat).run(bundle.generation,bundle.search,bundle.comparisons,
            previous=parent,observations={parent.candidates[0].direction_id:['Correct synthesis prose.']}))
    assert not chat.calls
