"""Fictional accepted-opportunity fixtures for generator fidelity validation.

Upstream review/acceptance is hand-authored, not an actual scientific finding.
Only the Direction Generator runs live. Rubrics assess consistency with these
explicit facts, not whether the proposed research is valuable or novel.
"""
from pathlib import Path

from landscape.schemas import AggregatedItem, Landscape
from opportunities.miner import digest
from opportunities.schemas import MiningResult, Opportunity, PaperAssessment, SourceReference
from reasoning.schemas import Explanation, Tension, TensionSide
from tools.opportunity_validation_cases import CASES, fixture

CASE_NAMES = ('tension_matched', 'tension_different_conditions', 'untested_interaction', 'gap_with_distraction')


def build_case(root: Path, name: str):
    if name not in CASE_NAMES:
        raise ValueError(name)
    category = 'contradiction' if name.startswith('tension') else (
        'mechanistic_interaction' if name == 'untested_interaction' else 'missing_evaluation')
    spec = CASES[category]
    if name == 'tension_different_conditions':
        spec = {
            'pages': [
                'Cache A reduces mean latency from 20 ms to 10 ms for model M on GPU G1, short-prompt workload W1, batch 1. Warmup procedure and implementation seed are not documented. No crossed hardware/workload experiment is reported.',
                'Cache A increases mean latency from 20 ms to 30 ms for model M on GPU G2, long-prompt workload W2, batch 64. Warmup procedure and implementation seed are not documented. No crossed hardware/workload experiment is reported.'],
            'question': 'Which hardware, workload, batch-size or measurement differences account for the opposite cache-latency effects in the two reported settings?',
            'rationale': 'Opposite effects are reported under different GPUs, prompt workloads and batch sizes. The pages do not isolate which condition explains the difference; this is not an identical-condition contradiction.'}
    land, reasoning, candidate = fixture(root, category, case=spec)
    land.topic = 'Synthetic generator control: ' + name
    observations = reasoning.observations
    reasoning.topic = land.topic
    if name.startswith('tension'):
        sides = [TensionSide(assertion=obs.statement,
                  evidence=[ev.model_copy(update={'evidence_id': f'e{i+1}'}) for ev in obs.evidence])
                 for i, obs in enumerate(observations)]
        tension = Tension(tension_id='tension-1', thread_id='synthetic-thread',
            refines=observations[0].refines, statement=spec['rationale'], sides=sides,
            candidate_explanations=[Explanation(
                text='Unreported warmup or implementation differences might explain the opposite effects; this cause has not been established.',
                evidence_ids=[], hypothesis=True)], distinct_papers=2,
            review_sources=[s for obs in observations for s in obs.review_sources],
            review_notes='Hand-authored synthetic reviewed tension. Both reports must remain visible; causal explanation is untested.')
        reasoning.observations = []
        reasoning.tensions = [tension]
        candidate = candidate.model_copy(update={
            'source_ids': ['tension:tension-1'],
            'evidence_ids': [f'tension:tension-1/{e.evidence_id}' for side in sides for e in side.evidence]})
        sources = [SourceReference(source_id='tension:tension-1', kind='tension', refines=tension.refines)]
        evidence = [e.model_copy(update={'evidence_id': f'tension:tension-1/{e.evidence_id}'}) for side in sides for e in side.evidence]
        pages = tension.review_sources
        rubric = [
            'Keep the opposed observed effects: 20→10 ms and 20→30 ms; do not claim a universal cache benefit.',
            'Investigate the accepted discrepancy rather than replacing it with an unrelated research gap.',
            'Treat warmup/implementation explanations and proposed interventions as untested, not established causes.',
            ('Preserve G1/W1/batch 1 versus G2/W2/batch 64; do not call these identical experimental conditions.'
             if name == 'tension_different_conditions' else
             'Preserve nominally matched M/G/W/batch 1 while acknowledging unknown seed/warmup; do not claim exact replication.')]
    else:
        sources = [SourceReference(source_id='observation:'+obs.observation_id, kind='observation', refines=obs.refines)
                   for obs in observations]
        evidence = [ev.model_copy(update={'evidence_id': 'observation:'+obs.observation_id+'/'+ev.evidence_id})
                    for obs in observations for ev in obs.evidence]
        pages = [s for obs in observations for s in obs.review_sources]
        if name == 'untested_interaction':
            rubric = [
                'Direction attacks the compression/entropy/cache interaction in model M and workload W, rather than an unrelated gap.',
                'Rationale preserves that the two components were tested separately: caching disabled in compression work and compression disabled in caching work.',
                'Joint cache-locality or latency benefit is an untested hypothesis, not a result demonstrated by either document.',
                'Experiments and falsification criteria can reveal absent or adverse joint benefits; proposed numbers are not presented as observed results.']
        else:
            land.aggregated_findings.append(AggregatedItem(
                statement='UNRELATED LANDSCAPE CONTEXT: a different research question concerns image-classification accuracy with low-bit weight quantization. This is not the selected opportunity.',
                supporting_papers=land.paper_ids))
            land = Landscape.model_validate(land.model_dump())
            rubric = [
                'Direction preserves the p95/p99 comparison on workload W; it does not switch to the unrelated image-classification/quantization topic.',
                'Keep the distinction between observed medians (A=10 ms, B=8 ms) and missing tail measurements.',
                'Do not infer or report the p99 ranking from those medians; any ordering is a hypothesis.',
                'An initial experiment measures tails and permits a reversed or absent ranking, rather than assuming B already wins on p99.']
    rubric.extend([
        'The direction and mechanism are specific to the accepted opportunity; hypotheses remain concrete condition/intervention/effect claims.',
        'Suggested tests name a relevant comparison, meaningful observations and informative supporting/contrary outcomes for the linked hypotheses.',
        'Suggestions stay at research-test level; they do not prescribe new seeds, warmup counts, arm matrices, hardware allocations, sample sizes or resource estimates. Source conditions relevant to the question may be retained.'])
    reasoning.landscape_ref = {'schema_version': land.schema_version, 'sha256': digest(land.model_dump())}
    op = Opportunity(opportunity_id='synthetic-'+name, candidate=candidate, sources=sources,
        paper_ids=land.paper_ids, supporting_paper_ids=land.paper_ids, context_paper_ids=[], support='multiple_papers',
        evidence=evidence, review_sources=pages, review_notes='Synthetic accepted fixture for generator-only validation.',
        paper_assessments=[PaperAssessment(paper_id=pid, role='supporting', rationale='Hand-authored fixture support.',
            evidence_ids=[e.evidence_id for e in evidence if e.paper_id == pid],
            page_ids=[f'{s.paper_id}#p{s.page}' for s in pages if s.paper_id == pid]) for pid in land.paper_ids])
    mining = MiningResult(topic=land.topic, landscape_ref=reasoning.landscape_ref,
        reasoning_ref={'schema_version': reasoning.schema_version, 'sha256': digest(reasoning.model_dump())},
        opportunities=[op], diagnostics=[], coverage={}, calls={}, usage={},
        run={'fixture': 'hand-authored acceptance; no live upstream review'})
    return land, reasoning, mining, rubric


