"""Contracts (Phase 0) and deterministic evidence access (Phase 1)."""
import hashlib
import json

import pytest
from pydantic import ValidationError

from reasoning import schemas as S
from reasoning.evidence import (BundleBudget, PaperStore, bundle, candidates, paper_card)
from reasoning.fixtures import demo_landscape


# ------------------------------------------------------------------ landscape

def test_demo_landscape_validates(corpus):
    _, land, _ = corpus
    assert S.landscape_errors(land) == []
    S.validate_landscape(land)


def test_landscape_rejects_paper_outside_inventory(corpus):
    _, land, ids = corpus
    bad = land.model_copy(deep=True)
    bad.items[0].supporting.append(S.PaperRef(paper_id="deadbeef0000"))
    errs = S.landscape_errors(bad)
    assert any("not in inventory" in e for e in errs)
    with pytest.raises(S.LandscapeValidationError):
        S.validate_landscape(bad)


def test_landscape_rejects_unknown_group_and_empty_group_ids(corpus):
    _, land, _ = corpus
    bad = land.model_copy(deep=True)
    bad.items[0].group_ids = ["g_nope"]
    bad.items[1].group_ids = []
    bad.relationships[0].target_group_id = "g_nope"
    errs = S.landscape_errors(bad)
    assert sum("unknown group" in e for e in errs) == 2
    assert any("group_ids is empty" in e for e in errs)


def test_landscape_rejects_duplicate_item_ids_and_missing_inventory(corpus):
    _, land, _ = corpus
    bad = land.model_copy(deep=True)
    bad.relationships[0].item_id = bad.items[0].item_id
    assert any("duplicate item_id" in e for e in S.landscape_errors(bad))
    with pytest.raises(ValidationError):           # group_ids is required by schema
        S.LandscapeItem(item_id="x", kind="aggregated_finding", statement="s", supporting=[])
    with pytest.raises(ValidationError):           # no inventory reconstruction
        S.LegacyLandscape.model_validate({"schema_version": "landscape_v1", "topic": "t",
                                    "groups": [], "items": []})


def test_json_schemas_export():
    for m in (S.Landscape, S.LegacyLandscape, S.EvidenceBundle, S.ThreadReasoningDraft,
              S.SupportReviewRequest, S.SupportReviewResponse, S.CrossPaperReasoning):
        json.dumps(m.model_json_schema())


# ------------------------------------------------------------------ draft / review validators

def _bundle():
    loc = [{"page": 1, "section": None, "table": None, "figure": None, "equation": None,
            "appendix": None}]
    items = [S.BundleItem(evidence_id=f"t-e{i}", paper_id=f"p{i % 2}", label="key result",
                          value_path=f"/key_results/{i}", provenance_path=f"/key_results/{i}",
                          source_locations=loc, source_value=f"v{i}") for i in range(3)]
    return S.EvidenceBundle(thread_id="t", statement="s", thread_papers=["p0", "p1"], items=items)


def test_draft_errors_categorise_citation_vs_structural():
    b = _bundle()
    d = S.ThreadReasoningDraft(thread_id="t", outcome="candidates", findings=[
        S.DraftFinding(statement="a", finding_kind="confirms", evidence_ids=["t-e0", "bogus"],
                       stance_by_evidence={"t-e0": "supports", "bogus": "supports"},
                       conditions=[S.DraftCondition(text="c", evidence_ids=["t-e2"])])])
    errs = S.draft_errors(d, b)
    cats = {cat for cat, _ in errs}
    assert cats == {S.CITATION}
    assert len(errs) == 2                       # unknown id + condition outside evidence
    d2 = S.ThreadReasoningDraft(thread_id="t", outcome="candidates", findings=[
        S.DraftFinding(statement="a", finding_kind="confirms", evidence_ids=["t-e0"],
                       stance_by_evidence={"t-e1": "supports"})])
    assert any(cat == S.STRUCTURAL for cat, _ in S.draft_errors(d2, b))
    d3 = S.ThreadReasoningDraft(thread_id="t", outcome="candidates", tensions=[
        S.DraftTension(statement="x", sides=[S.DraftSide(assertion="a", evidence_ids=["t-e0"])])])
    assert any("two sides" in m for _, m in S.draft_errors(d3, b))


def test_review_errors_unknown_missing_duplicate_and_page_ids():
    req = S.SupportReviewRequest(thread_id="t", candidates=[
        S.ReviewCandidate(candidate_id="c1", kind="finding", payload={}),
        S.ReviewCandidate(candidate_id="c2", kind="tension", payload={})],
        pages=[S.ReviewPage(page_id="p#p1", paper_id="p", page=1, sha256="x", text="t")])
    ok = S.SupportReviewResponse(decisions=[
        S.ReviewDecision(candidate_id="c1", decision="accept", page_ids=["p#p1"]),
        S.ReviewDecision(candidate_id="c2", decision="reject")])
    assert S.review_errors(ok, req) == []
    bad = S.SupportReviewResponse(decisions=[
        S.ReviewDecision(candidate_id="c1", decision="accept", page_ids=["q#p3"]),
        S.ReviewDecision(candidate_id="c1", decision="accept"),
        S.ReviewDecision(candidate_id="c9", decision="accept")])
    errs = S.review_errors(bad, req)
    assert any("duplicate" in e for e in errs)
    assert any("unknown candidate_id c9" in e for e in errs)
    assert any("missing decision for c2" in e for e in errs)
    assert any("unknown page_id q#p3" in e for e in errs)


