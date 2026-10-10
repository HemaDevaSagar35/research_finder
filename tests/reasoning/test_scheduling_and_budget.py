"""Thread scheduling (caps, quotas, expansion order) and the attempt budget."""
import asyncio

import pytest

from reasoning.budget import AttemptBudget, estimate_tokens
from reasoning.cross_paper import Budgets, _ordered, allocate_sides, schedule_threads


@pytest.mark.parametrize("cap", [1, 3, 12])
@pytest.mark.parametrize("overlap", [False, True])
def test_allocate_sides_never_exceeds_cap(cap, overlap):
    a = [f"a{i}" for i in range(5)]
    b = [f"b{i}" for i in range(5)]
    if overlap:
        b = ["a0"] + b                          # a0 on both sides
    chosen, members = allocate_sides(a, b, cap)
    assert len(chosen) <= cap
    assert len(chosen) == len(set(chosen))       # counted once
    assert len(chosen) == min(cap, len(set(a) | set(b)))
    if overlap and "a0" in chosen:
        assert members["a0"] == {"a", "b"}
    if cap >= 2:
        assert any("a" in members[p] for p in chosen) and any("b" in members[p] for p in chosen)
    # quotas: with cap 3 → 1 from a, 2 from b before redistribution
    if cap == 3 and not overlap:
        assert chosen == ["a0", "b0", "b1"]


def test_allocate_sides_redistributes_unused_quota():
    chosen, members = allocate_sides(["a0", "a1", "a2", "a3"], ["b0"], 4)
    assert chosen == ["a0", "a1", "b0", "a2"]    # a takes 2, b takes 1, leftover → a
    assert len(chosen) == 4


def test_allocate_sides_cap_one_keeps_one_paper_only():
    chosen, _ = allocate_sides(["a0"], ["b0"], 1)
    assert chosen == ["b0"]                      # floor(1/2)=0 for a, 1 for b; deterministic


def test_ordered_supporting_first_then_shared_group_expansion():
    chosen, omitted = _ordered(["p9", "p1"], [{"p1", "p2", "p3"}, {"p3", "p4"}], cap=4)
    assert chosen[:2] == ["p1", "p9"]            # supporting, paper_id asc
    assert chosen[2] == "p3"                     # in two groups → first
    assert chosen[3] == "p2"                     # then paper_id asc among one-group papers
    assert omitted == []
    chosen, omitted = _ordered(["p3", "p2", "p1"], [set()], cap=2)
    assert chosen == ["p1", "p2"] and omitted == ["p3"]   # excess supporting: deterministic


def test_schedule_threads_counts_kinds_and_budget_skip(corpus):
    _, land, ids = corpus
    threads = schedule_threads(land, Budgets(max_papers_per_thread=12))
    assert len(threads) == len(land.relationships) + len(land.items) + len(land.contradictions)
    kinds = [t.kind for t in threads]
    assert kinds.count("relationship") == 2 and kinds.count("contradiction") == 1
    assert "recurring_limitation" in kinds and "underexplored_regime" in kinds
    contra = next(t for t in threads if t.kind == "contradiction")
    assert contra.sides and all(s["paper_ids"] for s in contra.sides)
    # side A = its supporting papers + same-group expansion (P3 is in g_caching too);
    # side B = P3 only. P3 therefore sits on both sides and is counted once.
    assert set(contra.sides[0]["paper_ids"]) == {ids["P1"], ids["P2"], ids["P3"]}
    assert contra.sides[1]["paper_ids"] == [ids["P3"]]
    assert len(contra.papers) == 3
    # relationship expands into group members not listed as supporting
    rel = next(t for t in threads if t.refines == ["r_offload_introduces_transfer"])
    assert rel.papers[0] == ids["P4"] and set(rel.papers) == {ids["P1"], ids["P2"], ids["P4"]}
    skipped = schedule_threads(land, Budgets(max_threads=2))
    assert [t.status for t in skipped].count("budget_skipped") == len(skipped) - 2


def test_schedule_contradiction_cap_one_is_insufficient(corpus):
    _, land, _ = corpus
    threads = schedule_threads(land, Budgets(max_papers_per_thread=1))
    contra = next(t for t in threads if t.kind == "contradiction")
    assert contra.status == "insufficient_for_adjudication"
    assert len(contra.papers) <= 1


def test_attempt_budget_is_atomic_under_concurrency():
    budget = AttemptBudget(max_calls=10)

    async def go():
        results = await asyncio.gather(*(budget.reserve("draft") for _ in range(50)))
        return results

    results = asyncio.run(go())
    assert results.count(True) == 10 and results.count(False) == 40
    assert budget.used == 10 and budget.remaining == 0
    assert budget.snapshot() == {"draft": 10, "total": 10, "budget": 10}


def test_estimate_tokens_positive_and_monotone():
    assert estimate_tokens("") >= 1
    assert estimate_tokens("a" * 4000) > estimate_tokens("a" * 400)


def test_review_environment_budget_reaches_model(corpus, monkeypatch):
    from reasoning.fake import FakeChat
    from .conftest import make_reasoner, run

    root, land, _ = corpus
    monkeypatch.setenv("REASON_MAX_REVIEW_OUTPUT_TOKENS", "128000")
    chat = FakeChat()
    run(make_reasoner(root, chat), land)
    reviews = [c for c in chat.calls if c["task"] == "review"]
    assert reviews and all(c["max_tokens"] == 128000 for c in reviews)
    assert Budgets(max_review_output_tokens=12000).max_review_output_tokens == 12000
    monkeypatch.delenv("REASON_MAX_REVIEW_OUTPUT_TOKENS")
    assert Budgets().max_review_output_tokens == 500000


def test_unlimited_budget_keeps_accounting_under_concurrency():
    async def exercise():
        budget = AttemptBudget(None)
        assert all(await asyncio.gather(*(budget.reserve('draft') for _ in range(1000))))
        assert budget.remaining is None
        assert budget.snapshot() == {'draft': 1000, 'total': 1000, 'budget': None}
        zero = AttemptBudget(0)
        assert not await zero.reserve('draft')
        finite = AttemptBudget(7)
        results = await asyncio.gather(*(finite.reserve('review') for _ in range(100)))
        assert sum(results) == 7
        assert finite.remaining == 0
    asyncio.run(exercise())
