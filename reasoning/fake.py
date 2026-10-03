"""Fake LLM boundary for tests and offline demo runs.

The reasoner talks to the model through an async callable
`chat(messages=..., model=..., max_tokens=..., **kw) -> ChatResult`. This
module provides one that never touches a provider. Every reasoner prompt
ends with a single fenced ```json payload (task = "draft" | "review"); the
fake parses it and answers with a cooperative, schema-valid response —
or with whatever a test's `draft_fn` / `review_fn` returns instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from llm_client import ChatResult

_FENCE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def payload_of(messages: list[dict]) -> dict:
    """The fenced JSON payload of the most recent user message that has one
    (repair rounds append a user message without a payload)."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        blocks = _FENCE.findall(m["content"])
        if blocks:
            return json.loads(blocks[-1])
    raise ValueError("no ```json payload in prompt")


def cooperative_draft(payload: dict) -> dict:
    """One finding citing every evidence item, one condition backed by the
    first item, a tension when two papers disagree (a 'failure case' item
    exists), and stances by label."""
    ev = payload["evidence"]
    ids = [e["evidence_id"] for e in ev]
    if not ids:
        return {"thread_id": payload["thread_id"], "outcome": "insufficient_evidence",
                "findings": [], "tensions": [], "comparability_notes": []}

    def stance(e):
        lab = e["label"].lower()
        if "failure" in lab:
            return "contradicts"
        if "limitation" in lab or "future work" in lab or "opportunity" in lab:
            return "qualifies"
        return "supports"

    finding = {
        "statement": f"Collectively, the papers support: {payload['statement']}",
        "finding_kind": "confirms",
        "conditions": [{"text": "when routing locality is high", "evidence_ids": [ids[0]]}],
        "evidence_ids": ids,
        "stance_by_evidence": {e["evidence_id"]: stance(e) for e in ev},
    }
    tensions = []
    contra = [e["evidence_id"] for e in ev if stance(e) == "contradicts"]
    pro = [e["evidence_id"] for e in ev if stance(e) == "supports"]
    if contra and pro and {e["paper_id"] for e in ev if e["evidence_id"] in contra} != \
            {e["paper_id"] for e in ev if e["evidence_id"] in pro}:
        tensions.append({
            "statement": "Caching benefit depends on routing entropy.",
            "sides": [{"assertion": "caching helps", "evidence_ids": pro},
                      {"assertion": "caching fails", "evidence_ids": contra}],
            "candidate_explanations": [
                {"text": "routing entropy differs between workloads", "evidence_ids": contra[:1]},
                {"text": "hardware differs", "evidence_ids": []}],
        })
    return {"thread_id": payload["thread_id"], "outcome": "candidates",
            "findings": [finding], "tensions": tensions, "comparability_notes": []}


def cooperative_review(payload: dict) -> dict:
    pages = [p["page_id"] for p in payload["pages"]]
    return {"decisions": [{"candidate_id": c["candidate_id"], "decision": "accept",
                           "notes": "supported by the cited pages", "page_ids": pages[:2]}
                          for c in payload["candidates"]]}


@dataclass
class FakeChat:
    """Async callable. `draft_fn(payload) -> dict` and `review_fn(payload)
    -> dict` override the cooperative defaults; `finish_reason` and
    `raw_text` let tests return truncated or malformed output; `raise_exc`
    simulates a provider error. Every call is recorded in `calls`."""
    draft_fn: Callable[[dict], dict] = cooperative_draft
    review_fn: Callable[[dict], dict] = cooperative_review
    finish_reason: str | Callable[[dict], str] = "stop"
    raw_text: Callable[[dict], str | None] | None = None
    raise_exc: Callable[[dict], BaseException | None] | None = None
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, *, messages: list[dict], model: str | None = None,
                       max_tokens: int | None = None, **kwargs) -> ChatResult:
        payload = payload_of(messages)
        self.calls.append({"task": payload.get("task"), "thread_id": payload.get("thread_id"),
                           "model": model, "max_tokens": max_tokens,
                           "n_messages": len(messages)})
        if self.raise_exc:
            exc = self.raise_exc(payload)
            if exc is not None:
                raise exc
        if self.raw_text:
            text = self.raw_text(payload)
            if text is not None:
                return ChatResult(text=text, finish_reason=self._finish(payload), model="fake")
        body = (self.draft_fn if payload["task"] == "draft" else self.review_fn)(payload)
        return ChatResult(text=json.dumps(body), finish_reason=self._finish(payload), model="fake")

    def _finish(self, payload: dict) -> str:
        return self.finish_reason(payload) if callable(self.finish_reason) else self.finish_reason
