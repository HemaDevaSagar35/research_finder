"""Bibliographic identity, source fallback, links, and immutable scientific output."""
import json
import pytest

from portfolio import build_portfolio, FinalPortfolio
from portfolio.render import markdown
from portfolio.references import public_url
from tests.test_direction_generator import inputs
from tests.test_novelty_search import prepared
from tests.test_novelty_comparison import ready
from tests.test_novelty_assessment import bundle
from tests.test_research_critic import source, run


def test_catalog_titles_authors_links_and_source_roles(source, tmp_path):
    critic, _ = run(source)
    pid = critic.novelty.inputs.generation.directions[0].opportunity.supporting_paper_ids[0]
    catalog = tmp_path/'metadata.json'
    catalog.write_text(json.dumps([
        dict(paper_id=pid, title='Real catalog title [with brackets]', authors=['A. Researcher'], year=2026,
             venue='Conference', forum_url='https://openreview.net/forum?id=example',
             pdf_url='https://openreview.net/pdf?id=example', folder='s3://bucket/artifacts/'+pid),
        dict(paper_id='outside-paper', title='Prior comparison', authors=['B. Author'],
             identifiers=[dict(name='DOI',value='10.1234/example')]),
        dict(paper_id='unrelated-paper', title='Not part of this candidate')]))
    plain = build_portfolio(critic)
    result = build_portfolio(critic, metadata_paths=[catalog])
    assert result.candidates == plain.candidates and result.ranking == plain.ranking
    refs = {r.paper_id:r for r in result.references}
    assert 'unrelated-paper' not in refs
    assert refs[pid].title == 'Real catalog title [with brackets]'
    assert refs[pid].authors == ['A. Researcher'] and refs[pid].year == 2026
    assert 'motivating_support' in refs[pid].roles
    assert refs[pid].provenance['title'] == str(catalog)
    assert refs['outside-paper'].url == 'https://doi.org/10.1234/example'
    assert refs['outside-paper'].roles == ['novelty_comparison']
    text = markdown(result)
    assert '[Real catalog title \\[with brackets\\]](<https://openreview.net/forum?id=example>)' in text
    assert 'A. Researcher' in text and 'https://openreview.net/pdf?id=example' in text
    assert 's3://bucket' not in text  # Storage location remains available in JSON.
    assert refs[pid].artifact_location == 's3://bucket/artifacts/'+pid
    assert FinalPortfolio.model_validate_json(result.model_dump_json()) == result


def test_extracted_metadata_fallback_and_missing_reference(source):
    critic, _ = run(source)
    result = build_portfolio(critic)
    by_id = {r.paper_id:r for r in result.references}
    artifact = critic.candidates[0].packet['support_context']['paper_artifacts'][0]
    assert by_id[artifact['paper_id']].title == artifact['paper']['paper_metadata']['title']
    assert by_id['outside-paper'].title is None
    assert 'title' in by_id['outside-paper'].missing_fields
    assert 'Title unavailable (outside-paper)' in markdown(result)


def test_jsonl_fallback_and_conflicting_metadata_are_explicit(source, tmp_path):
    critic, _ = run(source)
    pid = critic.novelty.inputs.generation.directions[0].opportunity.paper_ids[0]
    first, second = tmp_path/'first.json', tmp_path/'second.jsonl'
    first.write_text(json.dumps({pid: dict(title='First title')}))
    second.write_text(json.dumps(dict(paper_id=pid,title='Second title', authors=['Author'],year=2026,
                                     arxiv_id='2601.12345v2'))+'\n')
    result = build_portfolio(critic, metadata_paths=[first,second])
    ref = next(r for r in result.references if r.paper_id == pid)
    assert ref.title == 'First title'
    assert 'Second title' in ref.conflicts['title']
    assert ref.url == 'https://arxiv.org/abs/2601.12345v2'
    assert ref.authors == ['Author']


@pytest.mark.parametrize('tamper', ['title','url','snapshot'])
def test_saved_bibliography_rejects_inconsistent_edits(source, tamper):
    critic, _ = run(source)
    data = build_portfolio(critic).model_dump()
    if tamper == 'snapshot': data['metadata_inputs_sha256'] = 'changed'
    else: data['references'][0][tamper] = 'changed'
    with pytest.raises(ValueError): FinalPortfolio.model_validate(data)


def test_v1_report_still_loads(source):
    critic, _ = run(source)
    data = build_portfolio(critic).model_dump()
    data['schema_version'] = 'final_portfolio_v1'
    for key in ('references','metadata_inputs','metadata_inputs_sha256'): data.pop(key)
    old = FinalPortfolio.model_validate(data)
    assert old.schema_version == 'final_portfolio_v1'
    assert 'Paper references' in markdown(old)


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///secret', 's3://private/paper',
    'https://user:password@example.org/paper', 'https://example.org/pdf?X-Amz-Signature=token'])
def test_nonpublic_or_credential_links_are_not_paper_links(url):
    assert public_url(url) is None


def test_extracted_prior_title_requires_matching_artifact(source):
    from reasoning.evidence import PaperStore
    critic, _ = run(source)
    with_store = build_portfolio(critic, store=PaperStore(source[0]))
    assert next(r for r in with_store.references if r.paper_id == 'outside-paper').title
    path = source[0]/'outside-paper'/'paper.json'
    data = json.loads(path.read_text())
    data['paper_metadata']['title'] = 'A changed artifact must not supply metadata'
    path.write_text(json.dumps(data))
    changed = build_portfolio(critic, store=PaperStore(source[0]))
    assert next(r for r in changed.references if r.paper_id == 'outside-paper').title is None
