"""Hand-authored fictional evidence for Opportunity Miner quality controls.

These are synthetic test documents, never scientific findings. Upstream reviewed
observations are constructed fixtures; only the miner is under test here.
"""
from pathlib import Path
from landscape.schemas import Landscape
from opportunities.miner import digest
from opportunities.schemas import Candidate
from reasoning.evidence import PaperStore, candidates
from reasoning.fixtures import _base, _loc
from reasoning.schemas import (CrossPaperReasoning, Coverage, Evidence, Observation,
                               ReviewSource, RunInfo)
import json

CASES = {
    'recurring_limitation': {
        'pages':[
            'Method A learns expert-residency scores offline and freezes them during serving. The authors explicitly state that adapting the scores when workloads change is unresolved. No online update method is evaluated.',
            'Method B fits expert-residency scores before deployment and never updates them. The authors explicitly identify handling post-deployment workload drift as a limitation. They do not evaluate an adaptation mechanism.'],
        'question':'How can frozen expert-residency scores remain useful after workload drift in the two described serving methods?',
        'rationale':'Both papers explicitly report frozen scores and unresolved adaptation to workload changes.'},
    'restrictive_assumption': {
        'pages':[
            'Scheduler A guarantees a bounded queue only under independent identically distributed arrivals. Its proof assumes stationarity. The authors explicitly leave correlated burst arrivals outside the theorem; no such experiment is reported.',
            'Scheduler B guarantees a bounded queue under independent stationary arrivals with bounded service times. Correlated bursts violate its independence assumption. The authors give no guarantee or measurement for burst-correlated arrivals.'],
        'question':'Can queue stability be established for either described scheduler under correlated burst arrivals?',
        'rationale':'The available stability guarantees require independent stationary arrivals and leave correlated bursts unresolved.'},
    'missing_regime': {
        'pages':[
            'Inference method A was evaluated on one eight-GPU node using NVLink. Tensor parallelism of four uses four of those eight GPUs; it is not a separate four-GPU machine. The authors explicitly did not evaluate communication across nodes.',
            'Inference method B was evaluated on one eight-GPU NVLink node. No multi-node evaluation is reported, and the authors explicitly leave inter-node latency and topology effects for future work.'],
        'question':'How do the two inference methods behave across nodes when inter-node latency and topology differ from the tested NVLink node?',
        'rationale':'Both evaluated one-node systems and explicitly leave multi-node behavior unresolved.'},
    'contradiction': {
        'pages':[
            'Table 1 reports baseline mean latency of 20 ms and cache-enabled latency of 10 ms on model M, GPU G, workload W, batch 1. Thus caching improves mean latency. The implementation seed and warmup procedure are not documented.',
            'Table 1 reports baseline mean latency of 20 ms and cache-enabled latency of 30 ms on model M, GPU G, workload W, batch 1. Thus caching worsens mean latency. The implementation seed and warmup procedure are not documented.'],
        'question':'What unreported implementation or measurement factors could explain the opposite cache-latency effects under the two papers\' nominally matched settings?',
        'rationale':'The reports describe the same model, GPU, workload and batch but opposite effects; undocumented seed and warmup prevent treating conditions as fully identical.'},
    'failure_mode': {
        'pages':[
            'Classifier A has 92% overall accuracy but zero recall on the rare class Z under shift S. The authors identify this as a failure, and report no repair. This result concerns class Z and shift S only.',
            'Classifier B has 94% overall accuracy but zero recall on rare class Z under shift S. The failure remains unresolved; no repair or shifted-class ablation is reported. This result is restricted to class Z and shift S.'],
        'question':'What causes the shared class-Z recall collapse under shift S, and can that failure be avoided in the two classifiers?',
        'rationale':'Both classifiers fail on the same rare class and shift despite high overall accuracy; neither explains or repairs that failure.'},
    'missing_evaluation': {
        'pages':[
            'Serving method A reports median request latency of 10 ms on workload W. The authors explicitly state that tail latency was not measured; p95 and p99 are absent. Median latency does not determine the tail.',
            'Serving method B reports median request latency of 8 ms on workload W. Its report explicitly omits tail-latency measurements; p95 and p99 were not recorded. No conclusion about tail performance is justified.'],
        'question':'How do the two serving methods compare on p95 and p99 latency for workload W?',
        'rationale':'Only median latency is reported, leaving the tail-latency comparison unmeasured.'},
    'unresolved_tradeoff': {
        'pages':[
            'On model M and workload W, offloading method A uses 12 GB memory at 40 ms/token, versus a 24 GB, 20 ms/token baseline. The memory saving costs latency. Only this offload setting is tested; intermediate allocations are not evaluated.',
            'On model M and workload W, offloading method B uses 8 GB memory at 70 ms/token, versus a 24 GB, 20 ms/token baseline. Only this allocation is tested. The report does not determine which allocations form a memory-latency Pareto frontier.'],
        'question':'Which memory allocations offer nondominated memory-latency choices between the measured endpoints for model M and workload W?',
        'rationale':'The reported memory savings come with latency costs, but isolated operating points do not determine the intermediate Pareto frontier.'},
    'mechanistic_interaction': {
        'pages':[
            'Compression method A lowers router entropy from 4 bits to 2 bits on model M and workload W at batch 1. Expert caching is disabled in every experiment. The authors explicitly state that the consequences for cache locality have not been measured.',
            'Cache method B improves latency when routing entropy is 2 bits versus 4 bits on model M and workload W at batch 1. Compression is disabled in every experiment. No joint compression-and-caching experiment is performed.'],
        'question':'Does compression-induced routing-entropy reduction improve expert-cache locality and latency when compression and caching are combined on model M and workload W?',
        'rationale':'One report connects compression to entropy and the other connects entropy to caching, but their joint effect is untested and cannot be inferred by simply adding independent benefits.'},
}


