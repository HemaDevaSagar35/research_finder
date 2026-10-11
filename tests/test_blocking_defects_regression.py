"""Regressions for the mechanical gates that left every hypothesis pending on real runs.

Each case mirrors a pattern observed in saved artifacts (see docs/implementation_status.md):
reviewer corroboration counted as a citation defect, direction prose edits blocking unchanged
hypotheses, direction-wide critic packets, truncation "repairs" resending the same input,
and provider output that the schema format did not actually constrain.
"""
import asyncio
import json
from copy import deepcopy

import pytest

from extraction.research_extract import _parse_json
from novelty.assessment import NoveltyAssessor, AssessmentSettings
from novelty.assessment_schemas import ScientificEdit
from novelty.assessment_evidence import build_packet, hypothesis_packet, locked_targets, synthesis_packet, validate_draft
from novelty.comparison import NoveltyComparator
from novelty.comparison_aliases import AliasedCalls, alias_map, alias_request, restore_response, restore_text
from novelty.comparison_records import EvidenceRecord, EvidenceReport, EvidenceReview
from novelty.comparison_workflow import corroborating_citations, eligible, repair_blockers, scope_citation_gaps
from novelty.comparison_schemas import NoveltyComparisonResult
from novelty.pipeline import ModelOutputError, drop_null_extras
from reasoning.evidence import PaperStore
from critic.evidence import packet_for
from critic.hypotheses import eligibility
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready, run as compare_run, Chat as CompareChat, evidence_report
from tests.test_novelty_assessment import bundle, Chat as AssessChat, proposal
from tests.test_novelty_result_scope import table_fixture


# --- D1: reviewer corroboration is not a citation defect ---------------------------------

def audited(extra_decision='supported', claim_decision='supported', keep_attached=True):
    passages, record = table_fixture()
    report = EvidenceReport.model_validate(evidence_report(record.model_dump()))
    check = report.claim_checks[0].scope_checks[0]
    extra = passages[3].passage_id  # a real passage of the same paper, not attached to c1
    check.passage_ids = ([passages[1].passage_id] if keep_attached else []) + [extra]
    check.decision = extra_decision
    report.claim_checks[0].decision = claim_decision
    review = EvidenceReview(version=0, trigger='initial', record=record, revision_notes=[], model='fixture', report=report, error=None)
    return passages, review, extra


def test_supported_check_citing_attached_plus_extra_passage_is_corroboration():
    passages, review, extra = audited()
    assert scope_citation_gaps(review.report, review.record) == {}
    assert corroborating_citations(review.report, review.record) == {'c1': [extra]}
    ids, claims = eligible(review)
    assert ids == {'H1'} and [c.claim_id for c in claims] == ['c1']


def test_missing_citation_still_blocks_and_routes_to_citation_repair():
    passages, review, extra = audited(extra_decision='missing_citation', claim_decision='revise')
    assert scope_citation_gaps(review.report, review.record) == {'c1': [extra]}
    assert eligible(review)[0] == set()


def test_supported_check_citing_only_unattached_passages_still_needs_attachment():
    passages, review, extra = audited(keep_attached=False)
    assert scope_citation_gaps(review.report, review.record) == {'c1': [extra]}
    assert eligible(review)[0] == set()


def test_recovery_reuses_saved_audit_without_new_extraction(ready):
    """A saved paper whose audit now clears a target goes straight to COMPARE/REVIEW."""
    complete, _ = compare_run(ready)
    saved = deepcopy(complete)
    paper = saved.candidates[0].papers[0]
    paper.status, paper.comparison, paper.reviews, paper.diagnostic = 'unresolved', None, [], 'blocked by a superseded bookkeeping rule'
    saved = NoveltyComparisonResult.model_validate(saved.model_dump())
    chat = CompareChat()
    root, generation, search = ready
    result = asyncio.run(NoveltyComparator(PaperStore(root), chat).run(generation, search, recovery=saved))
    tasks = [req['task'] for req, _ in chat.calls]
    assert 'extract_comparison_evidence' not in tasks and 'review_comparison_evidence' not in tasks
    assert tasks == ['compare_prior_work', 'review_comparison']
    fresh = result.candidates[0].papers[0]
    assert fresh.status == 'complete' and fresh.evidence_reviews == paper.evidence_reviews


