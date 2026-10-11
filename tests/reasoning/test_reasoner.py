"""End-to-end behaviour of the reasoner against the acceptance table in
docs/cross_paper_reasoner.md, using FakeChat variants instead of a provider."""
import hashlib
import json

import httpx
import openai

from reasoning import schemas as S
from reasoning.cross_paper import Budgets
from reasoning.fake import FakeChat, cooperative_draft, cooperative_review
from reasoning.fixtures import build_demo_corpus

from .conftest import draft, finding, items_of, make_reasoner, run


def _by_reason(out):
    return out.coverage.candidates["by_reason"]


# ------------------------------------------------------------------ happy path

def test_happy_path_accepts_findings_observations_tensions(corpus):
    root, land, ids = corpus
    chat = FakeChat()
    r = make_reasoner(root, chat)
    out = run(r, land)
    S.CrossPaperReasoning.model_validate(out.model_dump())          # output contract holds
    assert out.findings and out.tensions
    assert out.coverage.threads["completed"] == 6
    assert out.coverage.calls == {"draft": 6, "review": 6, "total": 12, "budget": None}
    assert out.diagnostics == []
    f = out.findings[0]
    assert f.verification == "verified"
    assert f.review_sources, "accepted items carry the reviewed pages"
    for src in f.review_sources:
        assert src.sha256 == hashlib.sha256(
            (root / src.paper_id / f"{src.page:02d}.md").read_bytes()).hexdigest()
    assert {e.paper_id for e in f.evidence} >= set(f.agreement.paper_ids) - {
        p for p, in zip([])}  # evidence papers ⊆ thread papers
    assert set(e.paper_id for e in f.evidence) <= set(f.agreement.paper_ids)
    assert sum(f.agreement.counts.model_dump().values()) == f.agreement.denominator
    assert all(e.source_kind == "paper_json" for e in f.evidence)
    assert all(e.summary.endswith(e.source_value) for e in f.evidence)
    t = out.tensions[0]
    assert len(t.sides) == 2 and all(s.evidence for s in t.sides) and t.distinct_papers >= 2
    assert any(x.hypothesis for x in t.candidate_explanations)   # "hardware differs" had no ids
    assert out.run.token_counter.endswith("(estimate)")
    assert out.landscape_ref["sha256"]


def test_single_paper_finding_becomes_observation_without_confidence(corpus):
    root, land, ids = corpus

    def one_paper(payload):
        mine = items_of(payload, ids["P1"])
        if not mine:
            return draft(payload, outcome="insufficient_evidence")
        return draft(payload, findings=[finding([e["evidence_id"] for e in mine])])

    out = run(make_reasoner(root, FakeChat(draft_fn=one_paper)), land)
    assert out.findings == []
    assert out.observations
    assert not hasattr(out.observations[0], "confidence")
    assert out.observations[0].verification == "verified"


# ------------------------------------------------------------------ citations

def test_unknown_citation_rejects_only_that_candidate(corpus):
    root, land, ids = corpus

    def bad_then_good(payload):
        ids_ = [e["evidence_id"] for e in payload["evidence"]]
        return draft(payload, findings=[finding(ids_[:2] + ["bogus-id"]), finding(ids_)])

    chat = FakeChat(draft_fn=bad_then_good)
    out = run(make_reasoner(root, chat), land)
    assert _by_reason(out).get("invalid_citation") == 6            # one per thread
    assert out.findings, "the well-cited candidate still proceeds"
    assert out.coverage.calls["repair"] == 6                         # one repair round each
    assert out.coverage.citations["invalid"] == 6
    bad = [d for d in out.diagnostics if d.reason == "invalid_citation"][0]
    assert "bogus-id" in bad.detail and bad.payload_snapshot["evidence_ids"]


def test_condition_citing_id_outside_finding_is_invalid_citation(corpus):
    root, land, _ = corpus

    def cond_bad(payload):
        ids_ = [e["evidence_id"] for e in payload["evidence"]]
        return draft(payload, findings=[
            finding(ids_[:2], conditions=[{"text": "c", "evidence_ids": [ids_[-1]]}])])

    out = run(make_reasoner(root, FakeChat(draft_fn=cond_bad)), land)
    assert _by_reason(out) == {"invalid_citation": 6}
    assert out.findings == [] and out.observations == []


