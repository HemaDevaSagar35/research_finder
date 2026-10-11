"""Bounded edits to reviewed records; unaffected content is owned by code."""
from pydantic import Field, model_validator
from directions.schemas import Strict, Text
from novelty.comparison_records import EvidenceRecord, PriorClaim, Relationship, EvidenceResponse
from novelty.comparison_schemas import InterpretationPair, ComparisonResponse, EvidenceRequest


class EvidencePatch(Strict):
    claims: list[PriorClaim] = Field(description='Replacement disputed claims only, retaining IDs. New IDs only when allowed to add omitted evidence. Never repeat unaffected claims.')
    relationships: list[Relationship] = Field(description='Replacement allowed target mappings only. Omit unchanged mappings.')
    revision_notes: list[Text]
    abstention_reason: Text | None


class InterpretationPatch(Strict):
    pairs: list[InterpretationPair] = Field(description='Replacement affected target pairs only; unaffected pairs are preserved by code.')
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    abstention_reason: Text | None

    @model_validator(mode='after')
    def exclusive(self):
        if sum(bool(x) for x in (self.pairs,self.evidence_requests,self.abstention_reason)) > 1:
            raise ValueError('patch pairs, evidence requests, or abstention exclusively')
        return self


def attach_review_citations(record, report, passages):
    """Attach only known source IDs actually named by the independent scope audit.

    Attachment does not accept a claim: the resulting record needs a new audit.
    """
    from novelty.comparison_workflow import scope_citation_gaps
    from novelty.comparison_evidence import attach_table_context
    known = {p.passage_id for p in passages}
    gaps = scope_citation_gaps(report, record)
    if any(not set(ids) <= known for ids in gaps.values()):
        raise ValueError('citation repair contains unknown source IDs')
    result = record.model_copy(deep=True)
    for claim in result.claims:
        claim.passage_ids = list(dict.fromkeys(claim.passage_ids + gaps.get(claim.claim_id, [])))
    return attach_table_context(result, passages)


def repair_scope(previous, corrections=None):
    """Only disputed claims and their dependents can change; omissions allow additions."""
    record, report = previous.record, previous.report
    # A pure missing-citation objection needs attachment and re-audit, not prose
    # regeneration. The fresh audit can still reject its scientific support.
    from novelty.comparison_workflow import scope_citation_gaps
    gaps = scope_citation_gaps(report, record)
    claims = {c.claim_id for c in report.claim_checks if c.decision != 'supported'
        and not (c.decision == 'revise' and c.claim_id in gaps and c.scope_checks
            and all(x.decision in ('supported', 'missing_citation') for x in c.scope_checks))} if report else set()
    targets = {c.target_id for c in report.coverage_checks if c.decision != 'adequate'} if report else set()
    allow_additions = bool(targets)
    if corrections:
        allow_additions = True
        for issue in corrections.get('issues', []):
            parts = issue['field_path'].strip('/').split('/')
            if len(parts) >= 2 and parts[0] == 'claims':
                # Comparison claims are an eligible subset, so the caller passes
                # stable IDs resolved against the actual comparison draft.
                claims.update(corrections.get('disputed_claim_ids', []))
        for request in corrections.get('evidence_requests', []):
            targets.update(request['target_ids'])
    targets.update(r.target_id for r in record.relationships if r.refs() & claims)
    return dict(claim_ids=sorted(claims), target_ids=sorted(targets), allow_new_claims=allow_additions)


def apply_evidence_patch(record, patch, scope):
    if patch.abstention_reason:
        raise ValueError('evidence_abstained: ' + patch.abstention_reason)
    old_claims = {c.claim_id: c for c in record.claims}
    old_targets = {r.target_id: r for r in record.relationships}
    if len({c.claim_id for c in patch.claims}) != len(patch.claims) or len({r.target_id for r in patch.relationships}) != len(patch.relationships):
        raise ValueError('duplicate patch identities')
    for c in patch.claims:
        if c.claim_id in old_claims and c.claim_id not in scope['claim_ids']:
            raise ValueError('patch changes an unaffected claim: ' + c.claim_id)
        if c.claim_id not in old_claims and not scope['allow_new_claims']:
            raise ValueError('new claims require a recorded omission/reopen')
    for r in patch.relationships:
        if r.target_id not in old_targets or r.target_id not in scope['target_ids']:
            raise ValueError('patch changes an unaffected/unknown target: ' + r.target_id)
    replacements = {c.claim_id: c for c in patch.claims}
    mappings = {r.target_id: r for r in patch.relationships}
    merged = EvidenceRecord(claims=[replacements.get(c.claim_id, c) for c in record.claims] +
        [c for c in patch.claims if c.claim_id not in old_claims],
        relationships=[mappings.get(r.target_id, r) for r in record.relationships])
    return EvidenceResponse(record=merged, abstention_reason=None, revision_notes=patch.revision_notes)


def affected_pair_ids(draft, report):
    affected = set()
    for issue in report.issues:
        parts = issue.field_path.strip('/').split('/')
        if len(parts) >= 2 and parts[0] == 'pairs':
            affected.add(draft.pairs[int(parts[1])].target_id)
        else:
            raise ValueError('claim objections require evidence repair before interpretation')
    return affected


def apply_interpretation_patch(draft, patch, affected):
    if patch.evidence_requests or patch.abstention_reason:
        return ComparisonResponse(pairs=[], evidence_requests=patch.evidence_requests,
            abstention_reason=patch.abstention_reason)
    replacements = {p.target_id: p for p in patch.pairs}
    if len(replacements) != len(patch.pairs) or not set(replacements) <= affected:
        raise ValueError('interpretation patch changes an unaffected/duplicate target')
    fields = InterpretationPair.model_fields
    pairs = [replacements.get(p.target_id) or InterpretationPair.model_validate(
        {k: v for k, v in p.model_dump().items() if k in fields}) for p in draft.pairs]
    return ComparisonResponse(pairs=pairs, evidence_requests=[], abstention_reason=None)


def review_addresses(draft):
    """Give reviewers exact legal pointers rather than requiring index arithmetic."""
    result = {}
    for i, c in enumerate(draft.claims):
        result[f'/claims/{i}/text'] = c.claim_id
    for i, p in enumerate(draft.pairs):
        for field in ('classification', 'rationale', 'additional_uncertainties', 'hypothesis_tested', 'hypothesis_claim_ids'):
            result[f'/pairs/{i}/{field}'] = p.target_id
        for j, d in enumerate(p.dimensions):
            for field in ('rationale', 'relation', 'claim_ids'):
                result[f'/pairs/{i}/dimensions/{j}/{field}'] = p.target_id + ': ' + d.dimension
    return result
