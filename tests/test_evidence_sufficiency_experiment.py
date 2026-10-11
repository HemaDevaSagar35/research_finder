import pytest
from novelty.comparison_records import EvidenceRecord, EvidenceReport
from tools.evidence_sufficiency import SufficiencyReport, apply, removal_review_context


def inputs():
    rel = dict(target_id='h1', intervention=['c1','c2'], comparator=[], conditions=[], outcome=[], conclusion=[], coverage='related', result='not_assessed', inference=None, proposal_matches=[], explanation='Related methods')
    record = EvidenceRecord.model_validate(dict(claims=[dict(claim_id=c, text='Method description', kind='method', passage_ids=['p1']) for c in ['c1','c2']], relationships=[rel]))
    report = EvidenceReport.model_validate(dict(claim_checks=[dict(claim_id=c, decision=d, explanation='Checked', passage_ids=['p1']) for c,d in [('c1','supported'),('c2','unresolved')]], coverage_checks=[dict(target_id='h1', decision='revise', explanation='Check uncertainty', passage_ids=['p1'])], summary='Needs review'))
    rel = {**rel, 'intervention':['c1']}
    proposal = SufficiencyReport.model_validate(dict(targets=[dict(target_id='h1', decision='sufficient', explanation='Context only', excluded_claims=[dict(claim_id='c2',reason='Nonessential context')], relationship=rel)]))
    return record, report, proposal


def test_mapping_proposal_preserves_claims_and_does_not_approve_review():
    record, report, proposal = inputs()
    result = apply(record, report, proposal)
    assert result.claims == record.claims
    assert result.relationships[0].refs() == {'c1'}
    assert report.coverage_checks[0].decision == 'revise'


def test_unsupported_claim_cannot_be_kept_in_sufficient_mapping():
    record, report, proposal = inputs()
    proposal.targets[0].relationship.intervention.append('c2')
    proposal.targets[0].excluded_claims = []
    with pytest.raises(ValueError, match='unsupported'): apply(record, report, proposal)


def test_removals_computed_without_model_bookkeeping():
    record, report, proposal = inputs()
    proposal.targets[0].excluded_claims=[]
    result = apply(record, report, proposal)
    context = removal_review_context(record, result, proposal)
    assert context['targets'][0]['removed_claim_ids'] == ['c2']


def test_insufficient_cannot_silently_change_mapping():
    record, report, proposal = inputs()
    proposal.targets[0].decision='insufficient'
    assert apply(record, report, proposal) == record
    proposal.targets[0].relationship = None
    assert apply(record, report, proposal) == record


def test_missing_target_rejected():
    record, report, proposal = inputs()
    proposal.targets=[]
    with pytest.raises(ValueError, match='exactly once'): apply(record, report, proposal)
