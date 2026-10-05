"""Shared hypothesis/test consistency audit used at generation and critique."""
from typing import Literal
from pydantic import Field, model_validator
from directions.schemas import Strict, Text, CorrectnessResponse


class TestLinkCheck(Strict):
    hypothesis_id: Text
    experiment_id: Text
    prediction: Text
    equality_case: Text
    decision: Literal['consistent', 'contradiction', 'uncertain']
    reasoning: Text


class LinkedCorrectnessResponse(CorrectnessResponse):
    test_link_checks: list[TestLinkCheck] = Field(min_length=1)

    @model_validator(mode='after')
    def links_agree(self):
        if self.decision == 'pass' and any(c.decision != 'consistent' for c in self.test_link_checks):
            raise ValueError('passing correctness review cannot hide contradictory or uncertain test links')
        return self

    def basic(self):
        return CorrectnessResponse.model_validate(self.model_dump(exclude={'test_link_checks'}))


def validate_links(checks, proposal):
    expected = sorted((h, e.experiment_id) for e in proposal.experiments for h in e.hypothesis_ids)
    if sorted((c.hypothesis_id, c.experiment_id) for c in checks) != expected:
        raise ValueError('review must audit every hypothesis-experiment link exactly once')


INSTRUCTION = '''Return test_link_checks for every (hypothesis_id, experiment_id) link.
Identify the prediction, equality/null case, and whether ALL linked outcome clauses
agree. Equal changes falsify a strictly positive difference-of-changes prediction;
a shared offset is not missing evidence. Distinguish interaction from main effect.
Source-scoped results must not borrow model/workload conditions from another experiment.
An O(sigma^2) bound is not proof of monotonic actual error growth. Speculative mechanisms
may remain hypotheses. Do not require detailed experimental protocols or already-proven
predictions. Report concrete contradictions as located correctness issues.'''
