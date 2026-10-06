"""Section-12 evidence, scoped overlap, exact reviewed handoff and async failures."""
import asyncio
from copy import deepcopy
import json
import pytest
from pydantic import ValidationError
from llm_client import ChatResult
from novelty.comparison import NoveltyComparator, Settings
from novelty.comparison_schemas import DIMENSIONS, NoveltyComparisonResult, PairComparison
from novelty.schemas import NoveltySearchResult
from opportunities.miner import digest
from reasoning.evidence import PaperStore
from tests.test_novelty_search import prepared, run as search_run
from tests.test_direction_generator import inputs


@pytest.fixture
def ready(prepared):
    root, generation = prepared
    search, _, _ = search_run(prepared)
    (root/'outside-paper'/'01.md').write_text('We evaluate caching under stable workloads. Compression is not evaluated in these experiments.')
    (root/'outside-paper'/'02.md').write_text('Future work could study compression-aware routing.')
    return root, generation, search


def output(payload):
    passage = payload['prior_passages'][0]
    return {'comparison': {'claims':[{'claim_id':'c1','text':'Caching is evaluated.',
        'kind':'result','passage_ids':[passage['passage_id']]}],
        'pairs': [dict(target_id=t['target_id'],
        dimensions=[dict(dimension=d, relation='PARTIAL', claim_ids=['c1'],
            rationale='Related findings, different conditions.') for d in DIMENSIONS],
        classification='PARTIAL_OVERLAP', rationale='Method family overlaps; the relationship is unresolved.',
        hypothesis_tested='not_established', hypothesis_claim_ids=[], additional_uncertainties=[])
        for t in payload['targets']]}, 'abstention_reason':None}


def evidence_output(payload):
    claims=output(payload)['comparison']['claims']
    return {'record':{'claims':claims,'relationships':[
        {'target_id':t['target_id'],'intervention':['c1'],'comparator':[],'conditions':[],
         'outcome':[],'conclusion':[],'coverage':'related','result':'not_assessed','inference':None,
         'explanation':'Related cache study, exact relationship not established in supplied evidence.'}
        for t in payload['targets']]},'abstention_reason':None,'revision_notes':[]}


def evidence_report(record):
    return {'claim_checks':[{'claim_id':c['claim_id'],'decision':'supported','explanation':'Fixture supported.',
         'passage_ids':c['passage_ids'], 'scope_summary':'Fixture scope checked.',
         'scope_checks':[{'aspect':'result','assertion':c['text'],'decision':'supported',
             'passage_ids':c['passage_ids'],'explanation':'Fixture conditions supported.'}]} for c in record['claims']],
        'coverage_checks':[{'target_id':r['target_id'],'decision':'adequate','explanation':'Fixture coverage inspected.',
         'passage_ids':[]} for r in record['relationships']], 'summary':'Fixture evidence audit.'}


