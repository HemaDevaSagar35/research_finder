"""Cross-Paper Reasoner (component 8): what do these papers collectively imply?

Input: a Landscape (reasoning.schemas) and the paper.json / page artifacts
behind it. Output: accepted cross-paper findings, single-paper observations
and unresolved tensions — each grounded in evidence the code handed to the
model and passed an automated page-based support review — plus typed
diagnostics for everything that did not make it, coverage, and usage.

Per landscape item / relationship / contradiction ("thread"):

    schedule   supporting papers first, deterministic same-group expansion,
               one shared cap (contradiction sides get quotas summing to it)
    bundle     complete evidence records with provenance (reasoning.evidence)
    draft      one LLM call → ThreadReasoningDraft (ids only); one repair round
    resolve    code maps ids → Evidence; unknown id ⇒ whole candidate rejected;
               0 papers ⇒ diagnostic, 1 ⇒ observation, ≥2 ⇒ finding; tensions
               keep two non-empty sides and ≥2 papers
    review     one LLM call over the thread's candidates + the cited pages
               (code-owned page ids) → one accept/reject/insufficient per
               candidate; anything else invalidates the call
    accept     agreement + ordered confidence for findings; review_sources
               with page hashes; everything else → diagnostics

Budgets are enforced by a reservation counter (reasoning.budget), not by
token accounting. See docs/cross_paper_reasoner.md.

    uv run python -m reasoning.cross_paper --landscape L.json --out out.json \
        [--root markdown] [--verify pages|none] [--chat fake] [--index-dir index] ...
"""

from __future__ import annotations

from llm_client.progress import gather as progress_gather
import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import load_dotenv
from pydantic import ValidationError

from extraction.research_extract import _parse_json
from llm_client import ChatResult
from llm_client import errors as llm_errors
from llm_client import usage
from reasoning import schemas as S
from landscape.schemas import LimitationSource, limitation_origin
from reasoning.budget import TOKEN_COUNTER, AttemptBudget, estimate_tokens
from reasoning.evidence import (BundleBudget, EvidenceCandidate, LoadedPaper, PaperStore,
                                bundle as make_bundle, candidates as make_candidates)

load_dotenv()

PROMPT_VERSION = "cross_paper_prompt_v2_attribution"
ChatFn = Callable[..., Awaitable[ChatResult]]


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

@dataclass
class Budgets:
    max_calls: int | None = None
    max_threads: int = 100
    max_papers_per_thread: int = 12
    max_bundle_chars: int = field(default_factory=lambda: int(os.getenv("REASON_MAX_BUNDLE_CHARS", "2000000")))
    max_draft_input_tokens: int = field(default_factory=lambda: int(os.getenv("REASON_MAX_DRAFT_INPUT_TOKENS", "500000")))
    max_draft_output_tokens: int = field(default_factory=lambda: int(os.getenv("REASON_MAX_DRAFT_OUTPUT_TOKENS", "500000")))
    max_review_pages: int = 16
    max_review_input_tokens: int = field(default_factory=lambda: int(os.getenv("REASON_MAX_REVIEW_INPUT_TOKENS", "500000")))
    max_review_output_tokens: int = field(default_factory=lambda: int(
        os.environ.get("REASON_MAX_REVIEW_OUTPUT_TOKENS", "500000")))
    repair_rounds: int = 1

    def as_dict(self) -> dict[str, int | None]:
        return asdict(self)


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

@dataclass
class Thread:
    thread_id: str
    kind: str                       # "relationship" | ItemKind | "contradiction"
    refines: list[str]
    statement: str
    papers: list[str]               # the named denominator set, ordered
    group_ids: list[str]
    sides: list[dict] | None = None  # contradiction: [{assertion, paper_ids}]
    omitted_supporting: list[str] = field(default_factory=list)
    status: str = "scheduled"       # completed | insufficient | failed | budget_skipped
                                    # | insufficient_for_adjudication
    detail: str = ""
    limitation_sources: list[LimitationSource] = field(default_factory=list)


def _ordered(supporting: list[str], group_sets: list[set[str]], cap: int
             ) -> tuple[list[str], list[str]]:
    """Supporting papers (paper_id asc, deduped) first, then expansion from
    the groups ordered by (-shared groups, paper_id). Returns (chosen,
    omitted_supporting)."""
    sup = sorted(set(supporting))
    chosen = sup[:cap]
    omitted = sup[cap:]
    if len(chosen) < cap:
        pool = set().union(*group_sets) if group_sets else set()
        pool -= set(sup)
        ranked = sorted(pool, key=lambda p: (-sum(p in g for g in group_sets), p))
        chosen += ranked[:cap - len(chosen)]
    return chosen, omitted


def allocate_sides(side_a: list[str], side_b: list[str], cap: int
                   ) -> tuple[list[str], dict[str, set[str]]]:
    """One shared cap for a contradiction. Quotas floor(cap/2) and
    cap - floor(cap/2); a paper on both sides is counted once and keeps
    both memberships; unused quota is redistributed (a first, then b).
    Returns (chosen in order, memberships {paper_id: {"a","b"}})."""
    q_a, q_b = cap // 2, cap - cap // 2
    chosen: list[str] = []

    def take(lst: list[str], quota: int) -> None:
        n = 0
        for pid in lst:
            if n >= quota:
                break
            if pid in chosen:
                continue
            chosen.append(pid)
            n += 1

    take(side_a, q_a)
    take(side_b, q_b)
    take(side_a, cap - len(chosen))
    take(side_b, cap - len(chosen))
    members = {p: {s for s, lst in (("a", side_a), ("b", side_b)) if p in lst}
               for p in chosen}
    return chosen, members


