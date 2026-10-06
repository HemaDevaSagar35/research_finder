"""The validation audit must detect broken evidence and paper-role handoffs."""
import asyncio
import json

import pytest

from llm_client import ChatResult
from opportunities.miner import OpportunityMiner
from reasoning.evidence import PaperStore
from tools.validate_opportunity_boundaries import audit, controls


def test_boundary_controls_have_valid_upstream_lineage(tmp_path):
    rows = controls(tmp_path)
    assert len(rows) == 6
    assert sum(row[-1] == 'accept' for row in rows) == 3
    for _, corpus, land, reasoning, candidate, _ in rows:
        m = OpportunityMiner(PaperStore(corpus))
        _, _, sources, evidence = m._inputs(land, reasoning)
        assert set(candidate.source_ids) == set(sources)
        assert set(candidate.evidence_ids) == set(evidence)
        assert len(sources) == 2


def test_handoff_audit_rejects_changed_pages_and_wrong_roles(tmp_path):
    name, corpus, land, reasoning, candidate, _ = controls(tmp_path)[0]

    async def chat(**kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])['payload']
        return ChatResult(text=json.dumps({
            'candidate_id': name, 'decision': 'accept', 'notes': 'Offline plumbing only.',
            'factual_status': 'supported', 'required_corrections': [],
            'page_ids': [p['page_id'] for p in payload['pages']],
            'paper_assessments': [{'paper_id': pid, 'role': 'supporting', 'rationale': 'Fixture.',
                'evidence_ids': [e['evidence_id'] for e in payload['evidence'] if e['paper_id'] == pid],
                'page_ids': [p['page_id'] for p in payload['pages'] if p['paper_id'] == pid]}
                for pid in land.paper_ids]}), finish_reason='stop', model='stub')

    m = OpportunityMiner(PaperStore(corpus), chat)
    _, _, sources, evidence = m._inputs(land, reasoning)
    op = asyncio.run(m._review(name, candidate, sources, evidence))
    assert audit(op, PaperStore(corpus), sources, evidence) == {'evidence_references': 2, 'page_references': 2}
    incorrect = op.model_copy(deep=True)
    incorrect.supporting_paper_ids = incorrect.supporting_paper_ids[:1]
    with pytest.raises(AssertionError):
        audit(incorrect, PaperStore(corpus), sources, evidence)
    page = corpus / land.paper_ids[0] / '01.md'
    page.write_text(page.read_text() + '\nChanged after review.\n')
    with pytest.raises(AssertionError):
        audit(op, PaperStore(corpus), sources, evidence)
