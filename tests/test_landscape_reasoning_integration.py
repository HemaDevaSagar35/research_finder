"""Real builder orchestration → serialized shared contract → real reasoner.

Only model-dependent builder stages and the reasoner's chat calls are faked.
"""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from landscape import builder
from landscape.evidence import resolve_references
from landscape.extraction import RawExtraction, RawMention, RawTriple
from landscape.paper_context import PaperContext, _project
from landscape.schemas import AggregatedItem, Landscape
from reasoning.cross_paper import Budgets, CrossPaperReasoner, schedule_threads
from reasoning.evidence import PaperStore
from reasoning.fake import FakeChat
from reasoning.fixtures import build_demo_corpus, _loc
from reasoning.schemas import load_landscape


@pytest.fixture
def built(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    _, ids = build_demo_corpus(root)
    contexts, extractions = {}, {}
    for short, pid in ids.items():
        paper = json.loads((root / pid / "paper.json").read_text())
        card, missing = _project(pid, paper)
        quote = card.findings[0]
        page = 2 if short != "P3" else 3
        contexts[pid] = PaperContext(
            paper_id=pid, card=card, missing_fields=missing,
            matched_records=[{"record_id": f"{pid}#result", "text": quote,
                              "source_locations": [_loc(page)]}])
        mentions = [RawMention(concept="expert caching", facet="method", evidence_quote=quote),
                    RawMention(concept="transfer latency", facet="bottleneck", evidence_quote=quote)]
        triples = []
        if short != "P4":
            triples.append(RawTriple(source_concept="expert caching",
                                     relation="HURTS" if short == "P3" else "REDUCES",
                                     target_concept="transfer latency", evidence_quote=quote))
        else:
            mentions.append(RawMention(concept="offloading", facet="method", evidence_quote=quote))
        extractions[pid] = RawExtraction(mentions=mentions, triples=triples)

    async def normalize(labels, **kwargs):
        return {label: label.text.replace(" ", "_") for label in labels}

    monkeypatch.setattr(builder, "extract_from_papers", AsyncMock(return_value=extractions))
    monkeypatch.setattr(builder, "normalize_concepts", normalize)
    monkeypatch.setattr(builder, "aggregate_findings", AsyncMock(return_value=(
        [AggregatedItem(statement="Caching reduces transfers.", supporting_papers=[ids['P1'], ids['P2']])],
        [AggregatedItem(statement="Hardware coverage is limited.", supporting_papers=[ids['P4']])],
        [AggregatedItem(statement="Routing has locality.", supporting_papers=[ids['P1']])],
    )))
    land = asyncio.run(builder.build_landscape("Efficient MoE inference", contexts))
    return root, land, ids


def test_builder_output_runs_through_reasoner(built, tmp_path):
    root, land, ids = built
    path = tmp_path / "landscape.json"
    path.write_text(land.model_dump_json())
    loaded = load_landscape(path)
    assert isinstance(loaded, Landscape)
    assert loaded == land
    chat = FakeChat()
    reasoner = CrossPaperReasoner(PaperStore(root), chat, provider="fake",
                                  draft_model="fake", review_model="fake")
    out = asyncio.run(reasoner.run(loaded))
    assert out.findings and out.observations and out.tensions
    assert out.coverage.threads['failed'] == 0
    assert out.diagnostics == []
    assert out.coverage.threads['completed'] == 7
    known_ids = {x.item_id for name in (
        'relationships', 'aggregated_findings', 'recurring_limitations',
        'common_assumptions', 'contradictions', 'underexplored_regimes')
                 for x in getattr(land, name)}
    for result in out.findings + out.observations + out.tensions:
        assert set(result.refines) <= known_ids
        assert result.review_sources
        for source in result.review_sources:
            assert source.sha256 == hashlib.sha256(
                (root / source.paper_id / f'{source.page:02d}.md').read_bytes()).hexdigest()
    assert out.landscape_ref == {
        'schema_version': land.schema_version,
        'sha256': hashlib.sha256(json.dumps(land.model_dump(), sort_keys=True).encode()).hexdigest()}


def test_record_locations_keep_their_paper(built):
    _, land, ids = built
    for rel in land.relationships:
        assert {e.paper_id for e in rel.evidence} == set(rel.supporting_papers)
        for ref in rel.evidence:
            assert ref.record_id == f'{ref.paper_id}#result'
            assert ref.source_locations[0].page == (3 if ref.paper_id == ids['P3'] else 2)
    refs = resolve_references('P1', 'shared quote', [
        {'record_id': 'r1', 'text': 'shared quote', 'source_locations': [_loc(1)]},
        {'record_id': 'r2', 'text': 'shared quote', 'source_locations': [_loc(2)]}])
    assert [(r.record_id, r.source_locations[0].page) for r in refs] == [('r1', 1), ('r2', 2)]
    assert resolve_references('P1', 'absent', []) == []


def test_ids_stable_under_reordering_and_json_roundtrip(built):
    _, land, _ = built
    data = land.model_dump()
    expected = {name: sorted(x['item_id'] for x in data[name]) for name in (
        'relationships', 'aggregated_findings', 'recurring_limitations',
        'common_assumptions', 'contradictions', 'underexplored_regimes')}

    def reorder(node):
        if isinstance(node, dict):
            node.pop('item_id', None)
            for v in node.values():
                reorder(v)
        elif isinstance(node, list):
            node.reverse()
            for v in node:
                reorder(v)
    reorder(data)
    rebuilt = Landscape.model_validate(data)
    for name, values in expected.items():
        assert sorted(x.item_id for x in getattr(rebuilt, name)) == values


def test_statement_papers_are_not_expanded_to_unrelated_groups(built):
    _, land, ids = built
    threads = schedule_threads(land, Budgets())
    by_kind = {t.kind: t for t in threads}
    assert set(by_kind['aggregated_finding'].papers) == {ids['P1'], ids['P2']}
    assert by_kind['aggregated_finding'].group_ids == []
    assert by_kind['recurring_limitation'].papers == [ids['P4']]
    assert 'selected landscape corpus' in by_kind['underexplored_regime'].statement
    assert '1 paper mentions' in by_kind['underexplored_regime'].statement


@pytest.mark.parametrize('cap', [1, 3, 12])
def test_shared_contradictions_respect_budget(built, cap):
    _, land, _ = built
    threads = schedule_threads(land, Budgets(max_papers_per_thread=cap))
    assert all(len(t.papers) <= cap for t in threads)
    contradiction = next(t for t in threads if t.kind == 'contradiction')
    if cap == 1:
        assert contradiction.status == 'insufficient_for_adjudication'
    else:
        assert all(side['paper_ids'] for side in contradiction.sides)


@pytest.mark.parametrize('bad', ['paper', 'concept', 'evidence', 'id', 'contradiction', 'count'])
def test_invalid_references_rejected(built, bad):
    _, land, _ = built
    data = land.model_dump()
    if bad == 'paper':
        data['aggregated_findings'][0]['supporting_papers'].append('unknown')
    elif bad == 'concept':
        data['relationships'][0]['source'] = 'unknown'
    elif bad == 'evidence':
        data['relationships'][0]['evidence'][0]['paper_id'] = 'unknown'
    elif bad == 'id':
        data['common_assumptions'][0]['item_id'] = data['aggregated_findings'][0]['item_id']
    elif bad == 'contradiction':
        data['contradictions'][0]['a']['supporting_papers'] = []
    else:
        data['underexplored_regimes'][0]['mention_count'] = 123
    with pytest.raises(ValidationError):
        Landscape.model_validate(data)