class Chat:
    def __init__(self, mutate=None, decisions=None, fail=None):
        self.mutate, self.decisions, self.fail = mutate, decisions or ['pass'], fail
        self.calls, self.reviews = [], 0
        self.active = self.peak = 0
    async def __call__(self, **kw):
        req = json.loads(kw['messages'][1]['content'])
        self.calls.append((req, kw))
        self.active += 1; self.peak = max(self.peak, self.active)
        await asyncio.sleep(.005)
        self.active -= 1
        if req['task'] == self.fail:
            raise RuntimeError('provider unavailable')
        if req['task'] in ('extract_comparison_evidence','revise_comparison_evidence','repair_comparison_evidence'):
            out=evidence_output(req['payload'])
        elif req['task'] in ('patch_comparison_evidence','repair_evidence_patch'):
            out={'record':deepcopy(req['record']),'abstention_reason':None,'revision_notes':[]}
        elif req['task'] in ('review_comparison_evidence','repair_evidence_review'):
            out=evidence_report(req['record'])
        elif req['task'] in ('review_comparison', 'repair_comparison_review'):
            decision = self.decisions[min(self.reviews, len(self.decisions)-1)]
            self.reviews += 1
            out = {'decision':decision, 'summary':'Fixture audit.', 'issues':[]}
            if decision == 'revise':
                out['issues'] = [{'category':'consistency', 'field_path':'/pairs/0/rationale',
                    'explanation':'Scope is unclear.', 'required_change':'Clarify scope.', 'passage_ids':[]}]
        else:
            pairs=output(req['payload'])['comparison']['pairs']
            for p in pairs:
                p.pop('hypothesis_tested');p.pop('hypothesis_claim_ids')
            out={'pairs':[p for p in pairs if p['target_id'] in req['eligible_target_ids']],
                'evidence_requests':[],'abstention_reason':None}
            if req['task'] == 'patch_comparison':
                out['pairs']=[p for p in out['pairs'] if p['target_id'] in req['affected_target_ids']]
                out['pairs'][0]['rationale'] = 'Corrected explicit comparison scope.'
        if self.mutate:
            self.mutate(out, req)
        if req['task'] in ('patch_comparison_evidence','repair_evidence_patch'):
            original={c['claim_id']:c for c in req['record']['claims']}
            mappings={r['target_id']:r for r in req['record']['relationships']}
            out=dict(claims=[c for c in out['record']['claims'] if c != original.get(c['claim_id'])],
                relationships=[r for r in out['record']['relationships'] if r != mappings.get(r['target_id'])],
                revision_notes=out['revision_notes'],abstention_reason=out['abstention_reason'])
        return ChatResult(json.dumps(out), 'stop', 'stub')


def run(ready, chat=None, selected=None, **settings):
    root, generation, search = ready
    chat = chat or Chat()
    result = asyncio.run(NoveltyComparator(PaperStore(root), chat,
        settings=Settings(**settings)).run(generation, search, paper_ids=selected))
    return result, chat


def test_full_evidence_batch_and_reviewed_round_trip(ready):
    result, chat = run(ready)
    result = NoveltyComparisonResult.model_validate_json(result.model_dump_json())
    c = result.candidates[0]; p = c.papers[0]
    assert p.status == 'complete' and len(p.comparison.pairs) == 3
    assert len(chat.calls) == 4  # evidence/examination then interpretation/review, targets together
    assert len(p.context_pages) == 2
    assert p.comparison == p.reviews[-1].draft
    payload = chat.calls[0][0]['payload']
    assert payload['paper_navigation'] and len(payload['prior_passages']) == 2
    assert 'source_context' not in payload and 'candidate_views' not in payload
    assert result.search_ref['sha256'] == digest(ready[2].model_dump())
    assert result.literature_novelty == 'not_assessed'


@pytest.mark.parametrize('change', ['paper','source_page','signature_page','no_prior_pages','no_source_page'])
def test_stale_and_missing_evidence(ready, change):
    root, generation, search = ready
    if change == 'paper':
        p = root/'outside-paper'/'paper.json'; p.write_text(p.read_text()+'\n')
    elif change == 'no_prior_pages':
        for p in (root/'outside-paper').glob('*.md'): p.unlink()
    elif change == 'signature_page':
        search.candidates[0].context_pages[0].sha256 = 'stale'
    else:
        ref = generation.directions[0].context_pages[0]
        p = root/ref.paper_id/'01.md'
        if change == 'no_source_page': p.unlink()
        else: p.write_text(p.read_text()+'\n')
    result, chat = run(ready)
    assert result.candidates[0].papers[0].status == 'failed'
    assert not chat.calls


@pytest.mark.parametrize('change', ['generation','candidate','duplicate_direction'])
def test_mismatched_handoff_stops_before_calls(ready, change):
    root, generation, search = ready
    if change == 'generation': search.directions_ref['sha256'] = 'stale'
    elif change == 'candidate': search.candidates[0].candidate_sha256 = 'stale'
    else: search.candidates.append(search.candidates[0])
    chat = Chat()
    with pytest.raises(ValueError): run(ready, chat)
    assert not chat.calls


