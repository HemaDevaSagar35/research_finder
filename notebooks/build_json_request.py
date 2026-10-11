"""Reconstruct notebook 01's request offline from hash-checked run evidence."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reasoning.evidence import PaperStore
from novelty.comparison import page_numbers
from novelty.comparison_evidence import passages_for_pages, table_context_refs
from novelty.comparison_workflow import proposal_requirements
from novelty.comparison_records import EvidenceReport
from novelty.comparison_prompts import EVIDENCE_REVIEW
from novelty.schemas import TargetSignature


def main(run, example_name='01_invalid_json', output_name='01_model_request'):
    directory = ROOT / 'notebooks'
    example = json.loads((directory / 'examples' / f'{example_name}.json').read_text())
    provenance = example['provenance']
    checkpoint = json.loads((run / 'comparison.json').read_text())
    candidate = next(c for c in checkpoint['result']['candidates'] if c['direction_id'] == provenance['direction_id'])
    paper = next(p for p in candidate['papers'] if p['paper_id'] == provenance['paper_id'])
    search = json.loads((run / 'novelty_search.json').read_text())['result']
    sources = next(c for c in search['candidates'] if c['direction_id'] == candidate['direction_id'])
    settings = json.loads(run.with_suffix('.settings.json').read_text())
    store = PaperStore(Path(settings['root']))  # No S3 or network fallback.
    artifact = store.get(paper['paper_id'])
    assert artifact.status == 'loaded' and artifact.sha256 == paper['expected_artifact_sha256']
    prior_pages = []
    for ref in paper['context_pages']:
        page = store.page(ref['paper_id'], ref['page'])
        assert page is not None and page.sha256 == ref['sha256']
        prior_pages.append({'page_id': f'{page.paper_id}#p{page.page}', 'sha256': page.sha256, 'text': page.text})
    passages = passages_for_pages(prior_pages)
    assert [p.model_dump() for p in passages] == paper['passages']
    source_pages = []
    prior_ids = {p.page_id for p in passages}
    for ref in sorted(sources['context_pages'], key=lambda r: (r['paper_id'], r['page'])):
        page = store.page(ref['paper_id'], ref['page'])
        assert page is not None and page.sha256 == ref['sha256']
        page_id = f'{page.paper_id}#p{page.page}'
        if page_id not in prior_ids:
            source_pages.append({'page_id': page_id, 'text': page.text})
    _, referenced = page_numbers(store.root, paper['paper_id'], artifact.paper)
    targets = [TargetSignature.model_validate(t) for t in paper['targets']]
    payload = {'targets': paper['targets'], 'prior_paper_id': paper['paper_id'],
        'proposal_requirements': {t.target_id: proposal_requirements(t) for t in targets},
        'prior_artifact_sha256': artifact.sha256,
        'paper_navigation': {'title': artifact.paper.get('title'), 'available_pages': [r['page'] for r in paper['context_pages']], 'structured_referenced_pages': sorted(referenced)},
        'candidate_source_passages': [p.model_dump() for p in passages_for_pages(source_pages)],
        'prior_passages': paper['passages'], 'table_context_refs': table_context_refs(passages),
        'missing_referenced_pages': paper['missing_referenced_pages'], 'evidence_scope': 'available_extracted_pages'}
    record = example['record']
    body = {'task': 'review_comparison_evidence', 'schema': EvidenceReport.model_json_schema(),
        'payload': payload, 'record': record,
        'required_scope_claim_ids': [c['claim_id'] for c in record['claims'] if c['kind'] in ('result', 'evaluation', 'theoretical_result')]}
    review_index = int(provenance['source_pointer'].split('/evidence_reviews/')[1].split('/')[0])
    request = {'provenance': {**provenance,
        'request_authenticity': 'Reconstructed with current production prompt/schema and saved record/targets; source artifact and page hashes verified. Original wire request and environment-dependent sampling/output caps were not logged.'},
        'provider': 'deepseek', 'model': paper['evidence_reviews'][review_index]['model'],
        'max_tokens': settings['comparison']['max_output_tokens'], 'response_format': {'type': 'json_object'},
        'system_prompt': EVIDENCE_REVIEW + '\nReturn a JSON object matching the supplied schema.', 'user_input': body}
    (directory / 'examples' / f'{output_name}.json').write_text(json.dumps(request, indent=2, ensure_ascii=False) + '\n')
    print('Saved reconstructed request; all source hashes and prior passages verified.')
    return request


if __name__ == '__main__':
    main(Path(sys.argv[1]))
