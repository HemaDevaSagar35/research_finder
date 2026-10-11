"""The saved live failures motivating v2: quote copying and candidate paraphrase drift."""
import json
import pytest
from pydantic import ValidationError
from novelty.comparison_evidence import passages_for_pages, candidate_view, validate_comparison
from novelty.comparison_schemas import Passage, ComparisonDraft, NoveltyComparisonResult
from tests.test_novelty_comparison import ready, run, output
from tests.test_novelty_search import prepared
from tests.test_direction_generator import inputs


def test_passage_addressing_preserves_all_nonwhitespace_source_characters():
    text='  # Heading\n\n**Method:** $\\hat{Z} \\leq Z$.\n\n| Metric | Value |\n| -- | -- |\n| α | 0.5 |\n\nSame words.\n\nSame words.  '
    pages=[{'page_id':'P#p1','text':text}]
    refs=passages_for_pages(pages)
    assert len(refs)==5 and len({p.passage_id for p in refs})==5
    assert refs==passages_for_pages(pages)
    for p in refs: assert text[p.start:p.end]==p.text
    covered={i for p in refs for i in range(p.start,p.end)}
    assert all(i in covered for i,char in enumerate(text) if not char.isspace())
    changed=passages_for_pages([{'page_id':'P#p1','text':text+'x'}])
    assert not {p.passage_id for p in refs} & {p.passage_id for p in changed}


@pytest.mark.parametrize('field,value',[('text','fabricated'),('page_id','OTHER#p1'),('page_sha256','changed'),('start',99)])
def test_passage_identity_binds_page_version_offsets_and_text(field,value):
    passage=passages_for_pages([{'page_id':'P#p1','text':'Exact source text'}])[0].model_dump()
    passage[field]=value
    with pytest.raises(ValidationError):Passage.model_validate(passage)


def test_candidate_unknowns_and_hedges_are_code_owned_and_unchanged(ready):
    root,generation,search=ready
    c=search.candidates[0]
    facet=c.signature.targets[0].mechanism[0]
    facet.text='If condition X holds, the intervention may help; whether X holds is unknown.'
    facet.basis='unknown'
    c.reviews[-1].signature=c.signature.model_copy(deep=True)
    result,_=run(ready)
    paper=result.candidates[0].papers[0]
    assert paper.targets==c.signature.targets
    view=paper.evidence_view()['pairs'][0]['candidate']
    assert view['target']==c.signature.targets[0].model_dump()
    assert view['unknowns'][0]['facet']==facet.model_dump()
    assert view['dimensions']['mechanism'][0]['facet']['text']==facet.text
    assert 'candidate_statement' not in paper.comparison.model_dump_json()
    # Persisted handoff rejects changing the candidate even after a judge pass.
    data=result.model_dump();data['candidates'][0]['papers'][0]['targets'][0]['mechanism'][0]['text']='It helps.'
    with pytest.raises(ValidationError):NoveltyComparisonResult.model_validate(data)


def test_code_resolves_exact_quotations_and_wrong_paper_reference_rejected(ready):
    result,chat=run(ready)
    p=result.candidates[0].papers[0]
    view=p.evidence_view()
    source=view['claims']['c1']['sources'][0]
    original=(ready[0]/'outside-paper'/'01.md').read_text()
    assert source['text']==original[source['start']:source['end']]
    assert 'quote' not in p.comparison.model_dump_json()
    data=p.comparison.model_dump()
    foreign=passages_for_pages([{'page_id':'OTHER#p1','text':original}])[0]
    data['claims'][0]['passage_ids']=[foreign.passage_id]
    with pytest.raises(ValueError,match='wrong-paper'):
        validate_comparison(ComparisonDraft.model_validate(data),chat.calls[0][0]['payload'])


def test_discussion_is_not_structural_proof_of_a_test(ready):
    _,chat=run(ready)
    data=output(chat.calls[0][0]['payload'])['comparison']
    data['claims'][0]['kind']='discussion'
    data['pairs'][1]['hypothesis_tested']='tested'
    data['pairs'][1]['hypothesis_claim_ids']=['c1']
    with pytest.raises(ValidationError,match='reported empirical/theoretical result'):ComparisonDraft.model_validate(data)
    data['pairs'][1]['hypothesis_tested']='discussed_only'
    assert ComparisonDraft.model_validate(data)


def test_unknown_and_explicit_noncoverage_remain_distinct(ready):
    _,chat=run(ready)
    data=output(chat.calls[0][0]['payload'])['comparison']
    data['claims'][0]['kind']='explicit_noncoverage'
    draft=ComparisonDraft.model_validate(data)
    assert draft.claims[0].kind=='explicit_noncoverage'
    assert all(p.hypothesis_tested=='not_established' for p in draft.pairs)
    for p in data['pairs']:
        p['classification']=None
        for d in p['dimensions']:d.update(relation='UNKNOWN',claim_ids=[])
    data['claims']=[]
    validate_comparison(ComparisonDraft.model_validate(data),chat.calls[0][0]['payload'])


def test_reviewer_cannot_rewrite_the_candidate_or_invent_quotes(ready):
    from tests.test_novelty_comparison import Chat
    def mutate(out,req):
        if req['task'] in ('review_comparison','repair_comparison_review'):
            out.update(decision='revise',issues=[{'category':'consistency','field_path':'/candidate_views/0/unknowns',
                'explanation':'Rewrite candidate.', 'required_change':'Drop uncertainty.','passage_ids':[]}])
    result,_=run(ready,Chat(mutate))
    assert result.candidates[0].papers[0].comparison is None
    assert result.candidates[0].papers[0].reviews[-1].error
