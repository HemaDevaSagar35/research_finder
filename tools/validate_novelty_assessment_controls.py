"""Small synthetic live controls with predeclared scientific-overlap expectations.

These exercise synthesis and its independent reviewer, not upstream extraction.
They do not measure literature-wide novelty or hypothesis usefulness.
"""
import argparse
import asyncio
from dataclasses import asdict
from copy import deepcopy
import json
from pathlib import Path

from llm_client import AsyncLLMClient
from novelty.assessment import NoveltyAssessor, Settings
from novelty.assessment_evidence import validate_draft
from tools.validate_novelty_search import save


def control(name):
    hypothetical = 'Entropy-conditioned expert cache allocation reduces transfer stalls versus LRU under changing routing distributions.'
    if name == 'separate_components':
        hypothetical = 'Entropy-conditioned cache allocation combined with expert compression reduces transfer stalls beyond either component alone.'
    candidate = dict(title='Synthetic cache research control', research_direction=hypothetical,
        rationale=[dict(statement='Prior cache research motivates a testable extension.', evidence_ids=['motivation'], page_ids=['seed#p1'])],
        proposed_mechanism='Routing uncertainty controls expert residency; compressed experts may change the cost of residency.',
        scope='Model B under changing routing distributions.', assumptions=['Matched memory budget.'],
        hypotheses=[dict(hypothesis_id='H1', condition='Changing routing distributions at matched memory budget.',
            intervention='Entropy-conditioned cache allocation' + (' with expert compression' if name == 'separate_components' else ''),
            expected_effect=hypothetical, mechanism='Adapt expert residency to routing uncertainty.',
            assumptions=['Equal available memory.'], falsification_criterion='No decrease in transfer stalls versus the specified comparator.',
            evidence_ids=['motivation'])],
        experiments=[dict(experiment_id='E1', stage='initial', hypothesis_ids=['H1'],
            objective='Test the proposed relationship.', comparison='Matched-budget baseline.',
            observations=['Transfer stalls.'], why_this_test='Directly tests the predicted effect.',
            informative_outcomes='Lower, unchanged or higher stalls distinguish the proposed effect.')],
        risks=['Workload-specific effects.'], uncertainties=['Generalization remains untested.'],
        what_would_falsify_it='No predicted improvement under the stated conditions.')
    if name in ('negative_result', 'known_with_skipped_work'):
        facts = [('P1', 'At matched memory on Model B with changing routing distributions, entropy-conditioned expert cache allocation was tested against LRU. It did not reduce transfer stalls.', 'direct_empirical', 'contradicts_prediction', 'SAME')]
    elif name == 'cosmetic_model_change':
        facts = [('P1', 'At matched memory on Model A with changing routing distributions, entropy-conditioned expert cache allocation was tested against LRU and reduced transfer stalls. Model B uses the same routing/caching mechanism; the proposal supplies no scientific reason this changes the relationship.', 'direct_empirical', 'supports_prediction', 'VERY_CLOSE')]
    elif name == 'separate_components':
        facts = [
            ('P1', 'Entropy-conditioned expert cache allocation reduces transfer stalls under changing routing distributions. No expert compression or joint interaction was studied.', 'related', 'not_assessed', 'PARTIAL_OVERLAP'),
            ('P2', 'Expert compression reduces transfer volume using static cache allocation. No entropy-conditioned residency or its interaction with compression was studied.', 'related', 'not_assessed', 'PARTIAL_OVERLAP')]
    elif name == 'material_regime_change':
        facts = [('P1', 'Entropy-conditioned expert caching reduces transfer stalls only on stationary routing distributions. The same evaluation explicitly did not study changing distributions. Stale routing estimates may fail when routing changes faster than cache residency can adapt.', 'related', 'not_assessed', 'PARTIAL_OVERLAP')]
    elif name == 'unsupported_result':
        facts = [('P1', 'On Model B with changing routing distributions at matched memory, entropy-conditioned allocation significantly reduced transfer stalls by 7% compared with LRU.', 'direct_empirical', 'supports_prediction', 'SAME')]
    else:
        raise ValueError(name)
    coverage, evidence, sources = [], [], []
    for pid, text, rel, result, classification in facts:
        source = text if name != 'unsupported_result' else 'On Model B with changing routing distributions at matched memory, entropy-conditioned allocation did not significantly reduce transfer stalls compared with LRU; the 95% interval was [-3%, +2%].'
        sources.append(dict(passage_id=pid+'#p1:control', page_id=pid+'#p1', text=source))
    for tid, level in [('D', 'direction'), ('H1', 'hypothesis')]:
        rows = [dict(paper_id=pid, status='reviewed', reasons=[], missing_referenced_pages=[]) for pid, *_ in facts]
        blockers = []
        if name == 'known_with_skipped_work':
            rows.append(dict(paper_id='P_unfinished', status='skipped', reasons=['Not compared yet.'], missing_referenced_pages=[]))
            blockers = ['P_unfinished: skipped']
        coverage.append(dict(target_id=tid, level=level, retrieval_status='complete', papers=rows,
            blockers=blockers, complete=not blockers))
        for pid, text, rel, result, classification in facts:
            evidence.append(dict(paper_id=pid, target_id=tid,
                claims=[dict(claim_id='C1', text=text, kind='result', passage_ids=[pid+'#p1:control'])],
                relationship=dict(target_id=tid, intervention=['C1'], comparator=['C1'], conditions=['C1'],
                    outcome=['C1'], conclusion=['C1'], coverage=rel, result=result, inference=None,
                    proposal_matches=[], explanation=text),
                comparison=dict(target_id=tid, classification=classification, rationale=text,
                    dimensions=[dict(dimension=d, relation='PARTIAL' if classification == 'PARTIAL_OVERLAP' else 'CLOSE' if classification == 'VERY_CLOSE' else 'SAME',
                        claim_ids=['C1'], rationale=text) for d in ['problem','method','mechanism','signal','regime','evaluation','scientific_question','hypothesis']],
                    hypothesis_tested='tested' if rel=='direct_empirical' else 'not_established',
                    hypothesis_claim_ids=['C1'] if rel=='direct_empirical' else [], additional_uncertainties=[]),
                evidence_scope='available_extracted_pages', missing_referenced_pages=[]))
    return dict(direction_id='D', candidate_sha256='synthetic-control', candidate=candidate,
        signature=None, coverage=coverage, evidence=evidence, source_passages=sources,
        scope='Synthetic control with fixed known facts, not an actual literature search.', retrieval_context={})