# ------------------------------------------------------------------ PaperStore

def test_paper_store_statuses_and_hashes(corpus):
    root, land, ids = corpus
    store = PaperStore(root)
    p1 = store.get(ids["P1"])
    assert p1.status == "loaded"
    assert p1.sha256 == hashlib.sha256((root / ids["P1"] / "paper.json").read_bytes()).hexdigest()
    assert store.get("000000000000").status == "missing"
    (root / "badpaper").mkdir()
    (root / "badpaper" / "paper.json").write_text("{not json")
    assert store.get("badpaper").status == "invalid"
    (root / "badschema").mkdir()
    (root / "badschema" / "paper.json").write_text('{"schema_version": "paper_analysis_v1"}')
    assert store.get("badschema").status == "invalid"
    page = store.page(ids["P1"], 2)
    assert page is not None and "Table 1" in page.text
    assert page.sha256 == hashlib.sha256((root / ids["P1"] / "02.md").read_bytes()).hexdigest()
    assert store.page(ids["P1"], 9) is None
    assert store.page("000000000000", 1) is None


def test_paper_store_never_builds_s3_client_when_local(corpus):
    root, _, ids = corpus
    calls = []
    store = PaperStore(root, store_factory=lambda: calls.append(1))
    store.get(ids["P1"])
    store.page(ids["P1"], 1)
    assert calls == []                          # lazy: local hit, no S3 construction


# ------------------------------------------------------------------ paper_card / candidates

def test_paper_card_projection(corpus):
    root, _, ids = corpus
    paper = json.loads((root / ids["P1"] / "paper.json").read_text())
    card = paper_card(ids["P1"], paper)
    assert card["paper_id"] == ids["P1"]
    assert card["title"].startswith("Locality-Aware")
    assert card["evaluation"]["hardware"] == ["1x NVIDIA A100 80GB", "PCIe 4.0"]
    assert card["findings"]["main_results"]
    assert card["boundaries"]["limitations_author"]
    assert card["claims"][0]["claim_type"] == "efficiency"


def test_candidates_result_rows_inherit_experiment_provenance_and_context(corpus):
    root, _, ids = corpus
    paper = json.loads((root / ids["P1"] / "paper.json").read_text())
    cs = candidates(paper, ids["P1"])
    rows = [c for c in cs if c.label == "experiment result"]
    assert rows, "result rows must be candidates"
    r = rows[0]
    assert r.value_path == "/experiments/0/results/0"
    assert r.provenance_path == "/experiments/0"
    assert r.source_locations == paper["experiments"][0]["source_locations"]
    assert "hardware: 1x NVIDIA A100 80GB" in r.context
    assert "metric definition: per-token decode latency" in r.context
    assert "23.0" in r.source_value and "ms/token" in r.source_value
    labels = {c.label for c in cs}
    assert "author-stated limitation" in labels
    assert all(c.boundary for c in cs if "limitation" in c.label or c.label == "failure case")
    setup = next(c for c in cs if c.label == "evaluation setup")
    assert "A100" in setup.source_value


def test_candidates_keep_inferred_and_author_limitations_distinct(corpus):
    root, _, ids = corpus
    paper = json.loads((root / ids["P2"] / "paper.json").read_text())
    cs = candidates(paper, ids["P2"])
    inferred = [c for c in cs if c.label.startswith("extractor-inferred limitation")]
    assert inferred and "severity medium" in inferred[0].label
    assert inferred[0].value_path == "/limitations/inferred/0"


def test_bundle_reserves_boundary_and_records_omissions(corpus):
    root, _, ids = corpus
    cands = {}
    for k in ("P1", "P3"):
        cands[ids[k]] = candidates(json.loads((root / ids[k] / "paper.json").read_text()), ids[k])
    tiny = BundleBudget(max_chars=900, boundary_share=0.5, max_item_chars=120)
    b = bundle("t007", "expert caching latency", [ids["P1"], ids["P3"]], cands,
               "expert caching reduces transfer latency", tiny)
    assert b.items and b.omitted
    assert all(i.evidence_id.startswith("t007-e") for i in b.items)
    assert len({i.evidence_id for i in b.items}) == len(b.items)
    assert any(i.boundary for i in b.items), "boundary reserve must admit a limitation/failure"
    assert any(i.truncated for i in b.items)
    assert all(len(i.source_value) + len(i.context) <= 120 for i in b.items)
    # deterministic
    b2 = bundle("t007", "expert caching latency", [ids["P1"], ids["P3"]], cands,
                "expert caching reduces transfer latency", tiny)
    assert [i.value_path for i in b.items] == [i.value_path for i in b2.items]


def test_demo_landscape_ids_are_canonical(corpus):
    _, land, ids = corpus
    assert set(land.paper_ids) == set(ids.values())
    assert demo_landscape(ids).model_dump() == land.model_dump()