def schedule_threads(land: S.Landscape | S.LegacyLandscape, budgets: Budgets) -> list[Thread]:
    """Consume the builder's shared contract; keep old fixtures compatible."""
    if not isinstance(land, S.Landscape):
        return _schedule_legacy_threads(land, budgets)
    groups = {g.concept_id: g for g in land.groups}
    cap = budgets.max_papers_per_thread
    threads = []

    def relationship_statement(r):
        return f"{groups[r.source].label} {r.relation} {groups[r.target].label}"

    def related(r):
        return [set(groups[gid].paper_ids) for gid in (r.source, r.target)]

    def append(kind, refs, statement, papers, group_ids, omitted=None, sides=None):
        threads.append(Thread(
            thread_id=f"t{len(threads) + 1:03d}", kind=kind, refines=refs,
            statement=statement, papers=papers, group_ids=group_ids,
            omitted_supporting=omitted or [], sides=sides))

    for r in land.relationships:
        chosen, omitted = _ordered(r.supporting_papers, related(r), cap)
        append("relationship", [r.item_id], relationship_statement(r),
               chosen, [r.source, r.target], omitted)
    for name, kind in (("aggregated_findings", "aggregated_finding"),
                       ("recurring_limitations", "recurring_limitation"),
                       ("common_assumptions", "common_assumption")):
        for item in getattr(land, name):
            chosen, omitted = _ordered(item.supporting_papers, [], cap)
            append(kind, [item.item_id], item.statement, chosen, [], omitted)
            threads[-1].limitation_sources = [s for s in item.limitation_sources
                                               if s.paper_id in chosen]
    for u in land.underexplored_regimes:
        chosen, omitted = _ordered(groups[u.concept_id].paper_ids, [], cap)
        statement = (f"Within the selected landscape corpus, {u.label} has "
                     f"{u.mention_count} paper mentions and maximum relationship "
                     f"support of {u.max_relation_support} papers. Examine the "
                     "available evidence and its limits; these counts do not "
                     "establish a gap in the wider literature.")
        append("underexplored_regime", [u.item_id], statement, chosen,
               [u.concept_id], omitted)
    for c in land.contradictions:
        side_lists, omitted = [], []
        for side in (c.a, c.b):
            selected, dropped = _ordered(side.supporting_papers, related(side), cap)
            side_lists.append(selected)
            omitted += dropped
        chosen, members = allocate_sides(*side_lists, cap)
        sides = [{"assertion": relationship_statement(r),
                  "paper_ids": [p for p in chosen if key in members[p]]}
                 for key, r in (("a", c.a), ("b", c.b))]
        append("contradiction", [c.item_id, c.a.item_id, c.b.item_id],
               f"Potential contradiction — A: {sides[0]['assertion']} | "
               f"B: {sides[1]['assertion']}", chosen,
               sorted({c.a.source, c.a.target, c.b.source, c.b.target}), omitted, sides)
        if not all(side["paper_ids"] for side in sides) or len(chosen) < 2:
            threads[-1].status = "insufficient_for_adjudication"
            threads[-1].detail = "a side has no papers within the cap"
    for t in threads[budgets.max_threads:]:
        t.status, t.detail = "budget_skipped", "beyond --max-threads"
    return threads


def _schedule_legacy_threads(land: S.LegacyLandscape, budgets: Budgets) -> list[Thread]:
    groups = {g.group_id: set(g.paper_ids) for g in land.groups}
    concept = {g.group_id: g.concept for g in land.groups}
    cap = budgets.max_papers_per_thread
    threads: list[Thread] = []

    for r in land.relationships:
        sup = [p.paper_id for p in r.supporting]
        chosen, omitted = _ordered(sup, [groups[r.source_group_id], groups[r.target_group_id]], cap)
        threads.append(Thread(
            thread_id=f"t{len(threads) + 1:03d}", kind="relationship", refines=[r.item_id],
            statement=f"{concept[r.source_group_id]} {r.relation} {concept[r.target_group_id]}",
            papers=chosen, group_ids=[r.source_group_id, r.target_group_id],
            omitted_supporting=omitted))

    for it in land.items:
        sup = [p.paper_id for p in it.supporting]
        chosen, omitted = _ordered(sup, [groups[g] for g in it.group_ids], cap)
        threads.append(Thread(
            thread_id=f"t{len(threads) + 1:03d}", kind=it.kind, refines=[it.item_id],
            statement=it.statement, papers=chosen, group_ids=list(it.group_ids),
            omitted_supporting=omitted))

    for c in land.contradictions:
        side_lists = []
        omitted_all: list[str] = []
        for side in (c.side_a, c.side_b):
            sup = [p.paper_id for p in side.supporting]
            ordered, omitted = _ordered(sup, [groups[g] for g in side.group_ids], cap)
            side_lists.append(ordered)
            omitted_all += omitted
        chosen, members = allocate_sides(side_lists[0], side_lists[1], cap)
        t = Thread(
            thread_id=f"t{len(threads) + 1:03d}", kind="contradiction",
            refines=[c.item_id, c.side_a.item_id, c.side_b.item_id],
            statement=f"Contradiction — A: {c.side_a.statement} | B: {c.side_b.statement}",
            papers=chosen, group_ids=sorted(set(c.side_a.group_ids) | set(c.side_b.group_ids)),
            sides=[{"assertion": c.side_a.statement,
                    "paper_ids": [p for p in chosen if "a" in members[p]]},
                   {"assertion": c.side_b.statement,
                    "paper_ids": [p for p in chosen if "b" in members[p]]}],
            omitted_supporting=omitted_all)
        if not t.sides[0]["paper_ids"] or not t.sides[1]["paper_ids"] or len(chosen) < 2:
            t.status, t.detail = "insufficient_for_adjudication", "a side has no papers within the cap"
        threads.append(t)

    for t in threads[budgets.max_threads:]:
        t.status, t.detail = "budget_skipped", "beyond --max-threads"
    return threads


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