def test_structural_draft_error_fails_thread_after_repair(corpus):
    root, land, _ = corpus

    def stance_mismatch(payload):
        ids_ = [e["evidence_id"] for e in payload["evidence"]]
        f = finding(ids_)
        f["stance_by_evidence"] = {ids_[0]: "supports"}                  # keys != evidence_ids
        return draft(payload, findings=[f])

    out = run(make_reasoner(root, FakeChat(draft_fn=stance_mismatch)), land)
    assert out.coverage.threads["failed"] == 6
    assert _by_reason(out) == {"draft_invalid": 6}
    assert out.coverage.calls == {"draft": 6, "repair": 6, "total": 12, "budget": None}


def test_tension_with_one_paper_is_no_valid_evidence_not_observation(corpus):
    root, land, ids = corpus

    def one_paper_tension(payload):
        mine = [e["evidence_id"] for e in items_of(payload, ids["P1"])]
        if len(mine) < 2:
            return draft(payload, outcome="insufficient_evidence")
        return draft(payload, tensions=[{
            "statement": "x", "sides": [{"assertion": "a", "evidence_ids": mine[:1]},
                                        {"assertion": "b", "evidence_ids": mine[1:2]}],
            "candidate_explanations": []}])

    out = run(make_reasoner(root, FakeChat(draft_fn=one_paper_tension)), land)
    assert out.tensions == [] and out.observations == []
    assert _by_reason(out).get("no_valid_evidence")
    assert all(d.kind == "tension" for d in out.diagnostics if d.reason == "no_valid_evidence")


def test_tension_explanation_outside_its_evidence_is_invalid_citation(corpus):
    root, land, ids = corpus

    def bad_expl(payload):
        a = [e["evidence_id"] for e in items_of(payload, ids["P1"])]
        b = [e["evidence_id"] for e in items_of(payload, ids["P3"])]
        other = [e["evidence_id"] for e in items_of(payload, ids["P2"])]
        if not (a and b and other):
            return draft(payload, outcome="insufficient_evidence")
        return draft(payload, tensions=[{
            "statement": "x", "sides": [{"assertion": "a", "evidence_ids": a[:1]},
                                        {"assertion": "b", "evidence_ids": b[:1]}],
            "candidate_explanations": [{"text": "e", "evidence_ids": other[:1]}]}])

    out = run(make_reasoner(root, FakeChat(draft_fn=bad_expl)), land)
    assert out.tensions == []
    assert _by_reason(out).get("invalid_citation")


# ------------------------------------------------------------------ verification

def test_verify_none_accepts_nothing(corpus):
    root, land, _ = corpus
    out = run(make_reasoner(root, FakeChat(), verify="none"), land)
    assert out.findings == [] and out.observations == [] and out.tensions == []
    assert set(_by_reason(out)) == {"verification_skipped"}
    assert out.coverage.calls == {"draft": 6, "total": 6, "budget": None}
    assert out.run.verify_mode == "none"


def test_missing_page_yields_insufficient_pages(tmp_path):
    land, ids = build_demo_corpus(tmp_path / "demo", drop_pages={"P1": [2]})
    out = run(make_reasoner(tmp_path / "demo", FakeChat()), land)
    assert _by_reason(out).get("insufficient_pages")
    assert out.coverage.review_pages["requested"] > out.coverage.review_pages["fetched"]
    # nothing accepted may require the missing page
    for f in out.findings + out.observations + out.tensions:
        assert (ids["P1"], 2) not in {(s.paper_id, s.page) for s in f.review_sources}


def test_review_reject_and_insufficient_route_to_diagnostics(corpus):
    root, land, _ = corpus

    def decide(payload):
        out = []
        for i, c in enumerate(payload["candidates"]):
            out.append({"candidate_id": c["candidate_id"],
                        "decision": "reject" if i % 2 == 0 else "insufficient",
                        "notes": "n", "page_ids": []})
        return {"decisions": out}

    out = run(make_reasoner(root, FakeChat(review_fn=decide)), land)
    assert out.findings == [] and out.tensions == []
    assert set(_by_reason(out)) <= {"rejected_on_review", "insufficient_pages"}
    assert _by_reason(out)["rejected_on_review"] >= 1


def test_review_unknown_or_missing_candidate_invalidates_call(corpus):
    root, land, _ = corpus

    def extra(payload):
        d = cooperative_review(payload)
        d["decisions"].append({"candidate_id": "ghost", "decision": "accept"})
        return d

    out = run(make_reasoner(root, FakeChat(review_fn=extra)), land)
    assert out.findings == [] and set(_by_reason(out)) == {"review_invalid"}
    assert out.coverage.calls["repair"] == 6

    def omit(payload):
        d = cooperative_review(payload)
        d["decisions"] = d["decisions"][1:]
        return d

    out = run(make_reasoner(root, FakeChat(review_fn=omit)), land)
    assert out.findings == [] and set(_by_reason(out)) == {"review_invalid"}