def fixture(root: Path, category: str, *, case=None):
    case=CASES[category] if case is None else case
    ids=[category+'-a',category+'-b']
    for pid,page in zip(ids,case['pages']):
        folder=root/pid
        folder.mkdir(parents=True,exist_ok=True)
        paper=_base('SYNTHETIC VALIDATION '+pid)
        paper['key_results']=[{'finding':page,'quantitative_result':None,'comparison':None,
                             'importance':'Controlled test evidence','source_locations':[_loc(1)]}]
        (folder/'paper.json').write_text(json.dumps(paper,indent=2))
        (folder/'01.md').write_text('# SYNTHETIC TEST DOCUMENT\n\n'+page+'\n')
    land=Landscape(topic='Synthetic control: '+category,paper_ids=ids,
        aggregated_findings=[{'statement':case['rationale'],'supporting_papers':ids}])
    store=PaperStore(root)
    observations=[]
    for pid in ids:
        paper=store.get(pid)
        if paper.status!='loaded':
            raise ValueError(paper.error)
        row=next(c for c in candidates(paper.paper,pid) if c.value_path=='/key_results/0')
        page=store.page(pid,1)
        ev=Evidence(evidence_id='e1',paper_id=pid,value_path=row.value_path,provenance_path=row.provenance_path,
            source_locations=row.source_locations,source_value=row.source_value,summary=row.source_value,
            stance='supports',artifact_sha256=paper.sha256)
        observations.append(Observation(observation_id=pid,thread_id='fixture',refines=[land.aggregated_findings[0].item_id],
            statement=case['pages'][ids.index(pid)],conditions=[],evidence=[ev],review_sources=[ReviewSource(
                paper_id=pid,page=1,key_or_path=page.key_or_path,sha256=page.sha256)],
            review_notes='Hand-authored reviewed-source fixture, not a live upstream review.'))
    reasoning=CrossPaperReasoning(topic=land.topic,landscape_ref={'schema_version':land.schema_version,'sha256':digest(land.model_dump())},
        run=RunInfo(provider=None,draft_model=None,review_model=None,prompt_version='synthetic-fixture',budgets={},verify_mode='pages',token_counter='none',started='fixture'),
        findings=[],observations=observations,tensions=[],diagnostics=[],usage={},
        coverage=Coverage(papers={},threads={},citations={},candidates={},review_pages={},calls={}))
    candidate=Candidate(category=category,question=case['question'],rationale=case['rationale'],
        scope='Only the settings and evidence in these two synthetic test documents; no claim about other papers or literature-wide novelty.',
        uncertainties=['The question is inferred from the supplied evidence and has not been experimentally resolved by these documents.'],
        source_ids=['observation:'+p for p in ids],evidence_ids=['observation:'+p+'/e1' for p in ids])
    return land,reasoning,candidate
