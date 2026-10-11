"""Deterministic evidence access for the reasoner (no LLM).

PaperStore      loads paper.json and page markdown, local-first
                (markdown/<paper_id>/) with a lazy S3 fallback; records a
                sha256 and loaded/missing/invalid status per paper.
paper_card      the design's PaperCard as a pure projection of paper.json
                (offline_ingestion_design.md decision 1).
candidates      walks a PaperAnalysis dict and emits complete records with
                JSON Pointer paths, inherited provenance, and attached
                context (experiment setup, metric definition, hardware).
bundle          per-paper allocation with a reserved share for boundary
                items (limitations, failure cases), then relevance fill.
                Records are kept whole or flagged truncated; omissions are
                returned, never treated as agreement.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from extraction.research_extract import PaperAnalysis
from reasoning.schemas import BundleItem, EvidenceBundle, OmittedItem
from landscape.schemas import limitation_origin

# --------------------------------------------------------------------------
# PaperStore
# --------------------------------------------------------------------------

PaperStatus = str  # "loaded" | "missing" | "invalid"


@dataclass
class LoadedPaper:
    paper_id: str
    status: PaperStatus
    paper: dict | None = None
    sha256: str | None = None
    error: str | None = None
    path: str | None = None


@dataclass
class PageRecord:
    paper_id: str
    page: int
    text: str
    sha256: str
    key_or_path: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PaperStore:
    """Local-first artifact access. `store_factory` builds an
    ingestion.s3store.ArtifactStore on first S3 need, so fixture runs never
    touch credentials."""

    def __init__(self, root: Path,
                 store_factory: Callable[[], object] | None = None,
                 validate: bool = True):
        self.root = Path(root)
        self._store_factory = store_factory
        self._store = None
        self._validate = validate
        self._papers: dict[str, LoadedPaper] = {}
        self._pages: dict[tuple[str, int], PageRecord | None] = {}

    # ---------------------------------------------------------- S3 fallback
    def _s3(self):
        if self._store is None and self._store_factory is not None:
            self._store = self._store_factory()
        return self._store

    def _fetch(self, paper_id: str, name: str) -> Path | None:
        """Download <paper_id>/<name> into the local root if S3 has it."""
        store = self._s3()
        if store is None:
            return None
        if not store.exists(paper_id, name):
            return None
        dest = self.root / paper_id / name
        store.download_file(dest, paper_id, name)
        return dest

    # ---------------------------------------------------------- paper.json
    def get(self, paper_id: str) -> LoadedPaper:
        if paper_id in self._papers:
            return self._papers[paper_id]
        path = self.root / paper_id / "paper.json"
        if not path.exists():
            path = self._fetch(paper_id, "paper.json") or path
        if not path.exists():
            rec = LoadedPaper(paper_id, "missing", error="paper.json not found")
        else:
            raw = path.read_bytes()
            try:
                data = json.loads(raw)
                if self._validate:
                    PaperAnalysis.model_validate(data)
                rec = LoadedPaper(paper_id, "loaded", paper=data,
                                  sha256=_sha256(raw), path=str(path))
            except (json.JSONDecodeError, ValidationError) as e:
                rec = LoadedPaper(paper_id, "invalid", sha256=_sha256(raw),
                                  error=f"{type(e).__name__}: {str(e)[:200]}",
                                  path=str(path))
        self._papers[paper_id] = rec
        return rec

    # ---------------------------------------------------------- pages
    def page(self, paper_id: str, page: int) -> PageRecord | None:
        key = (paper_id, page)
        if key in self._pages:
            return self._pages[key]
        folder = self.root / paper_id
        candidates = [folder / f"{page:02d}.md", folder / f"{page:03d}.md",
                      folder / f"{page}.md"]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            for name in (f"{page:02d}.md", f"{page:03d}.md"):
                path = self._fetch(paper_id, name)
                if path:
                    break
        rec = None
        if path is not None and path.exists():
            raw = path.read_bytes()
            rec = PageRecord(paper_id, page, raw.decode("utf-8", "replace"),
                             _sha256(raw), str(path))
        self._pages[key] = rec
        return rec


# --------------------------------------------------------------------------
# PaperCard projection (design decision 1)
# --------------------------------------------------------------------------

def paper_card(paper_id: str, paper: dict) -> dict:
    """Compact PaperCard view of a paper.json. Pure projection, no LLM.
    `paper_id` is explicit: PaperAnalysis does not carry it."""
    pm, rp, m = paper["paper_metadata"], paper["research_problem"], paper["method"]
    es, lim, fw = paper["experimental_setup"], paper["limitations"], paper["future_work"]
    return {
        "paper_id": paper_id,
        "title": pm["title"],
        "venue": pm["venue"],
        "year": pm["year"],
        "problem": {
            "research_problem": rp["specific_problem"],
            "general_problem": rp["general_problem"],
            "bottlenecks": rp["why_difficult"],
            "research_questions": rp["research_questions"],
        },
        "method": {
            "high_level_idea": m["high_level_idea"],
            "components": [c["name"] for c in m["components"]],
            "mechanism": m["architecture"]["description"],
        },
        "evaluation": {
            "datasets": [d["name"] for d in paper["datasets"]],
            "baselines": [b["name"] for b in paper["baselines"]],
            "metrics": [x["name"] for x in paper["metrics"]],
            "hardware": es["hardware"],
            "models": [x["name"] for x in paper["models"]],
        },
        "findings": {
            "main_results": [k["finding"] for k in paper["key_results"]],
            "ablations": [a["result"] for a in paper["ablations"]],
            "observations": [f["finding"] for f in paper["interesting_findings"]],
        },
        "boundaries": {
            "limitations_author": [x["limitation"] for x in lim["author_stated"]],
            "limitations_inferred": [x["limitation"] for x in lim["inferred"]],
            "failure_modes": [x["failure"] for x in paper["failure_cases"]],
            "authors_future_work": [x["direction"] for x in fw["author_proposed"]],
        },
        "claims": [{"claim": c["claim"], "claim_type": c["claim_type"],
                    "evidence_strength": c["evidence_strength"],
                    "source_locations": c["source_locations"]}
                   for c in paper["claims_and_evidence"]],
    }


# --------------------------------------------------------------------------
# Evidence candidates
# --------------------------------------------------------------------------

@dataclass
class EvidenceCandidate:
    paper_id: str
    label: str
    value_path: str
    provenance_path: str
    source_locations: list[dict]
    source_value: str
    context: str = ""
    boundary: bool = False
    tokens: set[str] = field(default_factory=set)


_WORD = re.compile(r"[a-z0-9][a-z0-9\-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was",
         "were", "than", "into", "over", "under", "when", "which", "while",
         "their", "these", "those", "have", "has", "not", "but", "its"}


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _s(*parts) -> str:
    """Join non-empty scalar/list parts into one string."""
    out = []
    for p in parts:
        if p is None or p == "" or p == []:
            continue
        if isinstance(p, list):
            p = "; ".join(str(x) for x in p if x not in (None, ""))
            if not p:
                continue
        out.append(str(p))
    return " ".join(out)


def _locs(rec: dict, key: str = "source_locations") -> list[dict]:
    return list(rec.get(key) or [])


def candidates(paper: dict, paper_id: str) -> list[EvidenceCandidate]:
    """Complete records from a PaperAnalysis dict, each with a JSON Pointer
    value_path and the provenance_path of the record owning
    source_locations (result rows inherit their experiment's)."""
    out: list[EvidenceCandidate] = []
    es = paper.get("experimental_setup", {})
    hardware = _s(es.get("hardware"))
    metrics_by_name = {m["name"].lower(): m for m in paper.get("metrics", [])}

    def add(label, vpath, ppath, locs, value, context="", boundary=False):
        value = value.strip()
        if not value:
            return
        out.append(EvidenceCandidate(
            paper_id=paper_id, label=label, value_path=vpath,
            provenance_path=ppath, source_locations=locs, source_value=value,
            context=context.strip(), boundary=boundary,
            tokens=tokens(value + " " + context)))

    for i, k in enumerate(paper.get("key_results", [])):
        add("key result", f"/key_results/{i}", f"/key_results/{i}", _locs(k),
            _s(k["finding"], k.get("quantitative_result"), k.get("comparison")),
            _s(k.get("importance")))

    for i, e in enumerate(paper.get("experiments", [])):
        base = f"/experiments/{i}"
        ctx = _s(f"setup: {e['setup']}", f"comparison: {e['comparison']}",
                 f"datasets: {_s(e.get('datasets'))}", f"models: {_s(e.get('models'))}",
                 f"baselines: {_s(e.get('baselines'))}",
                 f"metrics: {_s(e.get('metrics'))}",
                 f"hardware: {hardware}" if hardware else "",
                 f"evidence_supports_conclusion: {e.get('evidence_supports_conclusion')}")
        add("experiment", base, base, _locs(e),
            _s(e["experiment_name"] + ":", e["key_result"], e.get("authors_conclusion")),
            ctx)
        for j, r in enumerate(e.get("results", [])):
            metric = metrics_by_name.get(str(r.get("metric", "")).lower(), {})
            mctx = _s(f"metric definition: {metric.get('what_it_measures')}"
                      if metric else "",
                      f"better: {metric.get('better_direction')}" if metric else "")
            val = r.get("raw_value") if r.get("raw_value") not in (None, "") else r.get("value")
            add("experiment result", f"{base}/results/{j}", base, _locs(e),
                _s(f"{r['method']} — {r['metric']} = {val}",
                   r.get("unit"), f"({r['dataset_or_setting']})"
                   if r.get("dataset_or_setting") else ""),
                _s(f"experiment: {e['experiment_name']}", f"setup: {e['setup']}",
                   f"hardware: {hardware}" if hardware else "", mctx))

    for i, a in enumerate(paper.get("ablations", [])):
        add("ablation", f"/ablations/{i}", f"/ablations/{i}", _locs(a),
            _s(f"{a['component_or_variable']}: {a['change']}.", a["result"],
               a.get("quantitative_change"), a.get("interpretation")),
            _s(f"baseline: {a.get('baseline_setting')}",
               f"modified: {a.get('modified_setting')}",
               f"motivation: {a.get('motivation')}",
               f"hardware: {hardware}" if hardware else ""))

    for i, sc in enumerate(paper.get("scaling_and_sensitivity", [])):
        add("scaling / sensitivity", f"/scaling_and_sensitivity/{i}",
            f"/scaling_and_sensitivity/{i}", _locs(sc),
            _s(f"{sc['variable']} over {sc['values_tested']}:", sc["observed_trend"],
               sc.get("interpretation")))

    for i, f in enumerate(paper.get("interesting_findings", [])):
        add("interesting finding", f"/interesting_findings/{i}",
            f"/interesting_findings/{i}", _locs(f),
            _s(f["finding"], f.get("possible_implication")),
            _s(f"expected_or_unexpected: {f.get('expected_or_unexpected')}",
               f.get("why_interesting")),
            boundary=f.get("expected_or_unexpected") == "unexpected")

    for i, fc in enumerate(paper.get("failure_cases", [])):
        add("failure case", f"/failure_cases/{i}", f"/failure_cases/{i}", _locs(fc),
            _s(fc["failure"], f"under: {fc.get('conditions')}", fc.get("observed_behavior")),
            _s(f"possible cause: {fc.get('possible_cause')}", fc.get("implication")),
            boundary=True)

    lim = paper.get("limitations", {})
    for i, l in enumerate(lim.get("author_stated", [])):
        add("author-stated limitation", f"/limitations/author_stated/{i}",
            f"/limitations/author_stated/{i}", _locs(l),
            _s(l["limitation"], l.get("impact")), boundary=True)
    for i, l in enumerate(lim.get("inferred", [])):
        add(f"extractor-inferred limitation (severity {l.get('severity')})",
            f"/limitations/inferred/{i}", f"/limitations/inferred/{i}", _locs(l),
            _s(l["limitation"]), _s(f"reasoning: {l.get('reasoning')}",
                                    f"evidence: {l.get('evidence')}"),
            boundary=True)

    for i, c in enumerate(paper.get("claims_and_evidence", [])):
        add(f"claim ({c.get('claim_type')}, evidence {c.get('evidence_strength')})",
            f"/claims_and_evidence/{i}", f"/claims_and_evidence/{i}", _locs(c),
            _s(c["claim"], f"evidence: {_s(c.get('evidence'))}"), _s(c.get("reasoning")))

    m = paper.get("method", {})
    if m:
        arch = m.get("architecture", {})
        add("method: high-level idea", "/method/high_level_idea", "/method/architecture",
            _locs(arch), _s(m.get("high_level_idea")), _s(arch.get("description")))
        for i, c in enumerate(m.get("components", [])):
            add("method component", f"/method/components/{i}",
                f"/method/components/{i}", _locs(c),
                _s(f"{c['name']}: {c['purpose']}.", c.get("technical_details")),
                _s(f"novel_component: {c.get('novel_component')}"))
        sysc = m.get("systems_characteristics", {})
        if sysc:
            add("systems characteristics", "/method/systems_characteristics",
                "/method/systems_characteristics", _locs(sysc),
                _s(f"latency: {sysc.get('latency')}" if sysc.get("latency") else "",
                   f"throughput: {sysc.get('throughput')}" if sysc.get("throughput") else "",
                   f"memory: {sysc.get('memory')}" if sysc.get("memory") else "",
                   f"communication: {sysc.get('communication_cost')}"
                   if sysc.get("communication_cost") else "", sysc.get("other")))

    for i, c in enumerate(paper.get("contributions", [])):
        add(f"contribution ({c.get('contribution_type')})", f"/contributions/{i}",
            f"/contributions/{i}", _locs(c),
            _s(c["contribution"], c.get("novelty_claim")), _s(c.get("what_this_paper_changes")))

    gap = paper.get("research_gap")
    if gap:
        add("research gap", "/research_gap", "/research_gap", _locs(gap),
            _s(gap["gap_description"]), _s(gap.get("limitations_of_prior_work")))

    fw = paper.get("future_work", {})
    for i, f in enumerate(fw.get("author_proposed", [])):
        add("author future work", f"/future_work/author_proposed/{i}",
            f"/future_work/author_proposed/{i}", _locs(f),
            _s(f["direction"], f.get("motivation")), boundary=True)
    for i, o in enumerate(fw.get("inferred_research_opportunities", [])):
        add(f"inferred opportunity (confidence {o.get('confidence')})",
            f"/future_work/inferred_research_opportunities/{i}",
            f"/future_work/inferred_research_opportunities/{i}",
            _locs(o, "supporting_source_locations"),
            _s(o["observation_or_limitation"], o.get("research_question")),
            _s(o.get("why_it_matters")), boundary=True)

    if es:
        add("evaluation setup", "/experimental_setup", "/experimental_setup", _locs(es),
            _s(f"hardware: {hardware}" if hardware else "",
               f"software: {_s(es.get('software'))}" if es.get("software") else "",
               f"runs: {es.get('number_of_runs')}" if es.get("number_of_runs") else "",
               es.get("other_details")))
    return out


# --------------------------------------------------------------------------
# Bundles
# --------------------------------------------------------------------------

@dataclass
class BundleBudget:
    max_chars: int = 12_000
    boundary_share: float = 0.3
    max_item_chars: int = 1_500


def relevance(c: EvidenceCandidate, query_tokens: set[str],
              hint_tokens: set[str] | None = None) -> float:
    if not query_tokens:
        return 0.0
    score = len(c.tokens & query_tokens) / len(query_tokens)
    if hint_tokens:
        score += 0.5 * len(c.tokens & hint_tokens) / max(1, len(hint_tokens))
    return score


def bundle(thread_id: str, statement: str, thread_papers: list[str],
           cands_by_paper: dict[str, list[EvidenceCandidate]],
           query_text: str, budget: BundleBudget | None = None,
           hints: dict[str, set[str]] | None = None) -> EvidenceBundle:
    """Per-paper allocation (max_chars / n_papers), a reserved share for
    boundary items, then relevance fill. Deterministic: ties break on
    value_path. Items over max_item_chars are cut and flagged truncated."""
    budget = budget or BundleBudget()
    q = tokens(query_text)
    n = max(1, len(thread_papers))
    per_paper = budget.max_chars // n
    items: list[BundleItem] = []
    omitted: list[OmittedItem] = []
    k = 0
    for pid in thread_papers:
        cands = cands_by_paper.get(pid, [])
        hint = (hints or {}).get(pid)
        ranked = sorted(cands, key=lambda c: (-relevance(c, q, hint), c.value_path))
        boundary_first = [c for c in ranked if c.boundary]
        rest = [c for c in ranked if not c.boundary]
        reserve = int(per_paper * budget.boundary_share)
        used = 0
        chosen: list[EvidenceCandidate] = []
        for c in boundary_first:
            size = min(len(c.source_value) + len(c.context), budget.max_item_chars)
            if used + size <= reserve:
                chosen.append(c)
                used += size
        # fill remaining allocation by relevance across everything not yet chosen
        pool = [c for c in ranked if c not in chosen]
        for c in pool:
            size = min(len(c.source_value) + len(c.context), budget.max_item_chars)
            if used + size <= per_paper:
                chosen.append(c)
                used += size
            else:
                omitted.append(OmittedItem(paper_id=pid, value_path=c.value_path,
                                           reason="budget"))
        for c in sorted(chosen, key=lambda c: c.value_path):
            k += 1
            value, ctx, trunc = c.source_value, c.context, False
            if len(value) + len(ctx) > budget.max_item_chars:
                trunc = True
                room = budget.max_item_chars
                value = value[:room]
                ctx = ctx[:max(0, room - len(value))]
            items.append(BundleItem(
                evidence_id=f"{thread_id}-e{k:03d}", paper_id=pid, label=c.label,
                value_path=c.value_path, provenance_path=c.provenance_path,
                origin=limitation_origin(c.value_path),
                source_locations=c.source_locations, source_value=value,
                context=ctx, boundary=c.boundary, truncated=trunc))
    return EvidenceBundle(thread_id=thread_id, statement=statement,
                          thread_papers=list(thread_papers), items=items,
                          omitted=omitted)
