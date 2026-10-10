"""Bibliography resolved by exact paper ID from saved evidence and local catalogs."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import Field
from directions.schemas import Strict, Text
from opportunities.miner import digest

FIELDS = ('title', 'authors', 'year', 'venue', 'identifiers', 'forum_url', 'pdf_url',
          'doi', 'arxiv_id', 'openreview_id', 'url', 'paper_url', 'landing_page_url', 'folder', 's3_url')


class MetadataSnapshot(Strict):
    paper_id: Text
    source: Text
    source_sha256: Text
    metadata: dict


class PaperReference(Strict):
    paper_id: Text
    title: str | None
    authors: list[str]
    year: int | None
    venue: str | None
    url: str | None
    pdf_url: str | None
    artifact_location: str | None
    roles: list[str]
    candidate_ids: list[str]
    pages: list[int]
    provenance: dict[str, str]
    conflicts: dict[str, list] = Field(default_factory=dict)
    missing_fields: list[str]


def clean(value):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split())
    return None if value.casefold() in ('', 'unknown', 'n/a', 'none', 'not specified', 'not reported') else value


def public_url(value):
    value = clean(value)
    if not value or any(ord(ch) < 32 for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
        # Do not export temporary signed object-storage credentials as paper links.
        if any(x in parsed.query.lower() for x in ('x-amz-', 'awsaccesskeyid=', 'signature=')):
            return None
    except ValueError:
        return None
    return value.replace(' ', '%20')


def identifiers(meta):
    doi, arxiv = meta.get('doi'), meta.get('arxiv_id')
    for identifier in meta.get('identifiers') or []:
        if not isinstance(identifier, dict):
            continue
        name = str(identifier.get('name', '')).casefold()
        if name == 'doi': doi = doi or identifier.get('value')
        if name in ('arxiv', 'arxiv_id', 'arxiv id'): arxiv = arxiv or identifier.get('value')
    links = []
    if clean(doi):
        value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', doi.strip(), flags=re.I)
        if re.fullmatch(r'10\.\d{4,9}/\S+', value):
            links.append('https://doi.org/' + quote(value, safe='/():;.-_'))
    if clean(arxiv):
        value = re.sub(r'^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)', '', arxiv.strip(), flags=re.I)
        value = value.removesuffix('.pdf')
        if re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?', value):
            links.append('https://arxiv.org/abs/' + value)
    oid = clean(meta.get('openreview_id'))
    if oid and re.fullmatch(r'[\w-]+', oid):
        links.append('https://openreview.net/forum?id=' + oid)
    return links


def inventory(critic):
    """All cited motivation and compared prior papers, including pending candidates."""
    result = {}
    def add(pid, did, role, pages=()):
        row = result.setdefault(pid, dict(roles=set(), candidate_ids=set(), pages=set()))
        row['roles'].add(role)
        row['candidate_ids'].add(did)
        row['pages'].update(pages)
    for direction in critic.novelty.inputs.generation.directions:
        for evidence in direction.opportunity.evidence:
            role = 'motivating_support' if evidence.paper_id in direction.opportunity.supporting_paper_ids else 'context'
            pages = [s.page for s in direction.context_pages if s.paper_id == evidence.paper_id]
            add(evidence.paper_id, direction.direction_id, role, pages)
    for candidate in critic.novelty.inputs.comparisons.candidates:
        for paper in candidate.papers:
            add(paper.paper_id, candidate.direction_id, 'novelty_comparison', [p.page for p in paper.context_pages])
    return result


def embedded_snapshots(critic):
    result = []
    for candidate in critic.candidates:
        if candidate.packet:
            for row in candidate.packet['support_context']['paper_artifacts']:
                result.append(MetadataSnapshot(paper_id=row['paper_id'], source='reviewed_support_artifact',
                    source_sha256=row['sha256'], metadata=row['paper'].get('paper_metadata') or {}))
    return result


def collect_metadata(critic, paths=(), store=None):
    """Snapshot only referenced records. Never match titles or search the web."""
    wanted = inventory(critic)
    snapshots = []
    for path in paths:
        path = Path(path)
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open() as stream:
            if path.suffix == '.jsonl':
                records = (json.loads(line) for line in stream if line.strip())
            else:
                data = json.load(stream)
                if isinstance(data, list): records = data
                elif isinstance(data, dict):
                    records = [dict(row, paper_id=row.get('paper_id', key)) for key, row in data.items() if isinstance(row, dict)]
                else: raise ValueError('Metadata catalog must be a list or paper-ID mapping')
            for row in records:
                if row.get('paper_id') in wanted:
                    snapshots.append(MetadataSnapshot(paper_id=row['paper_id'], source=str(path.resolve()),
                        source_sha256=fingerprint, metadata={k: row[k] for k in FIELDS if k in row}))
    if store is not None:
        expected = {p.paper_id: p.expected_artifact_sha256 for c in critic.novelty.inputs.comparisons.candidates for p in c.papers}
        for direction in critic.novelty.inputs.generation.directions:
            expected.update(direction.paper_artifact_hashes)
        for pid in sorted(wanted):
            row = store.get(pid)
            if row.status == 'loaded' and row.sha256 == expected.get(pid):
                snapshots.append(MetadataSnapshot(paper_id=pid, source='matching_extracted_artifact',
                    source_sha256=row.sha256, metadata=row.paper.get('paper_metadata') or {}))
    return snapshots


def bibliography(critic, extra):
    wanted = inventory(critic)
    if any(row.paper_id not in wanted for row in extra):
        raise ValueError('Bibliography metadata references a paper outside the source artifact')
    # Explicit/catalog metadata has priority; reviewed extraction fills gaps.
    records = [*extra, *embedded_snapshots(critic)]
    refs = []
    for pid, usage in sorted(wanted.items()):
        rows = [r for r in records if r.paper_id == pid]
        values, provenance, conflicts = {}, {}, {}
        for field in ('title', 'authors', 'year', 'venue', 'url', 'pdf_url', 'artifact_location'):
            found = []
            for row in rows:
                meta = row.metadata
                if field == 'authors':
                    raw = meta.get(field)
                    value = [clean(a) for a in raw if clean(a)] if isinstance(raw, list) else []
                elif field == 'year':
                    raw = meta.get(field)
                    value = int(raw) if isinstance(raw, (str, int)) and re.fullmatch(r'\d{4}', str(raw)) else None
                elif field == 'url':
                    links = [public_url(meta.get(k)) for k in ('forum_url','paper_url','landing_page_url','url')]
                    links += identifiers(meta)
                    value = next((x for x in links if x), None)
                elif field == 'pdf_url': value = public_url(meta.get('pdf_url'))
                elif field == 'artifact_location': value = clean(meta.get('folder')) or clean(meta.get('s3_url'))
                else: value = clean(meta.get(field))
                if value and value not in [v for v, _ in found]:
                    found.append((value, row.source))
            values[field] = found[0][0] if found else ([] if field == 'authors' else None)
            if found: provenance[field] = found[0][1]
            if len(found) > 1 and field in ('title','authors','year','venue'):
                conflicts[field] = [v for v, _ in found]
        if not values['url'] and values['pdf_url']:
            values['url'] = values['pdf_url']
            provenance['url'] = provenance['pdf_url']
        refs.append(PaperReference(paper_id=pid, **values, **{k: sorted(v) for k,v in usage.items()},
            provenance=provenance, conflicts=conflicts,
            missing_fields=[k for k in ('title','authors','year','venue','url') if not values[k]]))
    return refs