DRAFT_SYSTEM = """You are a careful research analyst performing cross-paper reasoning.
You are given ONE statement from a research landscape and evidence records extracted from the
papers behind it. Decide what the papers COLLECTIVELY imply about the statement.

Rules (violations make your answer unusable):
1. Cite evidence ONLY by the evidence_id values supplied. Never invent ids, numbers, or papers.
2. A finding needs evidence from the supplied records. Conditions ("only when ...") must list the
   evidence_ids that show them; a condition with no evidence_ids is recorded as a hypothesis.
3. finding_kind: confirms = the papers support the statement and you can say WHY/HOW;
   qualifies = it holds only under conditions you can evidence; overturns = the evidence contradicts
   it; extends = the evidence supports a more specific or broader statement.
4. stance_by_evidence must have exactly the keys in evidence_ids: supports / contradicts / qualifies.
5. If papers disagree and the records do not explain why, emit a tension with two sides (each with
   evidence_ids) instead of forcing a conclusion. Explanations without evidence_ids are hypotheses.
6. If results are not comparable (different metrics, hardware, settings), say so in
   comparability_notes and prefer outcome "comparability_issue" over a causal story.
7. Returning zero findings with outcome "insufficient_evidence" is a valid, good answer.
8. Non-selection is not agreement: a paper with no relevant record says nothing.
9. Evidence origin and landscape limitation_sources preserve extraction attribution.
   author_stated is the extractor's classification, not verified author testimony.
   model_inferred is an interpretation, never an explicit author admission. An
   untested setting inferred from reported experiments is not an author-stated gap.
   Preserve distinctions per paper, including mixed-origin aggregates. Qualify
   any inference and scope it to available evidence. Missing attribution is unknown.
   limitation_sources are context, not additional citable evidence IDs; cite only
   supplied evidence records, and leave page-level support to the review.
Output ONLY one JSON object matching the schema in the user message."""

REVIEW_SYSTEM = """You are auditing candidate conclusions against the ORIGINAL paper pages.
For EVERY candidate return exactly one decision:
  accept        the cited pages support the statement AND each evidenced condition/side as written
  reject        the pages contradict it, or the statement/condition asserts something the pages do not show
  insufficient  the supplied pages do not contain enough to judge
You may cite page_ids you were given (only those). Do not rewrite candidates; if a narrower statement
would be supportable, say so in notes and still decide on the candidate as written.
Extraction origin labels are not proof: model_inferred statements are interpretations,
not explicit author admissions. Even author_stated labels must be verified against
these pages. Reject claimed author attribution the original pages do not establish;
an inference may be acceptable only if clearly presented as such and supported.
Output ONLY one JSON object: {"decisions": [{"candidate_id", "decision", "notes", "page_ids"}]}."""


def _fence(obj) -> str:
    return "```json\n" + json.dumps(obj, indent=1, ensure_ascii=False) + "\n```"


def draft_messages(thread: Thread, bnd: S.EvidenceBundle, titles: dict[str, str]) -> list[dict]:
    payload = {
        "task": "draft", "thread_id": thread.thread_id, "kind": thread.kind,
        "statement": thread.statement,
        "sides": thread.sides,
        "limitation_sources": [s.model_dump() for s in thread.limitation_sources],
        "thread_papers": [{"paper_id": p, "title": titles.get(p, "")} for p in thread.papers],
        "evidence": [i.model_dump(exclude={"value_path", "provenance_path", "source_locations"})
                     for i in bnd.items],
        "omitted_records": len(bnd.omitted),
    }
    user = ("Respond with one JSON object valid against this schema "
            "(ThreadReasoningDraft):\n" + _fence(S.ThreadReasoningDraft.model_json_schema())
            + "\n\nThread payload:\n" + _fence(payload))
    return [{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}]


def review_messages(req: S.SupportReviewRequest) -> list[dict]:
    payload = {"task": "review", "thread_id": req.thread_id,
               "candidates": [c.model_dump() for c in req.candidates],
               "pages": [p.model_dump(exclude={"sha256"}) for p in req.pages]}
    return [{"role": "system", "content": REVIEW_SYSTEM},
            {"role": "user", "content": "Review payload:\n" + _fence(payload)}]


# --------------------------------------------------------------------------
# Reasoner
# --------------------------------------------------------------------------

class FatalProviderError(RuntimeError):
    pass


@dataclass
class _Candidate:
    candidate_id: str
    kind: str                       # finding | observation | tension
    thread: Thread
    payload: dict                   # resolved, read-only for the reviewer
    evidence: list[S.Evidence]
    required_pages: set[tuple[str, int]]
    comparability: bool = False


@dataclass
class _ThreadResult:
    thread: Thread
    findings: list[S.CrossPaperFinding] = field(default_factory=list)
    observations: list[S.Observation] = field(default_factory=list)
    tensions: list[S.Tension] = field(default_factory=list)
    diagnostics: list[S.CandidateDiagnostic] = field(default_factory=list)
    counters: Counter = field(default_factory=Counter)
    inspected: set[str] = field(default_factory=set)
    cited: set[str] = field(default_factory=set)


