"""Shared fixtures: a synthetic corpus on disk and a runner."""
import asyncio
from pathlib import Path

import pytest

from reasoning.cross_paper import Budgets, CrossPaperReasoner
from reasoning.evidence import PaperStore
from reasoning.fixtures import build_demo_corpus


@pytest.fixture
def corpus(tmp_path: Path):
    land, ids = build_demo_corpus(tmp_path / "demo")
    return tmp_path / "demo", land, ids


def run(reasoner: CrossPaperReasoner, land):
    return asyncio.run(reasoner.run(land))


def make_reasoner(root: Path, chat, **kw) -> CrossPaperReasoner:
    budgets = kw.pop("budgets", Budgets())
    return CrossPaperReasoner(PaperStore(root), chat, budgets=budgets,
                              provider="fake", draft_model="fake-draft",
                              review_model="fake-review", **kw)


def items_of(payload: dict, paper_id: str | None = None) -> list[dict]:
    ev = payload["evidence"]
    return [e for e in ev if paper_id is None or e["paper_id"] == paper_id]


def finding(ids: list[str], statement="claim", kind="confirms", conditions=None,
            stances=None) -> dict:
    return {"statement": statement, "finding_kind": kind,
            "conditions": conditions or [],
            "evidence_ids": ids,
            "stance_by_evidence": {i: (stances or {}).get(i, "supports") for i in ids}}


def draft(payload: dict, findings=None, tensions=None, notes=None,
          outcome="candidates") -> dict:
    return {"thread_id": payload["thread_id"], "outcome": outcome,
            "findings": findings or [], "tensions": tensions or [],
            "comparability_notes": notes or []}
