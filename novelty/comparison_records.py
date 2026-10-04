"""Evidence-first section-12 records. Model review is a finding, not ground truth."""
from typing import Literal
from pydantic import Field, model_validator
from directions.schemas import Strict, Text


class PriorClaim(Strict):
    claim_id: Text
    text: Text = Field(description='One relevant prior fact, including its material qualifications. Every clause needs source support.')
    kind: Literal['method', 'evaluation', 'result', 'theoretical_result', 'discussion', 'explicit_noncoverage']
    passage_ids: list[Text] = Field(min_length=1)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.passage_ids)) != len(self.passage_ids):
            raise ValueError('duplicate passages in a claim')
        return self


class Implication(Strict):
    premise_claim_ids: list[Text] = Field(min_length=1)
    steps: list[Text] = Field(min_length=1)
    assumptions: list[Text] = Field(min_length=1)
    assumptions_status: Literal['established', 'unresolved']


class ProposalMatch(Strict):
    facet_path: Text
    claim_ids: list[Text] = Field(min_length=1)
    relationship_match: Literal['covered','partial','unknown']
    explanation: Text = Field(description='Why prior evidence covers this proposed component scientifically, not merely its motivating problem; effect sign is assessed separately.')


class Relationship(Strict):
    target_id: Text
    intervention: list[Text] = Field(description='Prior claim IDs; empty if not established.')
    comparator: list[Text]
    conditions: list[Text]
    outcome: list[Text]
    conclusion: list[Text]
    coverage: Literal['direct_empirical', 'direct_theoretical', 'inferred_implication', 'discussed', 'related', 'no_match_found', 'insufficient_evidence']
    result: Literal['supports_prediction', 'contradicts_prediction', 'mixed', 'inconclusive', 'not_assessed']
    inference: Implication | None
    proposal_matches: list[ProposalMatch] = Field(default_factory=list)
    explanation: Text = Field(description='Interpretation of these claims against the candidate. A changed dataset alone does not establish a new relationship.')

    def refs(self):
        return set(self.intervention+self.comparator+self.conditions+self.outcome+self.conclusion+(self.inference.premise_claim_ids if self.inference else [])) | {c for m in self.proposal_matches for c in m.claim_ids}

    @model_validator(mode='after')
    def implication(self):
        if (self.coverage=='inferred_implication') != (self.inference is not None):
            raise ValueError('inferred implication requires explicit premises, steps and assumptions exclusively')
        if self.inference and self.inference.assumptions_status!='established':
            raise ValueError('unresolved assumptions cannot establish implication; use insufficient_evidence and explain the missing assumption')
        if self.coverage in ('direct_empirical','direct_theoretical','discussed','inferred_implication') and not self.conclusion:
            raise ValueError('direct/discussed/inferred coverage needs conclusion claims')
        if self.coverage in ('related','no_match_found','insufficient_evidence','discussed') and self.result!='not_assessed':
            raise ValueError('an unestablished relationship cannot carry a result')
        return self


class EvidenceRecord(Strict):
    claims: list[PriorClaim]
    relationships: list[Relationship] = Field(min_length=1)

    @model_validator(mode='after')
    def references(self):
        claims={c.claim_id:c for c in self.claims}
        if len(claims)!=len(self.claims) or len({r.target_id for r in self.relationships})!=len(self.relationships):
            raise ValueError('duplicate evidence claim or target ID')
        for r in self.relationships:
            if not r.refs()<=claims.keys(): raise ValueError('unknown relationship claim reference')
            kinds={claims[c].kind for c in r.conclusion}
            required={'direct_empirical':'result','direct_theoretical':'theoretical_result','discussed':'discussion'}.get(r.coverage)
            if required and required not in kinds: raise ValueError(f'{r.coverage} needs a {required} conclusion claim')
        return self


class EvidenceResponse(Strict):
    record: EvidenceRecord | None
    abstention_reason: Text | None
    revision_notes: list[Text] = Field(description='Explain repairs or source-backed disagreement with earlier objections; empty on initial extraction.')

    @model_validator(mode='after')
    def exclusive(self):
        if (self.record is None)==(self.abstention_reason is None):raise ValueError('record or abstention exclusively')
        return self


class ScopeCheck(Strict):
    aspect: Literal['result', 'model', 'comparator', 'dataset', 'regime', 'measurement', 'assumption', 'other']
    assertion: Text = Field(description='The specific assertion/condition being checked, not a generic checklist item.')
    decision: Literal['supported', 'missing_citation', 'overstated', 'unresolved']
    passage_ids: list[Text] = Field(description='Exact evidence for this assertion, or passages exposing the defect.')
    explanation: Text


class ClaimCheck(Strict):
    claim_id: Text
    decision: Literal['supported','revise','unresolved']
    explanation: Text
    scope_checks: list[ScopeCheck] | None = None
    scope_summary: Text | None = Field(default=None, description='Explain which result conditions matter and any ambiguity. Empty checks are allowed only for non-result claims with no applicable scoped assertions.')
    passage_ids: list[Text] = Field(min_length=1, description='Passages inspected to support this judgment; do not invent quotations.')


class CoverageCheck(Strict):
    target_id: Text
    decision: Literal['adequate','revise','unresolved']
    explanation: Text = Field(description='Check relationship mapping AND search supplied pages for omitted evidence that would change it; state specific omissions or limits.')
    passage_ids: list[Text] = Field(description='Relevant inspected passages, including omitted decisive evidence if any. Empty only when evidence is insufficient.')


class EvidenceReport(Strict):
    claim_checks: list[ClaimCheck]
    coverage_checks: list[CoverageCheck]
    summary: Text


class ReviewFormatRepair(Strict):
    invalid_output: str | dict
    validation_error: Text
    model: str | None


class EvidenceReview(Strict):
    version: int = Field(ge=0, le=2)
    trigger: Literal['initial','evidence_repair','comparison_reopen']
    record: EvidenceRecord
    revision_notes: list[Text]
    format_repairs: list[ReviewFormatRepair] = Field(default_factory=list,max_length=1)
    model: str | None
    report: EvidenceReport | None
    error: str | None

    @model_validator(mode='after')
    def exclusive(self):
        if (self.report is None)==(self.error is None):raise ValueError('evidence review requires report or error exclusively')
        return self