def test_recovery_scope_resolves_to_saved_shortlist_papers(ready):
    from research.recover import comparison_scope
    complete, _ = compare_run(ready)
    state = {'comparison': complete}
    did = complete.candidates[0].direction_id
    assert comparison_scope(state) is None
    assert comparison_scope(state, directions=[did]) == {p.paper_id for p in complete.candidates[0].papers}
    assert comparison_scope(state, papers=['outside-paper']) == {'outside-paper'}
    with pytest.raises(ValueError, match='unknown direction'):
        comparison_scope(state, directions=['dir-999'])
    with pytest.raises(ValueError, match='unknown paper'):
        comparison_scope(state, papers=['invented'])


# --- D2: direction prose edits do not block unchanged, independently judged hypotheses ---

def isolated_result(bundle):
    return asyncio.run(NoveltyAssessor(AssessChat(), settings=AssessmentSettings()).run(bundle.generation, bundle.search, bundle.comparisons))


def test_direction_level_edit_rechecks_direction_not_locked_hypotheses(bundle):
    result = isolated_result(bundle)
    direction = bundle.generation.directions[0]
    c = result.candidates[0]
    c.assessment.refinement.action = 'reframe'
    c.assessment.refinement.scientific_edits = [ScientificEdit(field_path='/scope', proposed_value='Narrowed to the retained subset.', reason='Align prose.')]
    handoff = c.refinement_handoff(direction)
    assert handoff['requires_novelty_recheck_target_ids'] == [direction.direction_id]
    assert all(eligibility(c, direction, h.hypothesis_id) is None for h in direction.proposal.hypotheses)
    # A hypothesis-path edit still rechecks that hypothesis.
    hid = direction.proposal.hypotheses[0].hypothesis_id
    c.assessment.refinement.scientific_edits.append(ScientificEdit(field_path='/hypotheses/0/condition', proposed_value='Changed condition.', reason='Science changed.'))
    assert hid in c.refinement_handoff(direction)['requires_novelty_recheck_target_ids']
    assert eligibility(c, direction, hid) == 'Proposed scientific changes require a fresh novelty check.'
    # Without isolated judgments the synthesis is the only verdict: conservative behaviour stays.
    c.assessment.refinement.scientific_edits = c.assessment.refinement.scientific_edits[:1]
    c.hypothesis_assessments = None
    assert set(c.refinement_handoff(direction)['requires_novelty_recheck_target_ids']) == {direction.direction_id, *c.assessment.refinement.retained_hypothesis_ids}


def test_narrow_may_retain_pending_hypotheses_without_inventing_edits(bundle):
    p = build_packet(bundle, bundle.generation.directions[0].direction_id)
    p['assessment_contract'] = 'isolated_v1'
    data = proposal(p)
    removed, retained = p['candidate']['hypotheses']
    for t in data['targets']:
        if t['target_id'] == removed['hypothesis_id']:
            t.update(finding='UNRESOLVED', evidence=[], meaningful_difference=None)
    p['locked_hypotheses'] = [t for t in data['targets'] if t['target_id'] != p['direction_id']]
    data['refinement'].update(action='narrow', removed_hypothesis_ids=[removed['hypothesis_id']], retained_hypothesis_ids=[retained['hypothesis_id']], scientific_edits=[])
    from novelty.assessment_schemas import AssessmentDraft
    validate_draft(AssessmentDraft.model_validate(data), p)  # retained hypothesis is complete here
    # Retaining an individually pending hypothesis is allowed: it stays pending on its own judgment.
    next(c for c in p['coverage'] if c['target_id'] == retained['hypothesis_id'])['complete'] = False
    validate_draft(AssessmentDraft.model_validate(data), p)
    # An already-studied hypothesis (with valid direct evidence) still cannot be retained silently.
    for e in p['evidence']:
        if e['target_id'] == retained['hypothesis_id']: e['relationship']['coverage'] = 'direct_empirical'
    for t in data['targets']:
        if t['target_id'] == retained['hypothesis_id']: t['finding'] = 'ALREADY_STUDIED'
    p['locked_hypotheses'] = [t for t in data['targets'] if t['target_id'] != p['direction_id']]
    with pytest.raises(ValueError, match='already-studied'):
        validate_draft(AssessmentDraft.model_validate(data), p)


# --- D3: per-hypothesis critic packets carry only that hypothesis' evidence --------------