@pytest.mark.parametrize('bad', ['passage','other_paper','claim','target','dimension','candidate_override'])
def test_invalid_generated_evidence_cannot_pass_review(ready, bad):
    def mutate(out, req):
        if bad in ('passage','other_paper'):
            if req['task'] in ('extract_comparison_evidence','repair_comparison_evidence'):
                out['record']['claims'][0]['passage_ids']=['invented' if bad=='passage' else 'OTHER#p1']
            return
        if req['task'] not in ('compare_prior_work','repair_comparison'): return
        p=out['pairs'][0];d=p['dimensions'][0]
        if bad=='claim':d['claim_ids']=['made-up']
        elif bad=='target':p['target_id']='invented'
        elif bad=='dimension':p['dimensions'][1]['dimension']='problem'
        else:d['candidate_statement']='Altered candidate'
    result,chat=run(ready,Chat(mutate))
    assert result.candidates[0].papers[0].comparison is None
    assert len(chat.calls)==(2 if bad in ('passage','other_paper') else 4)


def test_format_repair_and_fresh_independent_revision(ready):
    def mutate(out, req):
        if req['task'] == 'compare_prior_work': out['pairs'][0]['dimensions'][0]['claim_ids'] = ['invalid']
    result, chat = run(ready, Chat(mutate, ['revise','pass']))
    p = result.candidates[0].papers[0]
    assert p.status == 'complete' and len(p.reviews) == 2
    assert p.comparison.pairs[0].rationale.startswith('Corrected')
    for req, kw in chat.calls:
        if req['task'] == 'review_comparison':
            assert 'corrections' not in req and 'invalid_output' not in req and len(kw['messages']) == 2
    assert result.calls['repair_comparison'] == 1


@pytest.mark.parametrize('decision', ['revise','abstain'])
def test_unresolved_review_withholds(ready, decision):
    result, _ = run(ready, Chat(decisions=[decision]))
    p = result.candidates[0].papers[0]
    assert (p.comparison is None and p.status=='unresolved') if decision=='abstain' else (p.status=='partial' and len(p.comparison.pairs)==2)


def test_invalid_judge_quote_is_not_published(ready):
    def mutate(out, req):
        if req['task'] in ('review_comparison', 'repair_comparison_review'):
            out['issues'][0].update(category='grounding', passage_ids=['fabricated'])
    result, _ = run(ready, Chat(mutate, ['revise']))
    p = result.candidates[0].papers[0]
    assert p.reviews[0].error and p.reviews[0].report is None and p.comparison is None


@pytest.mark.parametrize('limit', [0,1,2,3,4])
def test_attempt_allowance(ready, limit):
    result, chat = run(ready, max_calls=limit)
    assert result.calls['total'] == len(chat.calls) == limit
    assert (result.candidates[0].papers[0].status == 'complete') == (limit == 4)


def test_input_budget_and_provider_failure(ready):
    result, chat = run(ready, max_input_tokens=1)
    assert not chat.calls and result.candidates[0].papers[0].diagnostic
    result, chat = run(ready, Chat(fail='compare_prior_work'))
    assert len(chat.calls) == 3 and result.candidates[0].papers[0].comparison is None


@pytest.mark.parametrize('tamper', ['draft','review','coverage','level'])
def test_persisted_contract_cannot_bypass_review_or_coverage(ready, tamper):
    result, _ = run(ready)
    data = result.model_dump(); c = data['candidates'][0]; p = c['papers'][0]
    if tamper == 'draft': p['comparison']['pairs'][0]['rationale'] = 'edited after review'
    elif tamper == 'review': p['reviews'] = []
    elif tamper == 'coverage': c['papers'] = []
    else:
        p['targets'][0]['level'] = 'hypothesis'
    with pytest.raises(ValidationError): NoveltyComparisonResult.model_validate(data)


