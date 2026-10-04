"""Fixed source-entailment/coverage controls, not a novelty benchmark or judge guarantee."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from llm_client import AsyncLLMClient
from novelty.comparison_records import EvidenceRecord, EvidenceReport, EvidenceResponse
from novelty.comparison_evidence import passages_for_pages, attach_table_context
from novelty.comparison_schemas import Passage
from novelty.comparison_workflow import validate_record, validate_evidence_report, EvidenceBuilder, proposal_requirements, scope_citation_gaps
from novelty import comparison_prompts
from novelty.comparison_prompts import EVIDENCE_REVIEW
from novelty.pipeline import NoveltySearcher, Settings
from novelty.schemas import TargetSignature
from tools.validate_novelty_search import save


def target(text):
    f={'text':text,'basis':'candidate_proposal','proposal_paths':['/hypothesis'],'evidence_ids':[],'source_spans':[]}
    return TargetSignature(target_id='control-H1',level='hypothesis',problem=[f],intervention=[f],
        decision_signal=[],mechanism=[],regime=[],comparison=[],expected_effect=[f],queries=['control']).model_dump()


def relationship(tid,coverage='related',result='not_assessed',conclusion=None,inference=None):
    return {'target_id':tid,'intervention':['c1'],'comparator':[],'conditions':[],'outcome':[],
        'conclusion':conclusion or [],'coverage':coverage,'result':result,'inference':inference,
        'explanation':'Assess this relationship against the candidate and all supplied prior evidence.'}


def fixtures(root):
    cases=[]
    def synthetic(name,proposal,text,claim,kind,rel,expected_claim,expected_coverage,extra=None):
        passages=[p.model_dump() for p in passages_for_pages([{'page_id':'control#p1','text':text}])]
        payload={'prior_paper_id':'control','targets':[target(proposal)],'prior_passages':passages,
            'candidate_source_passages':[],'evidence_scope':'available_extracted_pages','missing_referenced_pages':[]}
        claims=[{'claim_id':'c1','text':claim,'kind':kind,'passage_ids':[passages[0]['passage_id']]}]
        if extra:claims.append({'claim_id':'c2','text':extra,'kind':'result','passage_ids':[passages[1]['passage_id']]})
        cases.append(dict(name=name,payload=payload,record={'claims':claims,'relationships':[rel]},
            expected_claim=expected_claim,expected_coverage=expected_coverage,origin='constructed boundary control'))
    proposal='Under changing request distributions at fixed cache capacity, entropy-adaptive caching improves hit rate over frequency-based caching.'
    setup='We compare entropy-adaptive and frequency-based caching under changing request distributions at equal cache capacity.'
    result='Across the changing-distribution trials, entropy-adaptive caching had lower hit rate than frequency-based caching. The prediction of an improvement was contradicted.'
    synthetic('negative_result_still_investigated',proposal,setup+'\n\n'+result,setup,'method',
        relationship('control-H1','direct_empirical','contradicts_prediction',['c2']),'supported','adequate',result)
    synthetic('omitted_matching_negative_experiment',proposal,setup+'\n\n'+result,setup,'method',
        relationship('control-H1','no_match_found'),'supported','revise')
    theory='Theorem: for bounded values with norm at most one and centered logit spread sigma <= 0.1, approximation error is at most 2*sigma^2. This is proved analytically; no accuracy experiment is performed.'
    synthetic('theory_establishes_relationship','For bounded values with norm at most one and sigma <= 0.1, approximation error is at most 2*sigma^2.',theory,
        'The paper proves approximation error <= 2*sigma^2 for value norms <= 1 and sigma <= 0.1.','theoretical_result',
        relationship('control-H1','direct_theoretical','supports_prediction',['c1']),'supported','adequate')
    inference={'premise_claim_ids':['c1'],'steps':['The approximation bound implies downstream task accuracy improves.'],
        'assumptions':['Downstream task accuracy strictly increases whenever approximation error decreases.'],'assumptions_status':'established'}
    synthetic('unsupported_accuracy_implication','This approximation improves downstream task accuracy.',theory,
        'The paper proves approximation error <= 2*sigma^2 for value norms <= 1 and sigma <= 0.1.','theoretical_result',
        relationship('control-H1','inferred_implication','supports_prediction',['c1'],inference),'supported','revise')
    synthetic('different_dataset_same_investigated_relationship',proposal,
        setup+' The paper evaluates synthetic trace set A.\n\n'+result,setup+' The evaluation uses synthetic trace set A.','method',
        relationship('control-H1','direct_empirical','contradicts_prediction',['c2']),'supported','adequate',result)
    synthetic('discussion_is_not_a_result',proposal,
        'We conjecture entropy-adaptive caching could improve hit rate under changing requests. This is future work; we have no experiment or proof of that relationship.',
        'The experiments show entropy-adaptive caching improves hit rate under changing requests.','result',
        relationship('control-H1','direct_empirical','supports_prediction',['c1']),'revise','revise')
    for name,run,call,selector,claim,expected in (
        ('real_background_is_not_proposed_direction','novelty_comparison_v2_rag_live','010','53eba1f29878',
         'Chain-of-Note never abstains, with zero correct refusals out of 128 on all three models.','supported'),
        ('real_rag_lost_prompt_qualifier','novelty_comparison_v2_rag_live','010','f3e654c2878f',
         'Trace supervision does not improve answer quality.','revise'),
        ('real_rag_explicit_lost_grounding_condition','novelty_comparison_v2_rag_live','010','f3e654c2878f',
         'Trace supervision never improves factual grounding, including when instructions are sparse.','revise'),
        ('real_rag_supported_qualified_null','novelty_comparison_v2_rag_live','010','f3e654c2878f',
         'Under a well-specified prompt, no consistent factual-grounding advantage is established for trace supervision.','supported'),
        ('real_longbench_supported_mention','novelty_comparison_v2_kv_live','008','60ca1bf96321',
         'The paper reports RULER margins over Ada-KV that are proportionally larger than on LongBench.','supported'),
        ('real_rag_fabricated_matched_experiment','novelty_comparison_v2_rag_live','010','b57caf1a6f75',
         'The prior paper experimentally compared mandatory answer/refuse tags with matched no-tag prompts and found increased correct refusals on two of three models.','revise')):
        raw=json.loads((root/run/'calls'/f'{call}.json').read_text())
        old=json.loads(raw['request']['messages'][1]['content'])['payload']
        t=old['targets'][0 if name=='real_background_is_not_proposed_direction' else 1]
        payload={'prior_paper_id':old['prior_paper_id'],'targets':[t],'prior_passages':old['prior_passages'],
            'candidate_source_passages':[],'evidence_scope':'available_extracted_pages','missing_referenced_pages':[]}
        passage=next(p for p in old['prior_passages'] if p['passage_id'].endswith(selector))
        record={'claims':[{'claim_id':'c1','text':claim,'kind':'result','passage_ids':[passage['passage_id']]}],
            'relationships':[relationship(t['target_id'])]}
        if name=='real_background_is_not_proposed_direction':
            record['relationships'][0].update(coverage='direct_empirical',result='supports_prediction',conclusion=['c1'])
        cases.append(dict(name=name,payload=payload,record=record,expected_claim=expected,expected_coverage='revise' if name=='real_background_is_not_proposed_direction' else None,
            origin='saved real-paper extracted evidence'))
    # Scope-specific controls retain the actual paper's complete evidence context.
    for name,run,pid,cid,mode in (
        ('scope_moment_original_mixed_models','novelty_comparison_v4_moment_source','436775470aa1','C14','original'),
        ('scope_moment_explicit_wrong_model','novelty_comparison_v4_moment_source','436775470aa1','C14','wrong_model'),
        ('scope_moment_qualified_models','novelty_comparison_v4_moment_source','436775470aa1','C14','qualified'),
        ('scope_moment_missing_caption','novelty_comparison_v4_moment_source','436775470aa1','C15','original'),
        ('scope_moment_attached_caption','novelty_comparison_v4_moment_source','436775470aa1','C15','caption'),
        ('scope_critical_missing_caption','novelty_comparison_v3_kv_live','a60b8c0468d0','c10','original'),
        ('scope_critical_attached_caption','novelty_comparison_v3_kv_live','a60b8c0468d0','c10','caption')):
        artifact=json.loads((root/run/'result.json').read_text())
        paper=next(p for c in artifact['candidates'] for p in c['papers'] if p['paper_id']==pid)
        claim=next(c.copy() for c in paper['evidence_reviews'][-1]['record']['claims'] if c['claim_id']==cid)
        claim['claim_id']='c1'
        if mode=='wrong_model':
            claim['text']='On each of LLaMA-3.1-8B and Qwen3-4B, MOMENTKV surpasses Ada-KV by exactly +1.35 at L=128 and +0.59 at L=1024 on LongBench.'
        if mode=='qualified':
            claim['text']='On LongBench MOMENTKV has the highest average score at each reported budget on both models. The +1.35 margin over Ada-KV at L=128 is reported for LLaMA-3.1-8B.'
        if mode=='caption':
            caption=next(p for p in paper['passages'] if ('Table 5: Approximation order' in p['text'] if pid=='436775470aa1' else 'Table 1. Detail Results on Ruler' in p['text']))
            claim['passage_ids']=claim['passage_ids']+[caption['passage_id']]
        t=paper['targets'][0]
        cases.append(dict(name=name,payload={'prior_paper_id':pid,'targets':[t],'prior_passages':paper['passages'],
            'candidate_source_passages':[],'evidence_scope':'available_extracted_pages','missing_referenced_pages':[]},
            record={'claims':[claim],'relationships':[relationship(t['target_id'])]},
            expected_claim='supported' if mode in ('qualified','caption') else 'revise',expected_coverage=None,
            origin='fixed saved result-scope regression'))
    same_setup='We compare entropy-adaptive and frequency-based caching under changing request distributions at equal cache capacity on Model A.'
    synthetic('scope_different_model_same_relationship',
        'Entropy-adaptive caching improves hit rate over frequency-based caching under changing request distributions at equal capacity; the proposed evaluation uses Model B.',
        same_setup+'\n\n'+result,same_setup,'method',
        relationship('control-H1','direct_empirical','contradicts_prediction',['c2']),'supported','adequate',result)
    cases[-1]['record']['relationships'][0]['explanation']='The same cache-policy/hit-rate relationship is directly investigated on Model A with a negative result. Transfer to Model B is not established; the model difference alone does not mean the scientific relationship is unstudied.'
    stationary='On Model A at equal cache capacity under stationary request distributions, entropy-adaptive caching outperformed frequency-based caching in hit rate. No changing-distribution experiment is reported in this passage.'
    synthetic('scope_material_regime_difference',proposal,stationary,
        'Entropy-adaptive caching improves hit rate over frequency-based caching under changing request distributions.','result',
        relationship('control-H1','direct_empirical','supports_prediction',['c1']),'revise','revise')
    for source_name,new_name in [('scope_moment_missing_caption','scope_moment_context_restored'),('scope_critical_missing_caption','scope_critical_context_restored')]:
        import copy
        case=copy.deepcopy(next(c for c in cases if c['name']==source_name))
        case['name']=new_name
        case['original_record']=case['record']
        case['record']=attach_table_context(EvidenceRecord.model_validate(case['record']),
            [Passage.model_validate(p) for p in case['payload']['prior_passages']]).model_dump()
        case['expected_claim']='supported'
        case['origin']='original missing-caption claim through production deterministic context attachment'
        cases.append(case)
    for c in cases:
        c['payload']['proposal_requirements']={t['target_id']:proposal_requirements(TargetSignature.model_validate(t)) for t in c['payload']['targets']}
        for rel in c['record']['relationships']:
            if rel['coverage'] in ('direct_empirical','direct_theoretical','inferred_implication'):
                rel['proposal_matches']=[{'facet_path':path,'claim_ids':rel['conclusion'] or ['c1'],
                    'relationship_match':'covered','explanation':'Control asserts relationship coverage; audit it against actual evidence.'}
                    for path in c['payload']['proposal_requirements'][rel['target_id']]]
        validate_record(EvidenceRecord.model_validate(c['record']),[TargetSignature.model_validate(t) for t in c['payload']['targets']],
            [Passage.model_validate(p) for p in c['payload']['prior_passages']])
    return cases


async def main(args):
    cases=fixtures(Path(args.runs))
    if args.case:cases=[c for c in cases if c['name'] in args.case]
    if args.exclude_case:cases=[c for c in cases if c['name'] not in args.exclude_case]
    if not cases:raise ValueError('No matching controls')
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    save(out/'fixtures.json',cases)
    client=AsyncLLMClient('deepseek',concurrency=4,max_retries=0,timeout=600)
    calls=[]
    async def chat(**kw):
        n=len(calls);calls.append(n)
        response=await client.chat_result(**kw)
        save(out/'calls'/f'{n:03d}.json',{'request':kw,'response':asdict(response)})
        return response
    io=NoveltySearcher(None,None,chat,provider=client.provider,model=client.default_model,
        settings=Settings(max_calls=2*len(cases),concurrency=4))
    async def one(c):
        try:
            history=[]
            response=EvidenceResponse(record=EvidenceRecord.model_validate(c['record']),abstention_reason=None,revision_notes=[])
            review=await EvidenceBuilder(io,comparison_prompts).audit(c['payload'],response,
                [Passage.model_validate(p) for p in c['payload']['prior_passages']],history,'initial')
            report=review.report
            claim=next(x.decision for x in report.claim_checks if x.claim_id=='c1')
            gaps=scope_citation_gaps(report,response.record)
            effective_claim='revise' if 'c1' in gaps else claim
            coverage=report.coverage_checks[0].decision
            matched=effective_claim==c['expected_claim'] and (c['expected_coverage'] is None or coverage==c['expected_coverage'])
            row={'name':c['name'],'matched':matched,'expected_claim':c['expected_claim'],
                'expected_coverage':c['expected_coverage'],'raw_claim_decision':claim,'effective_claim_decision':effective_claim,'citation_repairs':gaps,'report':report.model_dump(),'format_repairs':[r.model_dump() for r in review.format_repairs]}
        except Exception as exc:row={'name':c['name'],'matched':False,'error':str(exc)}
        save(out/'cases'/(c['name']+'.json'),row);print(json.dumps(row),flush=True)
        return row
    try:
        rows=await asyncio.gather(*(one(c) for c in cases))
        save(out/'summary.json',{'cases':rows,'calls':io.budget.snapshot(),
            'scope':'Fixed evidence/relationship controls; not an estimate of literature novelty or population judge accuracy.'})
        return all(c['matched'] for c in rows)
    finally:await client.raw.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',action='append');p.add_argument('--exclude-case',action='append');p.add_argument('--runs',default='/home/hema/research_runs');p.add_argument('--out',required=True)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
