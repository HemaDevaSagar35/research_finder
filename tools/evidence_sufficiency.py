"""Experimental mapping repair; never changes claims or bypasses independent review."""
from typing import Literal
from pydantic import Field
from directions.schemas import Strict, Text
from novelty.comparison_records import Relationship, EvidenceRecord
from novelty.comparison_workflow import scope_citation_gaps

PROMPT = '''Assess whether already-supported evidence suffices for a defensible comparison.
You receive original paper passages, immutable proposal targets, extracted claims and an
independent evidence review. Do not repair, rewrite or invent claims. For EACH target,
return sufficient or insufficient with an explanation. For insufficient, return a null
relationship; code preserves the original. For sufficient, propose a relationship and
explain why excluded evidence cannot materially change the judgment. Use only supplied supported_claim_ids in a sufficient relationship.
An unresolved claim may be excluded only if its uncertainty cannot materially change the
proposed judgment. Do not hide omitted experiments, counterexamples or decisive overlap.
Inspect the original review's coverage objections as well as claim objections. Do not
turn missing support into no_match_found or novelty. If decisive evidence is unavailable,
return insufficient. You may give a narrower
relationship only when the supported evidence establishes it and the remaining uncertainty
cannot change it. Explain why removed claims are nonessential to that judgment. This is
only a proposal: a fresh independent review will check the full record and all passages.
Return exactly the supplied schema.'''

class Exclusion(Strict):
    claim_id: Text
    reason: Text

class Decision(Strict):
    target_id: Text
    decision: Literal['sufficient', 'insufficient']
    explanation: Text
    excluded_claims: list[Exclusion] = Field(default_factory=list)
    relationship: Relationship | None

class SufficiencyReport(Strict):
    targets: list[Decision] = Field(min_length=1)


def supported_ids(record, report):
    gaps = scope_citation_gaps(report, record)
    return {c.claim_id for c in report.claim_checks if c.decision == 'supported' and c.claim_id not in gaps}


def apply(record, report, proposal):
    originals = {r.target_id: r for r in record.relationships}
    ids = [d.target_id for d in proposal.targets]
    if len(ids) != len(set(ids)) or set(ids) != set(originals):
        raise ValueError('Sufficiency must cover each target exactly once')
    supported = supported_ids(record, report)
    relationships = []
    for d in proposal.targets:
        old = originals[d.target_id]
        if d.decision == 'insufficient':
            relationships.append(old)
            continue
        if d.relationship is None or d.relationship.target_id != d.target_id:
            raise ValueError('Mismatched or absent sufficiency target')
        if not d.relationship.refs() <= supported:
            raise ValueError('Sufficient mapping relies on unsupported claims')
        relationships.append(d.relationship)
    return EvidenceRecord(claims=record.claims, relationships=relationships)


def removal_review_context(original, updated, proposal):
    """Code-owned removals; reviewer must assess materiality, not model bookkeeping."""
    old = {r.target_id:r for r in original.relationships}
    new = {r.target_id:r for r in updated.relationships}
    return dict(instructions='Check EVERY removed reference against the original passages and previous objections. '
        'Mark coverage adequate only if exclusion cannot materially change the judgment; '
        'do not infer absence from removed evidence. A sufficiency suggestion is not approval.',
        targets=[dict(target_id=d.target_id, decision=d.decision, explanation=d.explanation,
            removed_claim_ids=sorted(old[d.target_id].refs()-new[d.target_id].refs()),
            original_relationship=old[d.target_id].model_dump(),
            proposed_relationship=new[d.target_id].model_dump()) for d in proposal.targets])