def add_prior(ready):
    root, generation, search = ready
    other = root/'second'; other.mkdir()
    for name in ('paper.json','01.md'): (other/name).write_text((root/'outside-paper'/name).read_text())
    data = search.model_dump()
    for s in data['candidates'][0]['searches']:
        hit = dict(s['retrieval']['papers'][0]); hit['paper_id'] = 'second'
        s['retrieval']['papers'].append(hit)
        s['ranked'].append({'paper_id':'second', 'why_relevant':'Fixture'})
        s['paper_artifact_hashes']['second'] = s['paper_artifact_hashes']['outside-paper']
    return root, generation, NoveltySearchResult.model_validate(data)


def test_parallel_paper_work_and_explicit_subset_coverage(ready):
    ready = add_prior(ready)
    result, chat = run(ready, concurrency=2)
    assert chat.peak == 2 and len(chat.calls) == 8
    result, chat = run(ready, selected=['outside-paper'])
    assert len(chat.calls) == 4
    assert [p.status for p in result.candidates[0].papers] == ['complete','skipped']
    assert result.run['selected_papers'] == ['outside-paper']


def test_missing_referenced_page_recorded_not_fabricated(ready):
    root, generation, search = ready
    p = root/'outside-paper'/'paper.json'
    data = json.loads(p.read_text()); data['method']['architecture']['source_locations'] = [{'page':99, 'section':None, 'table':None, 'figure':None, 'equation':None, 'appendix':None}]
    p.write_text(json.dumps(data))
    sha = PaperStore(root).get('outside-paper').sha256
    for s in search.candidates[0].searches: s.paper_artifact_hashes['outside-paper'] = sha
    result, chat = run(ready)
    paper = result.candidates[0].papers[0]
    assert paper.missing_referenced_pages == [99]
    assert chat.calls[0][0]['payload']['missing_referenced_pages'] == [99]


@pytest.mark.parametrize('classification', ['SAME','VERY_CLOSE','PARTIAL_OVERLAP','ADJACENT','DIFFERENT',None])
def test_overlap_contract_and_same_requires_tested_matching_hypothesis(classification):
    pair = output({'targets':[{'target_id':'H1','level':'hypothesis'}], 'prior_passages':[{'passage_id':'P#p1:original','text':'Original evidence'}]})['comparison']['pairs'][0]
    pair['classification'] = classification
    if classification == 'SAME':
        with pytest.raises(ValidationError): PairComparison.model_validate(pair)
        pair['hypothesis_tested'] = 'tested'; pair['hypothesis_claim_ids'] = ['c1']
        for d in pair['dimensions']: d['relation'] = 'SAME'
    assert PairComparison.model_validate(pair).classification == classification


def test_metadata_is_not_candidate_provenance(ready):
    def mutate(out, req):
        if req['task'] in ('compare_prior_work','repair_comparison'):
            out['pairs'][0]['dimensions'][0]['candidate_paths'] = ['/queries/0']
    result, _ = run(ready, Chat(mutate))
    assert result.candidates[0].papers[0].comparison is None


def test_truncation_never_reaches_review(ready):
    stub = Chat()
    async def truncated(**kw):
        r = await stub(**kw)
        return ChatResult(r.text, 'length', r.model)
    result, _ = run(ready, truncated)
    assert result.candidates[0].papers[0].comparison is None
    assert len(stub.calls) == 2 and stub.reviews == 0


def test_separate_reviewer_client_and_model(ready):
    root, generation, search = ready
    draft, review = Chat(), Chat()
    result = asyncio.run(NoveltyComparator(PaperStore(root), draft, review_chat=review,
        model='generator', review_model='critic').run(generation, search))
    assert result.candidates[0].papers[0].status == 'complete'
    assert len(draft.calls) == len(review.calls) == 2
    assert draft.calls[0][1]['model'] == 'generator'
    assert review.calls[0][1]['model'] == 'critic'


def test_empty_and_failed_searches_are_not_novel(ready):
    root, generation, search = ready
    data = search.model_dump()
    for index, s in enumerate(data['candidates'][0]['searches']):
        s['ranked'] = []
        if index:
            s['status'] = 'failed'; s['diagnostic'] = 'retriever failed'
        else: s['retrieval']['papers'] = []
    result, chat = run((root, generation, NoveltySearchResult.model_validate(data)))
    c = result.candidates[0]
    assert not chat.calls and not c.papers and result.literature_novelty == 'not_assessed'
    assert set(c.retrieval_status.values()) == {'complete','failed'}


