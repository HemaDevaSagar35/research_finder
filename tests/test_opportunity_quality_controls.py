"""Offline plumbing for quality controls; these do not assess LLM judgments."""
import asyncio
import json

import pytest

from llm_client import ChatResult
from opportunities.miner import OpportunityMiner
from opportunities.schemas import MiningResult
from reasoning.evidence import PaperStore
from tools.opportunity_validation_cases import CASES, fixture


@pytest.mark.parametrize('category', list(CASES))
def test_category_control_keeps_both_original_papers_and_pages(tmp_path, category):
    land, reasoning, candidate = fixture(tmp_path, category)
    review_requests = []

    async def chat(**kwargs):
        message = json.loads(kwargs['messages'][1]['content'])
        payload = message['payload']
        if message['task'] == 'proposal':
            response = {'candidates': [candidate.model_dump()]}
        else:
            review_requests.append(payload)
            response = {'candidate_id': payload['candidate_id'], 'decision': 'accept',
                        'notes': 'Stub verdict for contract testing, not scientific review.',
                        'page_ids': [page['page_id'] for page in payload['pages']]}
            response.update(factual_status='supported', required_corrections=[],
                paper_assessments=[{'paper_id': pid, 'role': 'supporting', 'rationale': 'Supported in stub.',
                    'evidence_ids': [e['evidence_id'] for e in payload['evidence'] if e['paper_id']==pid],
                    'page_ids': [p['page_id'] for p in payload['pages'] if p['paper_id']==pid]}
                    for pid in sorted({e['paper_id'] for e in payload['evidence']})])
        return ChatResult(text=json.dumps(response), finish_reason='stop', model='offline-stub')

    result = asyncio.run(OpportunityMiner(PaperStore(tmp_path), chat).run(land, reasoning))
    result = MiningResult.model_validate_json(result.model_dump_json())
    assert not result.diagnostics
    accepted = result.opportunities[0]
    assert accepted.candidate.category == category
    assert accepted.support == 'multiple_papers'
    assert set(accepted.paper_ids) == set(land.paper_ids)
    assert len(accepted.evidence) == len(accepted.review_sources) == 2
    assert len(review_requests) == 1
    # Every original page, including both conflicting reports, reaches review.
    for page in review_requests[0]['pages']:
        assert page['text'] == (tmp_path / page['paper_id'] / '01.md').read_text()
    for evidence in accepted.evidence:
        assert evidence.value_path == '/key_results/0'
        assert evidence.source_value in next(page['text'] for page in review_requests[0]['pages']
                                            if page['paper_id'] == evidence.paper_id)


def test_review_keeps_counter_context_even_when_candidate_selects_one_paper(tmp_path):
    land, reasoning, candidate = fixture(tmp_path, 'contradiction')
    first, second = reasoning.observations
    first.review_sources.extend(second.review_sources)
    candidate = candidate.model_copy(update={
        'source_ids': ['observation:' + first.observation_id],
        'evidence_ids': ['observation:' + first.observation_id + '/e1'],
    })
    captured = []

    async def chat(**kwargs):
        data = json.loads(kwargs['messages'][1]['content'])['payload']
        captured.append(data)
        return ChatResult(text=json.dumps({'candidate_id': data['candidate_id'], 'decision': 'reject',
            'notes': 'Stub: inspect both reports before deciding.', 'page_ids': [],
            'factual_status':'uncertain', 'required_corrections':[], 'paper_assessments':[]}),
            finish_reason='stop', model='offline-stub')

    miner = OpportunityMiner(PaperStore(tmp_path), chat)
    _, _, sources, evidence = miner._inputs(land, reasoning)
    from opportunities.miner import MiningFailure
    with pytest.raises(MiningFailure) as exc:
        asyncio.run(miner._review('counter-context', candidate, sources, evidence))
    assert exc.value.reason == 'rejected'
    assert {page['paper_id'] for page in captured[0]['pages']} == set(land.paper_ids)
    assert len(captured[0]['evidence']) == 1
