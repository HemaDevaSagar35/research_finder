"""Evidence fidelity, reviewed handoff and full-corpus retrieval boundaries."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from directions.generator import DirectionGenerator
from llm_client import ChatResult
from novelty.pipeline import NoveltySearcher, Settings
from novelty.schemas import NoveltySearchResult
from reasoning.evidence import PaperStore
from research.retrieval import MultiQueryRetriever, PaperHit, RecordHit
from tests.test_direction_generator import inputs, Chat as GeneratorChat


@pytest.fixture
def prepared(inputs):
    root, land, reason, mining = inputs
    generation = asyncio.run(DirectionGenerator(PaperStore(root), GeneratorChat()).run(land, reason, mining))
    # A paper outside the seed set must be eligible for novelty retrieval.
    folder = root/'outside-paper'
    folder.mkdir()
    (folder/'paper.json').write_text((root/land.paper_ids[0]/'paper.json').read_text())
    return root, generation


class Backend:
    def __init__(self, paper='outside-paper', fail=False):
        self.paper, self.fail, self.calls = paper, fail, []
        self.active = self.peak = 0

    def search(self, query, *, record_k, filters):
        self.calls.append((query, record_k, filters))
        self.active += 1
        self.peak = max(self.peak, self.active)
        time.sleep(.01)
        self.active -= 1
        if self.fail:
            raise RuntimeError('backend unavailable')
        return [] if self.paper is None else [PaperHit(paper_id=self.paper, score=1,
            records=[RecordHit(record_id='r1', type='claim', text='Relevant expert caching comparison.', score=1)])]


def draft(payload):
    def facet(text, path):
        return {'text': text, 'basis': 'candidate_proposal', 'proposal_paths': [path],
                'evidence_ids': [], 'source_spans': []}
    pairs = [(payload['direction_id'], 'direction', '/research_direction')]
    pairs += [(h['hypothesis_id'], 'hypothesis', f'/hypotheses/{i}/expected_effect')
              for i, h in enumerate(payload['candidate']['hypotheses'])]
    targets = []
    for tid, level, path in pairs:
        targets.append({'target_id': tid, 'level': level,
            'problem': [facet('Expert transfer costs', '/research_direction')],
            'intervention': [facet('Compression with caching', path)],
            'decision_signal': [], 'mechanism': [facet('Routing concentration may improve reuse', '/proposed_mechanism')],
            'regime': [facet('Model M workload W', '/scope')], 'comparison': [],
            'expected_effect': [facet('Lower misses or latency', path)],
            'queries': ['compression expert cache locality', 'routing concentration memory latency']})
    return {'signature': {'targets': targets}, 'abstention_reason': None}


def report(decision='pass'):
    return {'decision': decision, 'summary': 'Fixture review.', 'issues': [] if decision != 'revise' else [
        {'category': 'consistency', 'field_path': '/targets/0/expected_effect/0/text',
         'explanation': 'Prediction changed.', 'required_change': 'Preserve the candidate prediction.', 'source_spans': []}]}


class Chat:
    def __init__(self, mutate=None, decisions=None, fail_task=None):
        self.mutate, self.decisions, self.fail_task = mutate, decisions or ['pass'], fail_task
        self.calls, self.review_calls = [], 0
        self.active = self.peak = 0

    async def __call__(self, **kw):
        req = json.loads(kw['messages'][1]['content'])
        # Provider JSON-object mode must not depend on incidental words in paper text.
        assert kw['response_format'] == {'type': 'json_object'}
        assert 'json' in kw['messages'][0]['content'].lower()
        self.calls.append((req, kw))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(.01)
        self.active -= 1
        task = req['task']
        if task == self.fail_task:
            raise RuntimeError('injected provider failure')
        if task == 'review_signature':
            out = report(self.decisions[min(self.review_calls, len(self.decisions)-1)])
            self.review_calls += 1
        elif task == 'rerank':
            out = {'papers': [{'paper_id': c['matches']['paper_id'], 'why_relevant': 'Potential matching mechanism.'}
                              for c in req['candidates'][:req['keep']]]}
        else:
            out = draft(req['payload'])
            if task == 'revise_signature':
                out['signature']['targets'][0]['expected_effect'][0]['text'] = 'Corrected lower misses prediction'
        if self.mutate:
            self.mutate(out, req)
        return ChatResult(json.dumps(out), 'stop', 'stub')


def run(prepared, chat=None, backend=None, extras=None, **settings):
    root, generation = prepared
    chat, backend = chat or Chat(), backend or Backend()
    async def execute():
        async with MultiQueryRetriever(backend, concurrency=2) as retriever:
            return await NoveltySearcher(PaperStore(root), retriever, chat, model='generation-model',
                review_model='review-model', settings=Settings(**settings)).run(generation, extra_pages=extras)
    return asyncio.run(execute()), chat, backend


def test_full_context_and_full_corpus_search_with_per_hypothesis_targets(prepared):
    result, chat, backend = run(prepared)
    candidate = result.candidates[0]
    assert candidate.signature and not candidate.diagnostic
    assert len(candidate.searches) == 3
    assert all(s.ranked[0].paper_id == 'outside-paper' and s.status == 'complete' for s in candidate.searches)
    assert all(not filters.types and filters.year is None for _, _, filters in backend.calls)
    assert result.calls['total'] == 5  # signature, review, 3 target rerankings
    assert backend.peak == 2 and chat.peak >= 2
    first = chat.calls[0][0]['payload']
    assert first['paper_artifacts'][0]['paper']
    page = first['pages'][0]
    assert page['text'] == (prepared[0]/page['paper_id']/'01.md').read_text()
    assert first['opportunity'] == prepared[1].directions[0].opportunity.model_dump()
    assert candidate.context_pages == prepared[1].directions[0].context_pages
    assert result.novelty == 'not_assessed'
    assert NoveltySearchResult.model_validate_json(result.model_dump_json()) == result


def test_review_revision_fresh_context_and_same_budgets(prepared):
    result, chat, _ = run(prepared, Chat(decisions=['revise', 'pass']))
    c = result.candidates[0]
    assert [r.report.decision for r in c.reviews] == ['revise', 'pass']
    assert result.calls['total'] == 7
    requests = [(req, kw) for req, kw in chat.calls if req['task'] == 'review_signature']
    assert all(len(kw['messages']) == 2 and kw['model'] == 'review-model' for req, kw in requests)
    assert all('corrections' not in req for req, _ in requests)
    assert c.signature != c.reviews[0].signature and c.signature == c.reviews[1].signature


@pytest.mark.parametrize('decision', ['revise', 'abstain'])
def test_unresolved_signature_never_searches(prepared, decision):
    result, _, backend = run(prepared, Chat(decisions=[decision]))
    assert result.candidates[0].signature is None and result.candidates[0].diagnostic
    assert not backend.calls
    assert result.calls['total'] == (4 if decision == 'revise' else 2)


@pytest.mark.parametrize('mutation', ['drop_hypothesis', 'wrong_target', 'wrong_level', 'pointer', 'evidence', 'fake_quote', 'queries'])
def test_invalid_signature_stops_before_review_and_retrieval(prepared, mutation):
    def mutate(out, req):
        if req['task'] not in ('create_signature', 'repair_signature'): return
        targets = out['signature']['targets']
        f = targets[0]['problem'][0]
        if mutation == 'drop_hypothesis': targets.pop()
        elif mutation == 'wrong_target': targets[0]['target_id'] = 'invented'
        elif mutation == 'wrong_level': targets[0]['level'] = 'hypothesis'
        elif mutation == 'pointer': f['proposal_paths'] = ['/hypotheses/999/mechanism']
        elif mutation == 'evidence': f['evidence_ids'] = ['invented']
        elif mutation == 'fake_quote': f['source_spans'] = [{'page_id': req['payload']['pages'][0]['page_id'], 'quote': 'Not in original page.'}]
        else: targets[0]['queries'] *= 2
    result, chat, backend = run(prepared, Chat(mutate))
    assert result.candidates[0].diagnostic and result.candidates[0].signature is None
    assert len(chat.calls) == 2 and not backend.calls


@pytest.mark.parametrize('change', ['paper', 'page', 'missing_page'])
def test_stale_or_missing_context_stops_before_calls(prepared, change):
    root, generation = prepared
    pid = generation.directions[0].context_pages[0].paper_id
    path = root/pid/('paper.json' if change == 'paper' else '01.md')
    if change == 'missing_page': path.unlink()
    else: path.write_text(path.read_text()+'\n')
    result, chat, backend = run(prepared)
    assert result.candidates[0].diagnostic and not chat.calls and not backend.calls


def test_extra_pages_are_loaded_and_audited_without_rewriting_source_manifest(prepared):
    root, generation = prepared
    pid = generation.directions[0].context_pages[0].paper_id
    (root/pid/'02.md').write_text('Additional available source page.')
    result, chat, _ = run(prepared, extras={pid: [2]})
    assert any(p.page == 2 for p in result.candidates[0].context_pages)
    assert any(p['text'] == 'Additional available source page.' for p in chat.calls[0][0]['payload']['pages'])
    assert all(p.page == 1 for p in generation.directions[0].context_pages)


@pytest.mark.parametrize('mode', ['missing', 'empty', 'failure', 'bad_rank'])
def test_retrieval_failures_and_empty_results_are_not_novelty_verdicts(prepared, mode):
    backend = Backend(paper='missing' if mode == 'missing' else None if mode == 'empty' else 'outside-paper', fail=mode == 'failure')
    def mutate(out, req):
        if mode == 'bad_rank' and req['task'] == 'rerank': out['papers'][0]['paper_id'] = 'invented'
    result, _, _ = run(prepared, Chat(mutate), backend)
    searches = result.candidates[0].searches
    expected = {'missing': 'partial', 'empty': 'complete', 'failure': 'failed', 'bad_rank': 'failed'}[mode]
    assert all(s.status == expected and not s.ranked and s.novelty == 'not_assessed' for s in searches)
    if mode == 'empty': assert result.calls['total'] == 2


@pytest.mark.parametrize('limit', [0, 1, 2])
def test_attempt_budget_cannot_be_bypassed(prepared, limit):
    result, chat, _ = run(prepared, max_calls=limit)
    assert result.calls['total'] == len(chat.calls) == limit
    if limit < 2:
        assert result.candidates[0].signature is None
    else:
        assert all(s.status == 'failed' for s in result.candidates[0].searches)


def test_complete_prompt_budget_and_failed_reviewer_block_search(prepared):
    result, chat, backend = run(prepared, max_input_tokens=1)
    assert not chat.calls and not backend.calls and result.candidates[0].diagnostic
    result, _, backend = run(prepared, Chat(fail_task='review_signature'))
    assert not backend.calls and result.candidates[0].reviews[0].error


@pytest.mark.parametrize('tamper', ['signature', 'review', 'search_coverage', 'ranking', 'filters'])
def test_persisted_contract_rejects_unreviewed_or_fabricated_handoff(prepared, tamper):
    result, _, _ = run(prepared)
    data = result.model_dump()
    c = data['candidates'][0]
    if tamper == 'signature': c['signature']['targets'][0]['queries'] = ['changed']
    elif tamper == 'review': c['reviews'] = []
    elif tamper == 'search_coverage': c['searches'].pop()
    elif tamper == 'ranking': c['searches'][0]['ranked'][0]['paper_id'] = 'invented'
    else: c['searches'][0]['retrieval']['filters']['year'] = 2026
    with pytest.raises(ValidationError): NoveltySearchResult.model_validate(data)


def test_source_fact_requires_real_page_quote_and_paper_ownership(prepared):
    def mutate(out, req):
        if req['task'] != 'create_signature': return
        payload = req['payload']
        ev = payload['opportunity']['evidence'][0]
        page = next(p for p in payload['pages'] if p['paper_id'] == ev['paper_id'])
        out['signature']['targets'][0]['problem'][0] = {
            'text': 'Source-supported fixture context', 'basis': 'source_fact', 'proposal_paths': [],
            'evidence_ids': [ev['evidence_id']],
            'source_spans': [{'page_id': page['page_id'], 'quote': page['text']}]
        }
    result, _, _ = run(prepared, Chat(mutate))
    assert result.candidates[0].signature
    def wrong_paper(out, req):
        mutate(out, req)
        if req['task'] in ('create_signature', 'repair_signature'):
            # Rebuild the valid factual entry, then quote the wrong paper.
            mutate(out, {**req, 'task': 'create_signature'})
            f = out['signature']['targets'][0]['problem'][0]
            other = next(p for p in req['payload']['pages'] if p['page_id'] != f['source_spans'][0]['page_id'])
            f['source_spans'] = [{'page_id': other['page_id'], 'quote': other['text']}]
    result, _, backend = run(prepared, Chat(wrong_paper))
    assert not result.candidates[0].signature and not backend.calls


@pytest.mark.parametrize('mode', ['fake_quote', 'bad_pointer', 'pass_with_issue', 'truncated'])
def test_invalid_signature_review_cannot_advance(prepared, mode):
    def mutate(out, req):
        if req['task'] != 'review_signature': return
        out.update(report('revise'))
        if mode == 'fake_quote':
            out['issues'][0].update(category='grounding', source_spans=[{'page_id': req['payload']['pages'][0]['page_id'], 'quote': 'fabricated quotation'}])
        elif mode == 'bad_pointer': out['issues'][0]['field_path'] = '/targets/999/problem'
        elif mode == 'pass_with_issue': out['decision'] = 'pass'
    chat = Chat(mutate)
    if mode == 'truncated':
        original = chat
        async def chat(**kw):
            response = await original(**kw)
            if json.loads(kw['messages'][1]['content'])['task'] == 'review_signature':
                return ChatResult(response.text, 'length', 'stub')
            return response
    result, _, backend = run(prepared, chat)
    c = result.candidates[0]
    assert not c.signature and not backend.calls and c.reviews[0].error


def test_tampered_retrieval_queries_are_not_bound_to_approved_signature(prepared):
    result, _, _ = run(prepared)
    saved = result.model_dump()
    saved['candidates'][0]['searches'][0]['retrieval']['queries'] = ['unreviewed replacement query']
    with pytest.raises(ValidationError): NoveltySearchResult.model_validate(saved)


def test_payload_wrapper_pointers_normalize_before_review_without_changing_content(prepared):
    def mutate(out, req):
        if req['task'] != 'create_signature': return
        for target in out['signature']['targets']:
            for key in ('problem','intervention','decision_signal','mechanism','regime','comparison','expected_effect'):
                for facet in target[key]:
                    facet['proposal_paths'] = ['/candidate'+p for p in facet['proposal_paths']]
    result, chat, _ = run(prepared, Chat(mutate))
    c = result.candidates[0]
    assert c.signature and c.signature == c.reviews[-1].signature
    target = c.signature.targets[0]
    assert target.problem[0].proposal_paths == ['/research_direction']
    assert target.problem[0].text == 'Expert transfer costs'
    review = next(req for req, _ in chat.calls if req['task'] == 'review_signature')
    assert review['signature'] == c.signature.model_dump()


def test_format_repair_rechecks_provenance_before_independent_review(prepared):
    def mutate(out, req):
        if req['task'] == 'create_signature':
            f = out['signature']['targets'][0]['mechanism'][0]
            f.update(basis='unknown', proposal_paths=[], evidence_ids=[], source_spans=[])
        if req['task'] == 'repair_signature':
            assert 'every facet needs provenance' in req['validation_errors']
            assert req['invalid_output']
    result, chat, _ = run(prepared, Chat(mutate))
    assert result.candidates[0].signature
    assert result.calls['repair_signature'] == 1
    assert [r['task'] for r, _ in chat.calls[:3]] == ['create_signature','repair_signature','review_signature']


def test_no_signature_repair_on_provider_failure_or_abstention(prepared):
    result, _, backend = run(prepared, Chat(fail_task='create_signature'))
    assert result.calls['total'] == 1 and not backend.calls
    def abstain(out, req):
        if req['task'] == 'create_signature':
            out.update(signature=None, abstention_reason='Insufficient candidate content.')
    result, _, backend = run(prepared, Chat(abstain))
    assert result.calls['total'] == 1 and not backend.calls


@pytest.mark.parametrize('defect', [None, 'quote', 'page', 'evidence', 'missing_quote'])
def test_direct_page_source_facts_keep_quote_and_reference_validation(prepared, defect):
    def mutate(out, req):
        if req['task'] not in ('create_signature', 'repair_signature'): return
        page = req['payload']['pages'][0]
        f = dict(text='A directly quoted source fact.', basis='source_fact', proposal_paths=[],
            evidence_ids=[], source_spans=[dict(page_id=page['page_id'], quote=page['text'])])
        if defect == 'quote': f['source_spans'][0]['quote'] = 'Invented unsupported quotation.'
        if defect == 'page': f['source_spans'][0]['page_id'] = 'unknown#p1'
        if defect == 'evidence': f['evidence_ids'] = ['unrelated-invented-id']
        if defect == 'missing_quote': f['source_spans'] = []
        out['signature']['targets'][0]['problem'][0] = f
    result, chat, backend = run(prepared, Chat(mutate))
    assert bool(result.candidates[0].signature) == (defect is None)
    assert bool(backend.calls) == (defect is None)
    if defect is None:
        assert any(req['task'] == 'review_signature' for req, _ in chat.calls)


def test_signature_recovery_revises_saved_draft_then_reviews_before_search(prepared):
    from opportunities.miner import digest
    parent, _, _ = run(prepared, Chat(decisions=['revise']))
    chat, backend = Chat(), Backend()
    async def execute():
        async with MultiQueryRetriever(backend) as retriever:
            return await NoveltySearcher(PaperStore(prepared[0]), retriever, chat).run(
                prepared[1], signature_recovery=parent)
    result = asyncio.run(execute())
    first = chat.calls[0][0]
    assert first['task'] == 'revise_signature'
    assert first['signature'] == parent.candidates[0].reviews[-1].signature.model_dump()
    assert first['corrections'] == parent.candidates[0].reviews[-1].report.model_dump()
    assert chat.calls[1][0]['task'] == 'review_signature'
    assert result.candidates[0].signature and backend.calls
    assert result.run['signature_recovery_parent_sha256'] == digest(parent.model_dump())
    assert NoveltySearchResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize('defect', ['generation', 'context', 'accepted', 'review_rejected'])
def test_signature_recovery_cannot_bypass_input_or_review_gates(prepared, defect):
    parent, _, _ = run(prepared, Chat(decisions=['pass'] if defect == 'accepted' else ['revise']))
    if defect == 'generation': parent.directions_ref['sha256'] = 'changed'
    if defect == 'context': parent.candidates[0].context_pages[0].sha256 = 'changed'
    chat, backend = Chat(decisions=['revise'] if defect == 'review_rejected' else ['pass']), Backend()
    async def execute():
        async with MultiQueryRetriever(backend) as retriever:
            return await NoveltySearcher(PaperStore(prepared[0]), retriever, chat).run(
                prepared[1], signature_recovery=parent)
    if defect in ('generation', 'accepted'):
        with pytest.raises(ValueError): asyncio.run(execute())
    else:
        result = asyncio.run(execute())
        assert result.candidates[0].signature is None
    assert not backend.calls
    if defect != 'review_rejected': assert not chat.calls