class CrossPaperReasoner:
    def __init__(self, store: PaperStore, chat: ChatFn | None = None, *,
                 budgets: Budgets | None = None, provider: str | None = None,
                 draft_model: str | None = None, review_model: str | None = None,
                 verify: str = "pages", concurrency: int = 4,
                 index_dir: Path | None = None, bundle_budget: BundleBudget | None = None):
        self.store = store
        self._chat = chat
        self.budgets = budgets or Budgets()
        self.provider = provider or os.environ.get("REASON_PROVIDER") or os.environ.get("PROVIDER")
        self.draft_model = draft_model or os.environ.get("REASON_MODEL")
        self.review_model = review_model or os.environ.get("REASON_REVIEW_MODEL") or self.draft_model
        if verify not in ("pages", "none"):
            raise ValueError("verify must be 'pages' or 'none'")
        self.verify = verify
        self.concurrency = concurrency
        self.index_dir = Path(index_dir) if index_dir else None
        self.bundle_budget = bundle_budget or BundleBudget(max_chars=self.budgets.max_bundle_chars)
        self.budget = AttemptBudget(self.budgets.max_calls)
        self._fatal: BaseException | None = None

    # ------------------------------------------------------------ boundary
    def _client(self) -> ChatFn:
        if self._chat is None:
            from llm_client import AsyncLLMClient
            client = AsyncLLMClient(self.provider, concurrency=self.concurrency, max_retries=0)
            self.provider = client.provider
            self.draft_model = self.draft_model or client.default_model
            self.review_model = self.review_model or self.draft_model
            self._chat = client.chat_result
        return self._chat

    async def _call(self, kind: str, messages: list[dict], model: str | None,
                    max_tokens: int) -> ChatResult | None:
        """Reserve an attempt, then call. None = budget exhausted."""
        if not await self.budget.reserve(kind):
            return None
        try:
            return await self._client()(messages=messages, model=model, max_tokens=max_tokens,
                                        response_format={"type": "json_object"})
        except BaseException as e:            # noqa: BLE001
            if llm_errors.is_fatal(e):
                self._fatal = e
                raise FatalProviderError(llm_errors.describe(e)) from e
            raise

    # ------------------------------------------------------------ run
    async def run(self, land: S.Landscape | S.LegacyLandscape) -> S.CrossPaperReasoning:
        land = S.validate_landscape(land)
        started = time.strftime("%Y-%m-%dT%H:%M:%S")
        usage_before = {k: vars(v) for k, v in usage.snapshot().items()}
        threads = schedule_threads(land, self.budgets)
        titles = {}
        results: list[_ThreadResult] = []
        sem = asyncio.Semaphore(self.concurrency)

        async def one(t: Thread) -> _ThreadResult:
            async with sem:
                if self._fatal is not None:
                    t.status, t.detail = "failed", "run stopped: provider error"
                    return _ThreadResult(t)
                if t.status != "scheduled":
                    return _ThreadResult(t)
                try:
                    return await self._process(t, titles)
                except FatalProviderError:
                    t.status, t.detail = "failed", "provider rejected requests"
                    return _ThreadResult(t)
                except Exception as e:           # noqa: BLE001 — record, continue
                    t.status, t.detail = "failed", f"{type(e).__name__}: {e}"
                    return _ThreadResult(t)

        results = list(await progress_gather(*(one(t) for t in threads), label="reasoning_threads"))
        return self._assemble(land, threads, results, started, usage_before)

    # ------------------------------------------------------------ per thread
    async def _process(self, t: Thread, titles: dict[str, str]) -> _ThreadResult:
        res = _ThreadResult(t)
        papers: dict[str, LoadedPaper] = {p: self.store.get(p) for p in t.papers}
        loaded = {p: lp for p, lp in papers.items() if lp.status == "loaded"}
        for p, lp in loaded.items():
            titles[p] = lp.paper["paper_metadata"]["title"]
        if len(loaded) == 0:
            t.status, t.detail = "insufficient", "no loadable paper.json in thread"
            return res
        if t.sides:
            for side in t.sides:
                if not any(p in loaded for p in side["paper_ids"]):
                    t.status = "insufficient_for_adjudication"
                    t.detail = "a contradiction side has no loadable artifacts"
                    return res

        # ---- bundle
        cands = {p: make_candidates(lp.paper, p) for p, lp in loaded.items()}
        res.inspected |= set(loaded)
        hints = self._hints(t)
        query = t.statement + " " + " ".join(t.group_ids)
        bnd = make_bundle(t.thread_id, t.statement, [p for p in t.papers if p in loaded],
                          cands, query, self.bundle_budget, hints)
        if not bnd.items:
            t.status, t.detail = "insufficient", "no evidence records in the loaded papers"
            return res
        if t.sides:
            for side in t.sides:
                if not any(i.paper_id in side["paper_ids"] for i in bnd.items):
                    t.status = "insufficient_for_adjudication"
                    t.detail = "a contradiction side has no usable evidence after selection"
                    return res

        # ---- draft (+ repair)
        draft, err, msgs = await self._draft(t, bnd, titles)
        if draft is None:
            if err == "budget":
                t.status, t.detail = "budget_skipped", "attempt budget exhausted before draft"
                res.diagnostics.append(self._thread_diag(t, "budget_skipped", t.detail))
            else:
                t.status, t.detail = "failed", err or "draft failed"
                res.diagnostics.append(self._thread_diag(t, "draft_invalid", t.detail))
            return res
        if draft.outcome != "candidates" or (not draft.findings and not draft.tensions):
            reason = "comparability_issue" if draft.outcome == "comparability_issue" \
                else "insufficient_evidence"
            t.status, t.detail = "insufficient", f"draft outcome {draft.outcome}"
            res.diagnostics.append(self._thread_diag(
                t, reason, "; ".join(draft.comparability_notes) or t.detail))
            return res

        # ---- structural resolution
        cands_out = self._resolve(t, draft, bnd, loaded, res)
        res.counters["candidates_drafted"] += len(draft.findings) + len(draft.tensions)
        res.counters["candidates_structurally_valid"] += len(cands_out)

        # ---- review
        if self.verify == "none":
            for c in cands_out:
                res.diagnostics.append(self._cand_diag(c, "verification_skipped",
                                                       "--verify none"))
            t.status = "completed"
            return res
        await self._review(t, cands_out, res)
        if t.status == "scheduled":
            t.status = "completed"
        return res

    def _hints(self, t: Thread) -> dict[str, set[str]] | None:
        if not self.index_dir or not t.papers:
            return None
        try:
            from reasoning.retrieval import hint_tokens
            return hint_tokens(t.statement, list(t.papers), self.index_dir)
        except Exception as e:               # noqa: BLE001 — hints are optional
            print(f"[{t.thread_id}] retrieval hints unavailable: {e}", file=sys.stderr)
            return None

    # ------------------------------------------------------------ draft
    async def _draft(self, t: Thread, bnd: S.EvidenceBundle, titles: dict[str, str]
                     ) -> tuple[S.ThreadReasoningDraft | None, str | None, list[dict]]:
        msgs = draft_messages(t, bnd, titles)
        # input budget: drop lowest-ranked items (from the end) until it fits
        while bnd.items and estimate_tokens(msgs[0]["content"] + msgs[1]["content"]) \
                > self.budgets.max_draft_input_tokens:
            dropped = bnd.items.pop()
            bnd.omitted.append(S.OmittedItem(paper_id=dropped.paper_id,
                                             value_path=dropped.value_path,
                                             reason="input_tokens"))
            msgs = draft_messages(t, bnd, titles)
        if not bnd.items:
            return None, "prompt exceeds --max-draft-input-tokens even with no evidence", msgs
        last_err = None
        for attempt in range(1 + self.budgets.repair_rounds):
            kind = "draft" if attempt == 0 else "repair"
            result = await self._call(kind, msgs, self.draft_model,
                                      self.budgets.max_draft_output_tokens)
            if result is None:
                return None, "budget", msgs
            draft, err = self._parse_draft(result, bnd,
                                           final=attempt == self.budgets.repair_rounds)
            if draft is not None:
                return draft, None, msgs
            last_err = err
            msgs = msgs + [{"role": "assistant", "content": result.text},
                           {"role": "user", "content":
                            "Your JSON failed validation. Fix these problems and output the "
                            "complete corrected JSON object only:\n" + err}]
        return None, f"draft invalid after {1 + self.budgets.repair_rounds} attempts: {last_err}", msgs

    def _parse_draft(self, result: ChatResult, bnd: S.EvidenceBundle, final: bool = False
                     ) -> tuple[S.ThreadReasoningDraft | None, str | None]:
        if result.truncated:
            return None, "output truncated (finish_reason=length); answer more concisely"
        try:
            data = _parse_json(result.text)
            draft = S.ThreadReasoningDraft.model_validate(data)
        except (ValueError, json.JSONDecodeError, ValidationError) as e:
            return None, f"{type(e).__name__}: {str(e)[:800]}"
        errs = S.draft_errors(draft, bnd)
        if any(cat == S.STRUCTURAL for cat, _ in errs) or (errs and not final):
            return None, S.format_errors(errs)
        # final attempt with citation-only errors left: resolution rejects
        # just the affected candidates (invalid_citation), the rest proceeds
        return draft, None

    # ------------------------------------------------------------ resolution
    def _evidence(self, eid: str, stance: str, bnd_by_id: dict[str, S.BundleItem],
                  loaded: dict[str, LoadedPaper]) -> S.Evidence:
        it = bnd_by_id[eid]
        return S.Evidence(
            evidence_id=eid, paper_id=it.paper_id, value_path=it.value_path,
            origin=limitation_origin(it.value_path),
            provenance_path=it.provenance_path, source_locations=it.source_locations,
            source_value=it.source_value, summary=f"{it.label} — {it.source_value}",
            stance=stance, artifact_sha256=loaded[it.paper_id].sha256 or "")

    @staticmethod
    def _location_problem(evs: list[S.Evidence]) -> tuple[str, str] | None:
        for e in evs:
            locs = e.source_locations
            if not locs or all(all(v is None for v in l.model_dump().values()) for l in locs):
                return "unverifiable_location", f"{e.evidence_id}: no usable source_locations"
            if any(l.page is None for l in locs):
                return "unresolved_location", f"{e.evidence_id}: a source_location has page=None"
        return None

    @staticmethod
    def _pages_of(evs: list[S.Evidence]) -> set[tuple[str, int]]:
        return {(e.paper_id, l.page) for e in evs for l in e.source_locations if l.page is not None}

    def _resolve(self, t: Thread, draft: S.ThreadReasoningDraft, bnd: S.EvidenceBundle,
                 loaded: dict[str, LoadedPaper], res: _ThreadResult) -> list[_Candidate]:
        by_id = bnd.by_id()
        out: list[_Candidate] = []
        n = 0
        comparability = bool(draft.comparability_notes)

        def cid() -> str:
            nonlocal n
            n += 1
            return f"{t.thread_id}-c{n:02d}"

        for df in draft.findings:
            c = cid()
            cited = list(dict.fromkeys(df.evidence_ids))
            res.counters["citations_emitted"] += len(cited)
            unknown = [e for e in cited if e not in by_id]
            cond_bad = [e for cond in df.conditions for e in cond.evidence_ids if e not in cited]
            res.counters["citations_invalid"] += len(unknown)
            res.counters["citations_valid"] += len(cited) - len(unknown)
            snap = df.model_dump()
            if unknown or cond_bad:
                res.diagnostics.append(S.CandidateDiagnostic(
                    candidate_id=c, kind="finding", thread_id=t.thread_id, payload_snapshot=snap,
                    reason="invalid_citation",
                    detail=f"unknown ids {unknown}; condition ids outside evidence {cond_bad}"))
                continue
            evs = [self._evidence(e, df.stance_by_evidence[e], by_id, loaded) for e in cited]
            prob = self._location_problem(evs)
            if prob:
                res.diagnostics.append(S.CandidateDiagnostic(
                    candidate_id=c, kind="finding", thread_id=t.thread_id, payload_snapshot=snap,
                    reason=prob[0], detail=prob[1]))
                continue
            distinct = {e.paper_id for e in evs}
            res.cited |= distinct
            if len(distinct) == 0:
                res.diagnostics.append(S.CandidateDiagnostic(
                    candidate_id=c, kind="finding", thread_id=t.thread_id, payload_snapshot=snap,
                    reason="no_valid_evidence", detail="zero valid papers"))
                continue
            kind = "observation" if len(distinct) == 1 else "finding"
            payload = {
                "statement": df.statement, "finding_kind": df.finding_kind,
                "conditions": [{"text": cnd.text, "evidence_ids": cnd.evidence_ids,
                                "hypothesis": not cnd.evidence_ids} for cnd in df.conditions],
                "evidence": [{"evidence_id": e.evidence_id, "paper_id": e.paper_id,
                              "stance": e.stance, "summary": e.summary, "origin": e.origin} for e in evs],
            }
            out.append(_Candidate(c, kind, t, payload, evs, self._pages_of(evs), comparability))

        for dt in draft.tensions:
            c = cid()
            snap = dt.model_dump()
            all_ids = [e for s in dt.sides for e in s.evidence_ids]
            res.counters["citations_emitted"] += len(set(all_ids))
            unknown = sorted({e for e in all_ids if e not in by_id})
            expl_bad = [e for x in dt.candidate_explanations for e in x.evidence_ids
                        if e not in all_ids]
            res.counters["citations_invalid"] += len(unknown)
            res.counters["citations_valid"] += len(set(all_ids)) - len(unknown)
            if unknown or expl_bad or len(dt.sides) != 2:
                res.diagnostics.append(S.CandidateDiagnostic(
                    candidate_id=c, kind="tension", thread_id=t.thread_id, payload_snapshot=snap,
                    reason="invalid_citation",
                    detail=f"unknown ids {unknown}; explanation ids outside tension {expl_bad}"))
                continue
            # stance relative to each side's assertion: cited on a side ⇒ supports that side
            sides_ev = [[self._evidence(e, "supports", by_id, loaded) for e in dict.fromkeys(s.evidence_ids)]
                        for s in dt.sides]
            evs = [e for side in sides_ev for e in side]
            prob = self._location_problem(evs)
            if prob:
                res.diagnostics.append(S.CandidateDiagnostic(
                    candidate_id=c, kind="tension", thread_id=t.thread_id, payload_snapshot=snap,
                    reason=prob[0], detail=prob[1]))
                continue
            distinct = {e.paper_id for e in evs}
            if any(not side for side in sides_ev) or len(distinct) < 2:
                res.diagnostics.append(S.CandidateDiagnostic(
                    candidate_id=c, kind="tension", thread_id=t.thread_id, payload_snapshot=snap,
                    reason="no_valid_evidence",
                    detail="a side is empty or fewer than two distinct papers"))
                continue
            res.cited |= distinct
            payload = {
                "statement": dt.statement,
                "sides": [{"assertion": s.assertion,
                           "evidence": [{"evidence_id": e.evidence_id, "paper_id": e.paper_id,
                                         "summary": e.summary, "origin": e.origin} for e in ev]}
                          for s, ev in zip(dt.sides, sides_ev)],
                "candidate_explanations": [{"text": x.text, "evidence_ids": x.evidence_ids,
                                            "hypothesis": not x.evidence_ids}
                                           for x in dt.candidate_explanations],
                "_sides_evidence": [[e.evidence_id for e in ev] for ev in sides_ev],
            }
            out.append(_Candidate(c, "tension", t, payload, evs, self._pages_of(evs)))
        return out

    # ------------------------------------------------------------ review
    async def _review(self, t: Thread, cands: list[_Candidate], res: _ThreadResult) -> None:
        if not cands:
            return
        # pages: prioritised by number of citing evidence items, capped, then tokens
        need: Counter = Counter()
        for c in cands:
            for e in c.evidence:
                for l in e.source_locations:
                    if l.page is not None:
                        need[(e.paper_id, l.page)] += 1
        ordered = sorted(need, key=lambda k: (-need[k], k[0], k[1]))
        res.counters["review_pages_requested"] += len(ordered)
        fetched: dict[tuple[str, int], object] = {}
        for key in ordered:
            rec = self.store.page(*key)
            if rec is not None:
                fetched[key] = rec
        res.counters["review_pages_fetched"] += len(fetched)
        supplied: list[S.ReviewPage] = []
        overhead = estimate_tokens(REVIEW_SYSTEM) + sum(
            estimate_tokens(json.dumps(c.payload)) for c in cands) + 400
        used = overhead
        for key in ordered:
            rec = fetched.get(key)
            if rec is None or len(supplied) >= self.budgets.max_review_pages:
                continue
            cost = estimate_tokens(rec.text) + 40
            if used + cost > self.budgets.max_review_input_tokens:
                continue
            used += cost
            supplied.append(S.ReviewPage(page_id=f"{rec.paper_id}#p{rec.page}",
                                         paper_id=rec.paper_id, page=rec.page,
                                         sha256=rec.sha256, text=rec.text))
        res.counters["review_pages_supplied"] += len(supplied)
        res.counters["review_pages_omitted"] += len(ordered) - len(supplied)
        have = {(p.paper_id, p.page): p for p in supplied}

        reviewable, pre = [], []
        for c in cands:
            if c.required_pages <= set(have):
                reviewable.append(c)
            else:
                missing = sorted(c.required_pages - set(have))
                res.diagnostics.append(self._cand_diag(
                    c, "insufficient_pages", f"pages not supplied: {missing}"))
        if not reviewable:
            return
        req = S.SupportReviewRequest(
            thread_id=t.thread_id,
            candidates=[S.ReviewCandidate(candidate_id=c.candidate_id, kind=c.kind,
                                          payload={k: v for k, v in c.payload.items()
                                                   if not k.startswith("_")})
                        for c in reviewable],
            pages=supplied)
        msgs = review_messages(req)
        resp, err = None, None
        for attempt in range(1 + self.budgets.repair_rounds):
            result = await self._call("review" if attempt == 0 else "repair", msgs,
                                      self.review_model, self.budgets.max_review_output_tokens)
            if result is None:
                for c in reviewable:
                    res.diagnostics.append(self._cand_diag(c, "budget_skipped",
                                                           "attempt budget exhausted before review"))
                t.status, t.detail = "budget_skipped", "budget exhausted before review"
                return
            resp, err = self._parse_review(result, req)
            if resp is not None:
                break
            msgs = msgs + [{"role": "assistant", "content": result.text},
                           {"role": "user", "content":
                            "Your JSON failed validation. Fix these problems and output the "
                            "complete corrected JSON object only:\n" + err}]
        if resp is None:
            for c in reviewable:
                res.diagnostics.append(self._cand_diag(c, "review_invalid", err or ""))
            return
        res.counters["candidates_reviewed"] += len(reviewable)
        decisions = {d.candidate_id: d for d in resp.decisions}
        for c in reviewable:
            d = decisions[c.candidate_id]
            if d.decision == "accept":
                # review_sources come from the code-owned page records, never from reviewer text
                sources = [S.ReviewSource(paper_id=p, page=n, key_or_path=fetched[(p, n)].key_or_path,
                                          sha256=fetched[(p, n)].sha256)
                           for p, n in sorted(c.required_pages)]
                self._accept(c, sources, d.notes, res)
            elif d.decision == "reject":
                res.diagnostics.append(self._cand_diag(c, "rejected_on_review", d.notes))
            else:
                res.diagnostics.append(self._cand_diag(c, "insufficient_pages", d.notes))

    def _parse_review(self, result: ChatResult, req: S.SupportReviewRequest
                      ) -> tuple[S.SupportReviewResponse | None, str | None]:
        if result.truncated:
            return None, "output truncated (finish_reason=length)"
        try:
            resp = S.SupportReviewResponse.model_validate(_parse_json(result.text))
        except (ValueError, json.JSONDecodeError, ValidationError) as e:
            return None, f"{type(e).__name__}: {str(e)[:800]}"
        errs = S.review_errors(resp, req)
        if errs:
            return None, "\n".join(errs)
        return resp, None

    # ------------------------------------------------------------ acceptance
    def _accept(self, c: _Candidate, sources: list[S.ReviewSource], notes: str,
                res: _ThreadResult) -> None:
        res.counters["candidates_accepted"] += 1
        t = c.thread
        if c.kind == "tension":
            side_ids = c.payload["_sides_evidence"]
            ev_by_id = {e.evidence_id: e for e in c.evidence}
            res.tensions.append(S.Tension(
                tension_id=c.candidate_id.replace("-c", "-x"), thread_id=t.thread_id,
                refines=t.refines, statement=c.payload["statement"],
                sides=[S.TensionSide(assertion=s["assertion"],
                                     evidence=[ev_by_id[i] for i in ids])
                       for s, ids in zip(c.payload["sides"], side_ids)],
                candidate_explanations=[S.Explanation(**x) for x in c.payload["candidate_explanations"]],
                distinct_papers=len({e.paper_id for e in c.evidence}),
                review_sources=sources, review_notes=notes or None))
            return
        conditions = [S.Condition(**x) for x in c.payload["conditions"]]
        if c.kind == "observation":
            res.observations.append(S.Observation(
                observation_id=c.candidate_id.replace("-c", "-o"), thread_id=t.thread_id,
                refines=t.refines, statement=c.payload["statement"], conditions=conditions,
                evidence=c.evidence, review_sources=sources, review_notes=notes or None))
            return
        agreement = self._agreement(t, c.evidence)
        res.findings.append(S.CrossPaperFinding(
            finding_id=c.candidate_id.replace("-c", "-f"), thread_id=t.thread_id,
            refines=t.refines, finding_kind=c.payload["finding_kind"],
            statement=c.payload["statement"], conditions=conditions, evidence=c.evidence,
            agreement=agreement,
            confidence=self._confidence(agreement, conditions, c.comparability),
            review_sources=sources, review_notes=notes or None))

    @staticmethod
    def _agreement(t: Thread, evs: list[S.Evidence]) -> S.Agreement:
        by_paper: dict[str, set[str]] = {}
        for e in evs:
            by_paper.setdefault(e.paper_id, set()).add(e.stance)
        counts = S.AgreementCounts()
        for p in t.papers:
            st = by_paper.get(p)
            if not st:
                counts.unknown += 1
            elif "supports" in st and "contradicts" in st:
                counts.mixed += 1
            elif "contradicts" in st:
                counts.contradicts += 1
            elif "qualifies" in st:
                counts.qualifies += 1
            else:
                counts.supports += 1
        return S.Agreement(denominator=len(t.papers), paper_ids=list(t.papers), counts=counts)

    @staticmethod
    def _confidence(a: S.Agreement, conditions: list[S.Condition], comparability: bool) -> str:
        c = a.counts
        if (c.unknown > a.denominator / 2 or comparability
                or any(x.hypothesis for x in conditions) or c.contradicts or c.mixed):
            return "low"
        if c.supports + c.qualifies == 2 or c.qualifies:
            return "medium"
        return "high"

    # ------------------------------------------------------------ diagnostics
    @staticmethod
    def _thread_diag(t: Thread, reason: str, detail: str) -> S.CandidateDiagnostic:
        return S.CandidateDiagnostic(candidate_id=t.thread_id, kind="thread",
                                     thread_id=t.thread_id, reason=reason, detail=detail,
                                     payload_snapshot={"statement": t.statement,
                                                       "papers": t.papers})

    @staticmethod
    def _cand_diag(c: _Candidate, reason: str, detail: str) -> S.CandidateDiagnostic:
        return S.CandidateDiagnostic(
            candidate_id=c.candidate_id, kind=c.kind, thread_id=c.thread.thread_id,
            payload_snapshot={k: v for k, v in c.payload.items() if not k.startswith("_")},
            reason=reason, detail=detail)

    # ------------------------------------------------------------ output
    def _assemble(self, land: S.Landscape | S.LegacyLandscape, threads: list[Thread], results: list[_ThreadResult],
                  started: str, usage_before: dict) -> S.CrossPaperReasoning:
        findings, observations, tensions, diags = [], [], [], []
        counters: Counter = Counter()
        inspected, cited = set(), set()
        for r in results:
            findings += r.findings
            observations += r.observations
            tensions += r.tensions
            diags += r.diagnostics
            counters.update(r.counters)
            inspected |= r.inspected
            cited |= r.cited
        for t in threads:
            if t.status in ("insufficient_for_adjudication", "failed", "budget_skipped") \
                    and not any(d.candidate_id == t.thread_id for d in diags):
                reason = t.status if t.status != "failed" else "failed"
                diags.append(self._thread_diag(t, reason, t.detail))

        selected = {p for t in threads for p in t.papers}
        statuses = Counter(self.store.get(p).status for p in selected) if selected else Counter()
        thread_status = Counter(t.status for t in threads)
        by_reason = Counter(d.reason for d in diags)

        usage_after = {k: vars(v) for k, v in usage.snapshot().items()}
        usage_delta = {}
        for model, after in usage_after.items():
            before = usage_before.get(model, {})
            delta = {k: after[k] - before.get(k, 0) for k in after}
            if any(delta.values()):
                usage_delta[model] = delta

        coverage = S.Coverage(
            papers={"selected": len(selected), "loaded": statuses.get("loaded", 0),
                    "missing": statuses.get("missing", 0), "invalid": statuses.get("invalid", 0),
                    "inspected": len(inspected), "cited": len(cited)},
            threads={"scheduled": len(threads), **{k: thread_status.get(k, 0) for k in
                     ("completed", "insufficient", "failed", "budget_skipped",
                      "insufficient_for_adjudication")}},
            citations={k: counters.get(f"citations_{k}", 0) for k in ("emitted", "valid", "invalid")},
            candidates={"drafted": counters.get("candidates_drafted", 0),
                        "structurally_valid": counters.get("candidates_structurally_valid", 0),
                        "reviewed": counters.get("candidates_reviewed", 0),
                        "accepted": counters.get("candidates_accepted", 0),
                        "by_reason": dict(sorted(by_reason.items()))},
            review_pages={k: counters.get(f"review_pages_{k}", 0)
                          for k in ("requested", "fetched", "supplied", "omitted")},
            calls=self.budget.snapshot())
        land_json = json.dumps(land.model_dump(), sort_keys=True).encode()
        return S.CrossPaperReasoning(
            topic=land.topic,
            landscape_ref={"schema_version": land.schema_version,
                           "sha256": hashlib.sha256(land_json).hexdigest()},
            run=S.RunInfo(provider=self.provider, draft_model=self.draft_model,
                          review_model=self.review_model, prompt_version=PROMPT_VERSION,
                          budgets=self.budgets.as_dict(), verify_mode=self.verify,
                          token_counter=TOKEN_COUNTER, started=started,
                          ended=time.strftime("%Y-%m-%dT%H:%M:%S")),
            findings=findings, observations=observations, tensions=tensions,
            diagnostics=diags, coverage=coverage, usage=usage_delta)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summary(out: S.CrossPaperReasoning) -> str:
    c = out.coverage
    lines = [f"topic: {out.topic}",
             f"threads: {c.threads}",
             f"papers: {c.papers}",
             f"accepted: {len(out.findings)} findings, {len(out.observations)} observations, "
             f"{len(out.tensions)} tensions | diagnostics: {len(out.diagnostics)} "
             f"{c.candidates['by_reason']}",
             f"citations: {c.citations} | review pages: {c.review_pages}",
             f"calls: {c.calls} | token counter: {out.run.token_counter}"]
    for f in out.findings:
        lines.append(f"  [{f.confidence}] {f.finding_kind}: {f.statement[:110]}")
    for x in out.tensions:
        lines.append(f"  [tension] {x.statement[:110]}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--landscape", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--root", default="markdown",
                        help="Local artifact root (<paper_id>/paper.json, NN.md); S3 fallback "
                             "from S3_ARTIFACTS_URL when set")
    parser.add_argument("--no-s3", action="store_true", help="Never fall back to S3")
    parser.add_argument("--index-dir", default=None, help="Optional local index for ranking hints")
    parser.add_argument("--verify", choices=["pages", "none"], default="pages")
    parser.add_argument("--chat", choices=["provider", "fake"], default="provider",
                        help="fake = cooperative offline model (fixtures / smoke runs)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--review-model", default=None)
    b = Budgets()
    for name, default in b.as_dict().items():
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=default)
    args = parser.parse_args()

    land = S.load_landscape(Path(args.landscape))
    budgets = Budgets(**{k: getattr(args, k) for k in b.as_dict()})
    factory = None
    if not args.no_s3 and os.environ.get("S3_ARTIFACTS_URL"):
        from ingestion.s3store import ArtifactStore
        factory = ArtifactStore
    store = PaperStore(Path(args.root), store_factory=factory)
    chat = None
    if args.chat == "fake":
        from reasoning.fake import FakeChat
        chat = FakeChat()
    reasoner = CrossPaperReasoner(store, chat, budgets=budgets, provider=args.provider,
                                  draft_model=args.model, review_model=args.review_model,
                                  verify=args.verify, concurrency=args.concurrency,
                                  index_dir=args.index_dir)
    out = asyncio.run(reasoner.run(land))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out.model_dump(), indent=2, ensure_ascii=False))
    print(summary(out))
    print(f"\nWrote {args.out}")
    if reasoner._fatal is not None:
        print(f"\n!! STOPPED: provider rejected requests — {llm_errors.describe(reasoner._fatal)}",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