def matched(name, candidate):
    if name == 'unsupported_result':
        return candidate.assessment is None and any(r.report and r.report.reassessment_requests for r in candidate.reviews)
    if candidate.assessment is None:
        return False
    findings = {t.target_id: t.finding for t in candidate.assessment.targets}
    if name in ('negative_result', 'known_with_skipped_work', 'cosmetic_model_change'):
        return findings == {'D':'ALREADY_STUDIED', 'H1':'ALREADY_STUDIED'} and candidate.assessment.refinement.action == 'reject'
    return findings['H1'] in ('COMPONENTS_KNOWN', 'PARTIAL_OVERLAP', 'LOW_PRIOR_OVERLAP') and candidate.assessment.refinement.action != 'reject'


async def main(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    names = ['negative_result', 'known_with_skipped_work', 'cosmetic_model_change',
        'separate_components', 'material_regime_change', 'unsupported_result']
    expectations = dict(
        negative_result='A negative empirical test is still an already-studied relationship.',
        known_with_skipped_work='Positive known-work evidence survives incomplete other comparisons.',
        cosmetic_model_change='Changing model alone does not make the same relationship novel.',
        separate_components='Components separately known do not establish the interaction.',
        material_regime_change='Mechanism-relevant regime change is a potential remaining distinction.',
        unsupported_result='Source contradiction causes an explicit section-12 reassessment, not publication.')
    save(out/'expectations.json', expectations)
    client = AsyncLLMClient('deepseek', concurrency=3, max_retries=0, timeout=600)
    async def one(name):
        packet = control(name)
        save(out/name/'packet.json', packet)
        calls = []
        async def chat(**kw):
            index = len(calls)
            calls.append(json.loads(kw['messages'][1]['content'])['task'])
            result = await client.chat_result(**kw)
            save(out/name/'calls'/f'{index:03d}.json', dict(request=kw, response=asdict(result)))
            print(json.dumps(dict(case=name, task=calls[index], model=result.model)), flush=True)
            return result
        runner = NoveltyAssessor(chat, provider=client.provider, model=client.default_model, settings=Settings(max_calls=8))
        candidate = await runner._candidate(deepcopy(packet))
        if candidate.assessment:
            validate_draft(candidate.assessment, packet)
        row = dict(case=name, matched=matched(name, candidate), expected=expectations[name],
            candidate=candidate.model_dump(), outcomes=candidate.outcomes(), calls=runner.io.budget.snapshot())
        save(out/name/'result.json', row)
        return row
    try:
        rows = await asyncio.gather(*(one(n) for n in names))
        summary = [dict(case=r['case'], matched=r['matched'], calls=r['calls'], outcomes=r['outcomes']) for r in rows]
        save(out/'summary.json', summary)
        print(json.dumps(summary, indent=2))
        return all(r['matched'] for r in rows)
    finally:
        await client.raw.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', required=True)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