# Predeclared checks for the observed real-paper mistakes. These guide manual
# inspection of saved outputs; they are neither a runtime judge nor substring tests.
CONSISTENCY_CHECKS = [
    'Attribute explicit statements only to supplied original text; inferred gaps and unknown details stay inferred/unknown.',
    'Supporting/contrary outcomes refer consistently to the linked prediction, including its success threshold and measured quantity.',
    'Falsification addresses the proposed explanation or benefit, not the usefulness of the research question; partial tests leave the remainder open.',
    'Retain tangible directions and high-level suggested tests without adding execution protocols.'
]
REAL_CONSISTENCY_CASES = {
    'moe:op-000-000': [
        'Unchanged or improved performance in a new regime may weaken an adaptation hypothesis but still answers the missing-regime question.'
    ],
    'kv_cache:op-000-003': [
        'Missing high-sigma evaluation remains a gap inferred from the reviewed material, not an author admission or demonstrated failure.',
        'Benign high-sigma behavior answers the regime question rather than showing the question was uninformative.'
    ],
    'kv_cache:op-000-004': [
        'Distinguish the reported total decode time for a workload from individual-request latency.',
        'If an adaptive policy predicts matching the best baseline, matching it is not later labeled failure for not beating it.'
    ],
    'rag_grounding:op-000-002': [
        'Unseen baseline prompt slots, exemplars or implementation details are not asserted as known facts.',
        'If an output interface changes, do not both require unchanged scoring and silently redefine what the scorer reads.'
    ],
    'rag_grounding:op-000-004': [
        'If a hypothesis predicts ranking instability, stable rankings contradict that hypothesis even when supporting the original ranking.'
    ],
}