def test_review_unknown_page_id_invalidates_call(corpus):
    root, land, _ = corpus

    def bad_page(payload):
        d = cooperative_review(payload)
        d["decisions"][0]["page_ids"] = ["nope#p3"]
        return d

    out = run(make_reasoner(root, FakeChat(review_fn=bad_page)), land)
    assert set(_by_reason(out)) == {"review_invalid"}


def test_review_page_ids_are_keyed_by_paper_and_page(corpus):
    root, land, ids = corpus
    seen = {}

    def spy(payload):
        seen.update({p["page_id"]: (p["paper_id"], p["page"]) for p in payload["pages"]})
        return cooperative_review(payload)

    run(make_reasoner(root, FakeChat(review_fn=spy)), land)
    assert f"{ids['P1']}#p2" in seen and f"{ids['P3']}#p2" in seen    # same page number, distinct ids
    assert all(pid == k.split("#p")[0] and n == int(k.split("#p")[1]) for k, (pid, n) in seen.items())


def test_page_cap_makes_candidates_insufficient_not_partially_accepted(corpus):
    root, land, _ = corpus
    out = run(make_reasoner(root, FakeChat(), budgets=Budgets(max_review_pages=2)), land)
    assert out.coverage.review_pages["omitted"] > 0
    assert _by_reason(out).get("insufficient_pages")
    for f in out.findings:
        assert {(s.paper_id, s.page) for s in f.review_sources} == {
            (e.paper_id, l.page) for e in f.evidence for l in e.source_locations}


# ------------------------------------------------------------------ budgets / failures

def test_truncated_draft_is_malformed_and_only_that_thread_fails(corpus):
    root, land, _ = corpus
    chat = FakeChat(finish_reason=lambda p: "length" if p["thread_id"] == "t001"
                    and p["task"] == "draft" else "stop")
    out = run(make_reasoner(root, chat), land)
    assert out.coverage.threads["failed"] == 1 and out.coverage.threads["completed"] == 5
    d = [x for x in out.diagnostics if x.reason == "draft_invalid"][0]
    assert d.thread_id == "t001" and "truncated" in d.detail


def test_malformed_json_repairs_once_then_fails(corpus):
    root, land, _ = corpus
    chat = FakeChat(raw_text=lambda p: "not json at all" if p["task"] == "draft" else None)
    out = run(make_reasoner(root, chat), land)
    assert out.coverage.threads["failed"] == 6
    assert out.coverage.calls == {"draft": 6, "repair": 6, "total": 12, "budget": None}


def test_failed_call_consumes_attempt_and_thread_fails(corpus):
    root, land, _ = corpus
    chat = FakeChat(raise_exc=lambda p: RuntimeError("boom") if p["thread_id"] == "t002" else None)
    out = run(make_reasoner(root, chat), land)
    assert out.coverage.threads["failed"] == 1
    assert out.coverage.calls["draft"] == 6                      # the raising call still counted
    assert out.usage == {}                                        # fake records no usage: by design


def test_budget_exhausted_between_draft_and_review(corpus):
    root, land, _ = corpus
    out = run(make_reasoner(root, FakeChat(), budgets=Budgets(max_calls=1), concurrency=1), land)
    assert out.coverage.calls["total"] == 1
    assert out.findings == [] and out.observations == [] and out.tensions == []
    assert out.coverage.threads["budget_skipped"] == 6
    assert _by_reason(out).get("budget_skipped")


def test_concurrent_threads_never_exceed_max_calls(corpus):
    root, land, _ = corpus
    out = run(make_reasoner(root, FakeChat(), budgets=Budgets(max_calls=5), concurrency=6), land)
    assert out.coverage.calls["total"] == 5
    assert out.coverage.threads["budget_skipped"] + out.coverage.threads["completed"] == 6


def test_fatal_provider_error_stops_run(corpus):
    root, land, _ = corpus
    resp = httpx.Response(401, request=httpx.Request("POST", "https://api.example/v1/chat"))
    err = openai.AuthenticationError("Incorrect API key", response=resp, body=None)
    chat = FakeChat(raise_exc=lambda p: err)
    r = make_reasoner(root, chat, concurrency=1)
    out = run(r, land)
    assert r._fatal is not None
    assert out.coverage.threads["failed"] == 6
    assert out.coverage.calls["total"] == 1                       # stopped after the first attempt


# ------------------------------------------------------------------ agreement / confidence