def test_no_repeat_instance_or_unknown_selection(ready):
    root, generation, search = ready
    with pytest.raises(ValueError): run(ready, selected=['not-shortlisted'])
    runner = NoveltyComparator(PaperStore(root), Chat())
    async def twice():
        await runner.run(generation,search)
        with pytest.raises(ValueError): await runner.run(generation,search)
    asyncio.run(twice())


def test_missing_quotes_and_unknowns_cannot_be_same():
    pair = output({'targets':[{'target_id':'H1','level':'hypothesis'}], 'prior_passages':[{'passage_id':'P#p1:original','text':'Evidence'}]})['comparison']['pairs'][0]
    pair['dimensions'][0]['claim_ids'] = []
    with pytest.raises(ValidationError): PairComparison.model_validate(pair)
    for d in pair['dimensions']: d.update(relation='UNKNOWN',claim_ids=[])
    pair['classification'] = 'DIFFERENT'
    with pytest.raises(ValidationError): PairComparison.model_validate(pair)
    pair['classification'] = None
    assert PairComparison.model_validate(pair).classification is None


def test_one_paper_failure_does_not_suppress_other_results(ready):
    ready = add_prior(ready)
    (ready[0]/'outside-paper'/'paper.json').unlink()
    result, chat = run(ready)
    assert [p.status for p in result.candidates[0].papers] == ['failed','complete']
    assert len(chat.calls) == 4


def test_passage_repair_identifies_bad_ids(ready):
    def mutate(out, req):
        if req['task'] == 'extract_comparison_evidence':
            out['record']['claims'][0]['passage_ids'] = ['bad-first','bad-second']
        if req['task'] == 'repair_comparison_evidence':
            assert 'bad-first' in req['validation_errors'] and 'bad-second' in req['validation_errors']
            assert 'c1' in req['validation_errors']
    result, _ = run(ready, Chat(mutate))
    assert result.candidates[0].papers[0].status == 'complete'


def test_one_review_format_repair_preserves_invalid_output_and_exact_draft(ready):
    def mutate(out, req):
        if req['task'] == 'review_comparison':
            out.update(decision='revise', issues=[{'category':'grounding','field_path':'/pairs/0/rationale',
                'explanation':'Bad ID fixture.', 'required_change':'Correct the claim.',
                'passage_ids':['not-a-real-passage']}])
        if req['task'] == 'repair_comparison_review':
            assert '/issues/0/passage_ids' in req['validation_errors']
            assert 'not-a-real-passage' in req['validation_errors']
            assert req['invalid_review']['decision'] == 'revise'
            out.update(decision='pass',issues=[],summary='Invalid original issue withdrawn after checking source.')
    result, _ = run(ready, Chat(mutate))
    p = result.candidates[0].papers[0]
    assert p.status == 'complete' and result.calls['repair_comparison_review'] == 1
    assert p.comparison == p.reviews[-1].draft
    assert len(p.reviews[-1].format_repairs) == 1
    assert p.reviews[-1].format_repairs[0].invalid_output['issues'][0]['passage_ids'] == ['not-a-real-passage']


def test_review_repair_uses_reviewer_client_and_obeys_attempt_budget(ready):
    root,generation,search = ready
    draft,review = Chat(),Chat()
    async def bad_review(**kw):
        await review(**kw)
        return ChatResult('{invalid json','stop','critic')
    runner=NoveltyComparator(PaperStore(root),draft,review_chat=bad_review,review_model='critic',settings=Settings(max_calls=3))
    result=asyncio.run(runner.run(generation,search))
    assert result.calls['total'] == 3
    assert len(draft.calls) == 1 and len(review.calls) == 2
    assert all(kw['model']=='critic' for _,kw in review.calls)
    p=result.candidates[0].papers[0]
    assert p.comparison is None and p.evidence_reviews[-1].error
    assert len(p.evidence_reviews[-1].format_repairs)==1
