"""Correctness gate behavior, audit integrity and bounded review/revision calls."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from directions.generator import DirectionGenerator, Settings
from directions.schemas import GenerationResult
from llm_client import ChatResult
from reasoning.evidence import PaperStore
from tests.test_direction_generator import inputs, Chat, response, link_checks


def issue(**changes):
    return {'category': 'consistency', 'field_path': '/hypotheses/0/expected_effect',
            'explanation': 'Prediction and outcome disagree.',
            'required_change': 'Make the prediction and outcome agree.', 'source_spans': [], **changes}


def report(decision='pass', issues=None):
    return {'decision': decision, 'summary': 'Concrete correctness review.',
            'issues': issues if issues is not None else ([issue()] if decision == 'revise' else [])}


class Reviewer:
    def __init__(self, reports=None, finish='stop'):
        self.reports = reports or [report()]
        self.calls = []
        self.finish = finish

    async def __call__(self, **kw):
        self.calls.append(kw)
        value = self.reports[min(len(self.calls)-1, len(self.reports)-1)]
        if isinstance(value, Exception):
            raise value
        value={**value,'test_link_checks':value.get('test_link_checks',link_checks(json.loads(kw['messages'][1]['content'])['candidate']))}
        return ChatResult(json.dumps(value), self.finish, 'independent-model')


def run_gate(inputs, reviewer, chat=None, **settings):
    root, land, reasoning, mining = inputs
    generator = DirectionGenerator(PaperStore(root), chat or Chat(), review_chat=reviewer,
        model='generator-model', review_model='reviewer-model', settings=Settings(**settings))
    return asyncio.run(generator.run(land, reasoning, mining))


def test_pass_requires_fresh_independent_review_and_binds_exact_output(inputs):
    reviewer, chat = Reviewer(), Chat()
    result = run_gate(inputs, reviewer, chat)
    assert len(chat.calls) == len(reviewer.calls) == 1
    call = reviewer.calls[0]
    assert call['model'] == 'reviewer-model'
    assert len(call['messages']) == 2
    assert all(m['role'] != 'assistant' for m in call['messages'])
    request = json.loads(call['messages'][1]['content'])
    original = json.loads(chat.calls[0]['messages'][1]['content'])
    assert request['payload'] == original['payload']
    assert request['task'] == 'review_direction'
    assert 'correctness_issues' not in request
    d, review = result.directions[0], result.reviews[0]
    assert d.correctness_review == 'passed' and d.correctness_review_id == review.review_id
    assert review.proposal == d.proposal and review.model == 'independent-model'
    assert d.literature_novelty == d.scientific_review == 'not_assessed'
    assert GenerationResult.model_validate_json(result.model_dump_json()) == result


def test_one_revision_is_reviewed_without_previous_verdict_in_judge_context(inputs):
    reviewer = Reviewer([report('revise'), report()])
    captured = []
    async def generate(**kw):
        request = json.loads(kw['messages'][1]['content'])
        captured.append(request)
        out = response(request['payload'])
        if request['task'] == 'revise_direction':
            assert request['correctness_issues']['issues'] == [issue()]
            out['direction']['hypotheses'][0]['expected_effect'] = 'Corrected prediction.'
        return ChatResult(json.dumps(out), 'stop', 'generator-model')
    result = run_gate(inputs, reviewer, generate)
    assert len(result.directions) == 1
    assert result.calls['generation'] == result.calls['revision'] == 1
    assert result.calls['review'] == 2 and result.calls['total'] == 4
    assert [r.report.decision for r in result.reviews] == ['revise', 'pass']
    assert result.reviews[0].proposal != result.reviews[1].proposal
    second = json.loads(reviewer.calls[1]['messages'][1]['content'])
    assert 'correctness_issues' not in second and len(reviewer.calls[1]['messages']) == 2
    assert second['candidate']['hypotheses'][0]['expected_effect'] == 'Corrected prediction.'


def test_unresolved_revision_is_withheld_and_both_drafts_remain_auditable(inputs):
    result = run_gate(inputs, Reviewer([report('revise')]))
    assert not result.directions
    assert result.diagnostics[0].reason == 'correctness_unresolved'
    assert len(result.reviews) == 2 and result.calls['total'] == 4
    assert result.coverage['opportunities_without_direction'] == 1


def test_abstaining_judge_does_not_trigger_revision(inputs):
    result = run_gate(inputs, Reviewer([report('abstain')]))
    assert not result.directions and result.diagnostics[0].reason == 'review_abstained'
    assert result.calls['total'] == 2 and len(result.reviews) == 1


@pytest.mark.parametrize('bad', [
    {'decision': 'pass', 'summary': 'Pass with issues is invalid.', 'issues': [issue()]},
    report('revise', []),
    report('revise', [issue(field_path='/hypotheses/999/expected_effect')]),
    report('revise', [issue(field_path='/hypotheses/-1/expected_effect')]),
    report('revise', [issue(field_path='/title/no_such_child')]),
    report('revise', [issue(category='attribution')]),
    report('revise', [issue(source_spans=[{'page_id': 'invented#p1', 'quote': 'invented text'}])]),
])
def test_malformed_judge_output_cannot_promote_or_revise(inputs, bad):
    result = run_gate(inputs, Reviewer([bad]))
    assert not result.directions and result.diagnostics[0].reason == 'invalid_review'
    assert len(result.reviews) == 1 and result.reviews[0].report is None
    assert result.reviews[0].error and 'revision' not in result.calls


@pytest.mark.parametrize('valid_quote', [False, True])
def test_judge_quote_checked_against_actual_page(inputs, valid_quote):
    op = inputs[3].opportunities[0]
    src = op.review_sources[0]
    text = (inputs[0]/src.paper_id/'01.md').read_text().splitlines()[-1]
    quote = text if valid_quote else 'An invented statement not present in this page.'
    bad = issue(category='grounding', field_path='/rationale/0/statement',
                source_spans=[{'page_id': f'{src.paper_id}#p{src.page}', 'quote': quote}])
    result = run_gate(inputs, Reviewer([report('revise', [bad]), report()]))
    assert bool(result.directions) == valid_quote
    assert result.calls['total'] == (4 if valid_quote else 2)


@pytest.mark.parametrize('mode,expected', [('truncated','invalid_review'), ('transport','call_failed'), ('budget','call_budget')])
def test_unavailable_review_fails_closed(inputs, mode, expected):
    reviewer = Reviewer([RuntimeError('Transient network failure')]) if mode == 'transport' else Reviewer(finish='length' if mode == 'truncated' else 'stop')
    result = run_gate(inputs, reviewer, max_calls=1 if mode == 'budget' else 40)
    assert not result.directions and result.diagnostics[0].reason == expected
    assert result.reviews[0].report is None and result.reviews[0].proposal
    assert result.calls['total'] == (1 if mode == 'budget' else 2)


def test_revision_cannot_bypass_existing_evidence_checks(inputs):
    async def generate(**kw):
        req = json.loads(kw['messages'][1]['content'])
        out = response(req['payload'])
        if req['task'] == 'revise_direction':
            out['direction']['rationale'][0]['evidence_ids'] = ['invented']
        return ChatResult(json.dumps(out), 'stop', 'stub')
    result = run_gate(inputs, Reviewer([report('revise')]), generate, repair_rounds=0)
    assert not result.directions and result.diagnostics[0].reason == 'invalid_generation'
    assert len(result.reviews) == 1 and result.calls['total'] == 3


@pytest.mark.parametrize('tamper', ['proposal', 'missing_review', 'failed_review', 'wrong_opportunity', 'version'])
def test_persisted_handoff_rejects_unreviewed_or_changed_proposals(inputs, tamper):
    saved = run_gate(inputs, Reviewer()).model_dump()
    if tamper == 'proposal': saved['directions'][0]['proposal']['title'] = 'Unreviewed change'
    elif tamper == 'missing_review': saved['reviews'] = []
    elif tamper == 'failed_review': saved['reviews'][0]['report'] = report('revise')
    elif tamper == 'wrong_opportunity': saved['reviews'][0]['opportunity_id'] = 'other'
    else: saved['schema_version'] = 'directions_v2'
    with pytest.raises(ValidationError): GenerationResult.model_validate(saved)