def test_hypothesis_critic_packet_excludes_sibling_evidence(bundle):
    result = isolated_result(bundle)
    direction = bundle.generation.directions[0]
    full = build_packet(result.inputs, direction.direction_id)
    assert len({e['target_id'] for e in full['evidence']}) > 1
    import critic.evidence as ce
    original = ce.validate_support
    ce.validate_support = lambda support, direction: None
    try:
        for h in direction.proposal.hypotheses:
            packet = packet_for(result, direction, {'paper_artifacts': [], 'pages': []}, hypothesis_id=h.hypothesis_id)
            assert {e['target_id'] for e in packet['evidence']} <= {h.hypothesis_id}
            cited = {pid for e in packet['evidence'] for c in e['claims'] for pid in c['passage_ids']}
            assert cited <= {p['passage_id'] for p in packet['source_passages']}
            assert len(packet['source_passages']) <= len(full['source_passages'])
            assert packet['candidate'] == full['candidate']  # whole direction remains context
        with pytest.raises(ValueError, match='unknown hypothesis'):
            packet_for(result, direction, {'paper_artifacts': [], 'pages': []}, hypothesis_id='nope')
    finally:
        ce.validate_support = original


def test_synthesis_packet_keeps_only_cited_passages(bundle):
    p = build_packet(bundle, bundle.generation.directions[0].direction_id)
    p['source_passages'].append(dict(passage_id='P#p9:uncited00000', page_id='P#p9', text='never cited'))
    s = synthesis_packet(p)
    cited = {pid for e in p['evidence'] for c in e['claims'] for pid in c['passage_ids']}
    assert {x['passage_id'] for x in s['source_passages']} == cited
    assert s['evidence'] == p['evidence'] and s['coverage'] == p['coverage']


# --- D5: provider output that the schema format did not constrain -------------------------

def test_parse_json_tolerates_trailing_duplicate_tail_but_not_cut_off_output():
    good = '{"record": {"claims": []}, "revision_notes": []}'
    assert _parse_json(good + ',"revision_notes":[]}') == json.loads(good)
    assert _parse_json('```json\n' + good + '\n```') == json.loads(good)
    with pytest.raises(ValueError):
        _parse_json('{"record": {"claims": [{"claim_id": "c1", "text": "cut off')


def test_null_valued_unknown_keys_are_dropped_but_content_bearing_ones_fail():
    passages, record = table_fixture()
    data = record.model_dump()
    data['relationships_extra'] = None
    data['claims'][0]['result_note'] = None
    parsed, removed = drop_null_extras(EvidenceRecord, data)
    assert parsed == record and set(removed) == {'/relationships_extra', '/claims/0/result_note'}
    bad = record.model_dump()
    bad['claims'][0]['result_note'] = 'misplaced scientific content'
    with pytest.raises(Exception):
        drop_null_extras(EvidenceRecord, bad)


def test_comparison_requests_use_short_exact_source_labels(ready):
    _, chat = compare_run(ready)
    for req, kw in chat.calls:
        for p in req['payload']['prior_passages']:
            assert p['passage_id'].startswith('src')
        assert 'Source labels' in kw['messages'][0]['content']
    passages, record = table_fixture()
    body = {'payload': {'prior_passages': [p.model_dump() for p in passages], 'table_context_refs': {passages[4].passage_id: [passages[3].passage_id]}},
            'validation_errors': f'claim c1: unknown passage IDs [{passages[0].passage_id!r}]'}
    mapping = alias_map(body)
    aliased = alias_request(body, mapping)
    assert aliased['payload']['table_context_refs'] == {'src4': ['src3']}
    assert 'src0' in aliased['validation_errors'] and passages[0].passage_id not in json.dumps(aliased)
    labelled = EvidenceRecord.model_validate(json.loads(json.dumps(record.model_dump()).replace(record.claims[0].passage_ids[0], mapping[record.claims[0].passage_ids[0]])))
    assert restore_response(labelled, mapping) == record
    assert restore_text('see src1 and src3; src999 is unknown', mapping) == f'see {passages[1].passage_id} and {passages[3].passage_id}; src999 is unknown'


def test_aliased_call_restores_ids_in_model_output_errors():
    passages, record = table_fixture()
    class IO:
        async def _call(self, task, system, schema, body):
            raise ModelOutputError('claim c1: unknown passage IDs [src1]', '{"bad": "src1"}')
    body = {'payload': {'prior_passages': [p.model_dump() for p in passages]}}
    with pytest.raises(ModelOutputError) as info:
        asyncio.run(AliasedCalls(IO())._call('t', 'sys', EvidenceRecord, body))
    assert passages[1].passage_id in str(info.value) and passages[1].passage_id in info.value.raw