def _findings_by_stance(root, land, ids, stance_for, papers):
    def d(payload):
        chosen = [e for e in payload["evidence"] if e["paper_id"] in papers]
        by_paper = {}
        for e in chosen:
            by_paper.setdefault(e["paper_id"], []).append(e["evidence_id"])
        ids_ = [v[0] for v in by_paper.values()]
        if len(ids_) < len(papers):
            return draft(payload, outcome="insufficient_evidence")
        st = {i: stance_for(next(e for e in chosen if e["evidence_id"] == i)) for i in ids_}
        return draft(payload, findings=[finding(ids_, stances=st)])
    return run(make_reasoner(root, FakeChat(draft_fn=d)), land)


def test_confidence_high_medium_low_rules(corpus):
    root, land, ids = corpus
    # three supporting papers, no unknown majority → high (thread with P1,P2,P4: r_offload)
    out = _findings_by_stance(root, land, ids, lambda e: "supports", {ids["P1"], ids["P2"], ids["P4"]})
    rel = [f for f in out.findings if f.refines == ["r_offload_introduces_transfer"]]
    assert rel and rel[0].confidence == "high" and rel[0].agreement.counts.supports == 3
    # two supporting papers → medium
    out = _findings_by_stance(root, land, ids, lambda e: "supports", {ids["P1"], ids["P2"]})
    rel = [f for f in out.findings if f.refines == ["r_caching_reduces_transfer"]]
    assert rel and rel[0].confidence == "medium"
    # a contradicting paper → low; a paper with both stances → mixed → low
    out = _findings_by_stance(root, land, ids,
                              lambda e: "contradicts" if e["paper_id"] == ids["P2"] else "supports",
                              {ids["P1"], ids["P2"], ids["P4"]})
    rel = [f for f in out.findings if f.refines == ["r_offload_introduces_transfer"]]
    assert rel and rel[0].confidence == "low" and rel[0].agreement.counts.contradicts == 1


def test_mixed_stance_and_unknown_majority(corpus):
    root, land, ids = corpus

    def d(payload):
        mine = [e["evidence_id"] for e in items_of(payload, ids["P1"])][:2]
        other = [e["evidence_id"] for e in items_of(payload, ids["P2"])][:1]
        if len(mine) < 2 or not other:
            return draft(payload, outcome="insufficient_evidence")
        st = {mine[0]: "supports", mine[1]: "contradicts", other[0]: "supports"}
        return draft(payload, findings=[finding(mine + other, stances=st)])

    out = run(make_reasoner(root, FakeChat(draft_fn=d)), land)
    f = out.findings[0]
    assert f.agreement.counts.mixed == 1 and f.confidence == "low"
    assert sum(f.agreement.counts.model_dump().values()) == f.agreement.denominator


def test_comparability_notes_force_low_confidence(corpus):
    root, land, _ = corpus

    def d(payload):
        base = cooperative_draft(payload)
        for f in base["findings"]:
            f["stance_by_evidence"] = {k: "supports" for k in f["stance_by_evidence"]}
            f["conditions"] = []
        base["tensions"] = []
        base["comparability_notes"] = ["different hardware across papers"]
        return base

    out = run(make_reasoner(root, FakeChat(draft_fn=d)), land)
    assert out.findings and all(f.confidence == "low" for f in out.findings)


def test_hypothesis_condition_forces_low(corpus):
    root, land, _ = corpus

    def d(payload):
        ids_ = [e["evidence_id"] for e in payload["evidence"]]
        return draft(payload, findings=[finding(ids_, conditions=[{"text": "maybe", "evidence_ids": []}])])

    out = run(make_reasoner(root, FakeChat(draft_fn=d)), land)
    assert out.findings and all(f.confidence == "low" for f in out.findings)
    assert all(f.conditions[0].hypothesis for f in out.findings)


def test_insufficient_evidence_outcome_is_a_valid_answer(corpus):
    root, land, _ = corpus
    out = run(make_reasoner(root, FakeChat(draft_fn=lambda p: draft(p, outcome="insufficient_evidence"))),
              land)
    assert out.coverage.threads["insufficient"] == 6
    assert _by_reason(out) == {"insufficient_evidence": 6}
    assert out.coverage.calls == {"draft": 6, "total": 6, "budget": None}


def test_output_json_roundtrip(corpus, tmp_path):
    root, land, _ = corpus
    out = run(make_reasoner(root, FakeChat()), land)
    path = tmp_path / "out.json"
    path.write_text(json.dumps(out.model_dump()))
    again = S.CrossPaperReasoning.model_validate(json.loads(path.read_text()))
    assert again.coverage == out.coverage
