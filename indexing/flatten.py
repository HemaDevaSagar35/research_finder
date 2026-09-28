"""Flatten paper.json files into typed retrieval records (index contract v2).

Reads <paper_id>/paper.json artifacts — from the S3 artifact store with --s3
(the canonical source: extraction uploads there; see ingestion/), or from
local markdown roots — links them to the download metadata for canonical
paper_ids, and emits:

    index/records.jsonl   one self-contained statement per line:
                          {record_id, paper_id, type, text, source_locations,
                           meta}
    index/papers.jsonl    one row per paper: id, title, authors, venue, year,
                          abstract, urls, folder (evidence location),
                          reproducibility flags, facet arrays, tags

Which schema field lands where is defined by indexing/index_contract.py
(TEXT -> record text, META -> record.meta or the paper row, ONDEMAND -> not
here; read paper.json by paper_id). `python -m indexing.index_contract
--verify-flatten` checks that this module implements the contract.

Records are what gets embedded / BM25-indexed. Small and single-topic on
purpose: one vector per statement retrieves far better than one vector per
paper. Every record's text starts with the paper title so a statement
retrieved on its own still identifies its paper. `meta` carries the
denormalised filter fields (year, venue, research_areas + per-type flags
such as severity or claim_type) so record-level filters need no join.

Both outputs are written incrementally (papers stream through; nothing is
held in memory), so corpus size only affects disk, not RAM.

Usage:
    uv run python -m indexing.flatten --s3                  # S3 artifacts
    uv run python -m indexing.flatten                       # scan markdown/
    uv run python -m indexing.flatten --roots markdown_test # other roots
    uv run python -m indexing.flatten --s3 --paper-ids a b  # subset (testing)
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from .ids import markdown_folder_name, openreview_paper_id, paper_id

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# Records longer than this are split into sentence-aligned chunks (same type,
# meta and source_locations; record_id suffixed #p0, #p1, ...; every chunk
# keeps the title prefix). Chars are a deliberate proxy for tokens so records
# stay provider-agnostic. 4000 chars ~ 1000-1300 tokens: far inside
# Qwen3-Embedding's 32k window and OpenAI's 8k, and large enough that the
# one-record-per-paper types (summary, method, training, ...) stay in a single
# vector. The local bge-small fallback truncates at 512 tokens — acceptable
# for a dev model, not for production.
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "4000"))
_chunked = []

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` chars at sentence
    boundaries (hard-splitting only pathological run-on sentences)."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > limit:
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        if current and len(current) + len(sentence) + 1 > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def _txt(*parts) -> str:
    """Join labeled sentence parts, skipping empties.

    Each part is a string, a (label, value) pair, or None. Values may be
    lists (joined with '; ') or scalars. Empty/None values are dropped.
    """
    out = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            if part.strip():
                out.append(part.strip())
            continue
        label, value = part
        if value is None:
            continue
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value if v)
        value = str(value).strip()
        if value:
            out.append(f"{label}: {value}")
    return " ".join(out)


def _names(items: list[dict] | None, key: str = "name") -> list[str]:
    """Distinct, order-preserving non-empty `key` values of a list of dicts."""
    seen, out = set(), []
    for it in items or []:
        v = (it.get(key) or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def derive_year(*candidates) -> int | None:
    """First 4-digit year found in any candidate (int or string)."""
    for c in candidates:
        if isinstance(c, int):
            return c
        if isinstance(c, str):
            m = _YEAR_RE.search(c)
            if m:
                return int(m.group(0))
    return None


def _meta(base: dict, **extra) -> dict:
    """Record meta: denormalised paper fields + per-type META fields
    (None-valued extras are dropped)."""
    m = dict(base)
    m.update({k: v for k, v in extra.items() if v is not None})
    return m


def flatten_paper(paper: dict, pid: str, paper_meta: dict | None = None) -> list[dict]:
    """Turn one PaperAnalysis dict into a list of typed records.

    `paper_meta` (optional) overrides title/year/venue when the download
    metadata is more reliable than the extraction (venue and year usually
    are). Keys: title, year, venue.
    """
    pm = paper["paper_metadata"]
    paper_meta = paper_meta or {}
    title = (paper_meta.get("title") or pm["title"] or "").strip().rstrip(".")
    venue = paper_meta.get("venue") or pm["venue"]
    year = derive_year(paper_meta.get("year"), pm["year"], venue,
                       paper_meta.get("venueid"))
    base_meta = {"year": year, "venue": venue,
                 "research_areas": pm["research_areas"]}
    prefix = f"{title}. " if title else ""

    records = []
    counters: dict[str, int] = {}

    def add(rtype: str, text: str, srcs: list | None = None, **extra) -> None:
        text = text.strip()
        if not text:
            return
        n = counters.get(rtype, 0)
        counters[rtype] = n + 1
        base_id = f"{pid}#{rtype}/{n}"
        parts = _split_text(text, CHUNK_CHARS - len(prefix))
        if len(parts) > 1:
            _chunked.append(f"{base_id} ({len(parts)} chunks)")
        meta = _meta(base_meta, **extra)
        for k, part in enumerate(parts):
            records.append({
                "record_id": base_id if len(parts) == 1 else f"{base_id}#p{k}",
                "paper_id": pid,
                "type": rtype,
                "text": prefix + part,
                "source_locations": srcs or [],
                "meta": meta,
            })

    # ---- summary / problem / gap -------------------------------------------
    s = paper["high_level_summary"]
    add("summary", _txt(
        s["one_sentence_summary"],
        ("General problem", s["general_problem"]),
        ("Specific problem", s["specific_problem"]),
        ("Proposed solution", s["proposed_solution"]),
        ("Main result", s["main_result"]),
        ("Why it matters", s["why_it_matters"])))

    p = paper["research_problem"]
    add("problem", _txt(
        ("General problem", p["general_problem"]),
        ("Specific problem", p["specific_problem"]),
        ("Motivation", p["motivation"]),
        ("Why difficult", p["why_difficult"])), p["source_locations"])
    for q in p["research_questions"] or []:
        add("research_question", _txt(("Research question", q)),
            p["source_locations"])
    for h in p["hypotheses"] or []:
        add("hypothesis", _txt(("Hypothesis", h)), p["source_locations"])

    g = paper["research_gap"]
    add("research_gap", _txt(
        g["gap_description"],
        ("What existed before", g["what_existed_before"]),
        ("Limitations of prior work", g["limitations_of_prior_work"]),
        ("Why insufficient", g["why_existing_methods_are_insufficient"]),
        ("Evidence for gap", g["evidence_for_gap"])),
        g["source_locations"],
        gap_explicitly_claimed_by_authors=g["gap_explicitly_claimed_by_authors"])

    for w in paper["prior_work"]:
        add("prior_work", _txt(
            ("Prior method", w["method_or_family"]),
            w["description"],
            ("Strengths", w["strengths"]),
            ("Limitations", w["limitations"]),
            ("Relationship to this paper", w["relationship_to_current_paper"])),
            w["source_locations"])

    for c in paper["contributions"]:
        add("contribution", _txt(
            c["contribution"],
            ("Novelty", c["novelty_claim"]),
            ("What existed before", c["what_existed_before"]),
            ("What this changes", c["what_this_paper_changes"]),
            ("Why it matters", c["why_it_matters"])), c["source_locations"],
            contribution_type=c["contribution_type"],
            explicitly_claimed_by_authors=c["explicitly_claimed_by_authors"])

    # ---- method ------------------------------------------------------------
    m = paper["method"]
    arch = m["architecture"]
    add("method", _txt(
        m["high_level_idea"],
        ("Architecture", arch["description"]),
        ("Model components", arch["model_components"]),
        ("Information flow", arch["information_flow"])),
        arch["source_locations"])

    for st in m["pipeline"]:
        label = f"Step {st['step']}" if st.get("step") is not None else "Step"
        add("pipeline_step", _txt(
            (label, st["name"]),
            st["description"],
            ("Inputs", st["inputs"]),
            ("Outputs", st["outputs"])), st["source_locations"])

    for comp in m["components"]:
        add("method_component", _txt(
            ("Component", comp["name"]),
            ("Purpose", comp["purpose"]),
            ("Inputs", comp["inputs"]),
            ("Operations", comp["operations"]),
            ("Outputs", comp["outputs"]),
            comp["technical_details"]), comp["source_locations"],
            novel_component=comp["novel_component"])

    for ob in m["objectives_and_losses"]:
        add("objective", _txt(
            ("Objective", ob["name"]),
            ob["description"],
            ("Purpose", ob["purpose"])), ob["source_locations"])

    for eq in m["important_equations"]:
        add("equation", _txt(
            ("Equation meaning", eq["meaning"]),
            ("Importance", eq["importance"])), eq["source_locations"])

    tr = m["training"]
    add("training", _txt(
        ("Training procedure", tr["training_procedure"]),
        ("Training data", tr["training_data"]),
        ("Preprocessing", tr["preprocessing"]),
        ("Optimization", tr["optimization"]),
        ("Initialization", tr["initialization"]),
        ("Fine-tuning strategy", tr["fine_tuning_strategy"]),
        ("Regularization", tr["regularization"]),
        ("Compute requirements", tr["compute_requirements"])),
        tr["source_locations"])

    inf = m["inference"]
    add("inference", _txt(
        ("Inference procedure", inf["procedure"]),
        ("Decoding or sampling", inf["decoding_or_sampling"]),
        ("Complexity", inf["complexity"]),
        ("Latency", inf["latency_considerations"]),
        ("Throughput", inf["throughput_considerations"]),
        ("Memory", inf["memory_considerations"])))

    sy = m["systems_characteristics"]
    add("systems", _txt(
        ("Time complexity", sy["time_complexity"]),
        ("Space complexity", sy["space_complexity"]),
        ("FLOPs", sy["flops"]),
        ("Memory", sy["memory"]),
        ("Memory bandwidth", sy["memory_bandwidth"]),
        ("Communication cost", sy["communication_cost"]),
        ("Latency", sy["latency"]),
        ("Throughput", sy["throughput"]),
        ("Other systems characteristics", sy["other"])))

    # ---- models / datasets / baselines / metrics / setup -------------------
    for mo in paper["models"]:
        add("model", _txt(
            ("Model", mo["name"]),
            ("Role", mo["role"]),
            ("Architecture", mo["architecture"]),
            ("Parameters", mo["parameter_count"])))

    for ds in paper["datasets"]:
        add("dataset", _txt(
            ("Dataset", ds["name"]),
            ("Purpose", ds["purpose"]),
            ("Size", ds["size"]),
            ("Task", ds["task"]),
            ("Domain", ds["domain"]),
            ("Characteristics", ds["important_characteristics"])))

    for b in paper["baselines"]:
        add("baseline", _txt(
            ("Baseline", b["name"]),
            b["description"],
            ("Why selected", b["why_selected"]),
            ("Relationship to proposed method",
             b["relationship_to_proposed_method"])))

    for me in paper["metrics"]:
        add("metric", _txt(
            ("Metric", me["name"]),
            me["description"],
            ("What it measures", me["what_it_measures"])))

    es = paper["experimental_setup"]
    add("evaluation_setup", _txt(
        ("Hardware", es["hardware"]),
        ("Software", es["software"]),
        ("Statistical testing", es["statistical_testing"]),
        ("Other details", es["other_details"])))

    # ---- experiments / ablations / scaling / results -----------------------
    for e in paper["experiments"]:
        add("experiment", _txt(
            ("Experiment", e["experiment_name"]),
            ("Research question", e["research_question"]),
            ("Hypothesis tested", e["hypothesis_being_tested"]),
            ("Setup", e["setup"]),
            ("Comparison", e["comparison"]),
            ("Datasets", e["datasets"]),
            ("Models", e["models"]),
            ("Baselines", e["baselines"]),
            ("Metrics", e["metrics"]),
            ("Key result", e["key_result"]),
            ("Conclusion", e["authors_conclusion"])), e["source_locations"],
            evidence_supports_conclusion=e["evidence_supports_conclusion"],
            datasets=e["datasets"] or None, models=e["models"] or None,
            baselines=e["baselines"] or None, metrics=e["metrics"] or None)

    for a in paper["ablations"]:
        add("ablation", _txt(
            ("Ablation", a["component_or_variable"]),
            ("Change", a["change"]),
            ("Motivation", a["motivation"]),
            ("Baseline setting", a["baseline_setting"]),
            ("Modified setting", a["modified_setting"]),
            ("Result", a["result"]),
            ("Quantitative change", a["quantitative_change"]),
            ("Interpretation", a["interpretation"])), a["source_locations"])

    for sc in paper["scaling_and_sensitivity"]:
        add("scaling", _txt(
            ("Variable", sc["variable"]),
            ("Values tested", sc["values_tested"]),
            ("Trend", sc["observed_trend"]),
            ("Interpretation", sc["interpretation"])), sc["source_locations"])

    for k in paper["key_results"]:
        add("key_result", _txt(
            k["finding"],
            ("Quantitative result", k["quantitative_result"]),
            ("Comparison", k["comparison"]),
            ("Importance", k["importance"])), k["source_locations"])

    for f in paper["interesting_findings"]:
        add("interesting_finding", _txt(
            f["finding"],
            ("Why interesting", f["why_interesting"]),
            ("Possible implication", f["possible_implication"])),
            f["source_locations"],
            expected_or_unexpected=f["expected_or_unexpected"],
            authors_discuss_implication=f["authors_discuss_implication"])

    for ft in paper["important_figures_and_tables"]:
        add("figure_table", _txt(
            (ft["type"].capitalize() if ft.get("type") else "Figure/table",
             ft["identifier"]),
            ("Shows", ft["what_it_shows"]),
            ("Key observation", ft["key_observation"]),
            ("Importance", ft["importance"])), ft["source_locations"],
            type=ft["type"])

    for fc in paper["failure_cases"]:
        add("failure_case", _txt(
            ("Failure", fc["failure"]),
            ("Conditions", fc["conditions"]),
            ("Observed behavior", fc["observed_behavior"]),
            ("Possible cause", fc["possible_cause"]),
            ("Implication", fc["implication"])), fc["source_locations"])

    # ---- limitations / future work / claims --------------------------------
    for lim in paper["limitations"]["author_stated"]:
        add("limitation_author", _txt(
            lim["limitation"], ("Impact", lim["impact"])),
            lim["source_locations"])
    for lim in paper["limitations"]["inferred"]:
        add("limitation_inferred", _txt(
            lim["limitation"],
            ("Reasoning", lim["reasoning"]),
            ("Evidence", lim["evidence"])), lim["source_locations"],
            severity=lim["severity"])

    for fw in paper["future_work"]["author_proposed"]:
        add("future_work_author", _txt(
            fw["direction"], ("Motivation", fw["motivation"])),
            fw["source_locations"])
    for op in paper["future_work"]["inferred_research_opportunities"]:
        add("research_opportunity", _txt(
            ("Observation", op["observation_or_limitation"]),
            ("Research question", op["research_question"]),
            ("Why it matters", op["why_it_matters"]),
            ("Potential approach", op["potential_approach"])),
            op["supporting_source_locations"], confidence=op["confidence"])

    for cl in paper["claims_and_evidence"]:
        add("claim", _txt(
            cl["claim"],
            ("Evidence", cl["evidence"]),
            ("Reasoning", cl["reasoning"])), cl["source_locations"],
            claim_type=cl["claim_type"],
            evidence_strength=cl["evidence_strength"])

    # ---- reproducibility / assessment / tags -------------------------------
    rp = paper["reproducibility"]
    add("reproducibility", _txt(
        ("Required resources", rp["required_resources"]),
        ("Missing information", rp["missing_information"]),
        ("Reproducibility assessment", rp["reproducibility_assessment"])))

    pa = paper["paper_assessment"]
    add("paper_card", _txt(
        ("Most important contribution", pa["most_important_contribution"]),
        ("Most important result", pa["most_important_result"]),
        ("Most important limitation", pa["most_important_limitation"]),
        ("Strengths", pa["main_strengths"]),
        ("Weaknesses", pa["main_weaknesses"]),
        ("Demonstrates", pa["what_the_paper_demonstrates"]),
        ("Does not demonstrate", pa["what_the_paper_does_not_demonstrate"])))
    add("open_question", _txt(
        ("Open question", pa["most_interesting_open_question"])))

    tags = paper["retrieval_tags"]
    add("tags", "; ".join(
        t for values in [pm["keywords"], *tags.values()] for t in (values or []) if t))

    return records


def _paper_row(pid: str, paper: dict, meta: dict | None, folder: str) -> dict:
    """One papers.jsonl row, merging download metadata with extracted fields
    and the paper-level META facets from the index contract.
    `folder` is the evidence location: a repo-relative path or an s3:// URL."""
    pm = paper["paper_metadata"]
    meta = meta or {}
    es = paper["experimental_setup"]
    rp = paper["reproducibility"]
    venue = meta.get("venue") or pm["venue"]
    return {
        "paper_id": pid,
        "title": meta.get("title") or pm["title"],
        "authors": meta.get("authors") or pm["authors"],
        "venue": venue,
        "venueid": meta.get("venueid"),
        "year": derive_year(pm["year"], venue, meta.get("venueid")),
        "paper_type": pm["paper_type"],
        "identifiers": pm["identifiers"],
        "abstract": meta.get("abstract"),
        "openreview_id": meta.get("id"),
        "forum_url": meta.get("forum_url"),
        "folder": folder,
        "research_areas": pm["research_areas"],
        "keywords": pm["keywords"],
        "retrieval_tags": paper["retrieval_tags"],
        # facets (ALSO_META in the contract)
        "model_names": _names(paper["models"]),
        "dataset_names": _names(paper["datasets"]),
        "baseline_names": _names(paper["baselines"]),
        "metric_names": _names(paper["metrics"]),
        "hardware": es["hardware"],
        # paper-level META
        "number_of_runs": es["number_of_runs"],
        "code_available": rp["code_available"],
        "code_url": rp["code_url"],
        "data_available": rp["data_available"],
        "data_url": rp["data_url"],
        "model_checkpoints_available": rp["model_checkpoints_available"],
    }


def _metadata_lookup(entries: list[dict]) -> dict[str, dict]:
    """Folder-name -> metadata entry. Folders are preferably named by
    paper_id directly; title-derived names (the sanitization the scrapper
    used to name PDFs) are kept as a fallback for folders made before that."""
    by_folder: dict[str, dict] = {}
    for entry in entries:
        pid = entry.get("paper_id") or openreview_paper_id(
            entry["title"], entry["venueid"])
        entry["paper_id"] = pid
        by_folder[pid] = entry
        by_folder[markdown_folder_name(entry["title"])] = entry
    return by_folder


def _iter_local(roots: list[str], by_folder: dict):
    """Yield (paper dict, folder_name, evidence_location) from local roots."""
    for root in roots:
        root_path = (REPO_ROOT / root) if not Path(root).is_absolute() else Path(root)
        for pj in sorted(root_path.glob("*/paper.json")):
            try:
                folder = str(pj.parent.relative_to(REPO_ROOT))
            except ValueError:
                folder = str(pj.parent)
            yield json.loads(pj.read_text()), pj.parent.name, folder


def _iter_s3(store, only: set[str] | None = None, fetch_workers: int = 24):
    """Yield (paper dict, paper_id, evidence_location) streamed from the S3
    artifact store — objects are fetched concurrently and never hit disk."""
    pairs = store.list_paper_jsons()
    if only:
        pairs = [p for p in pairs if p[0] in only]
    print(f"{len(pairs)} paper.json artifacts in {store.url()}")
    with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        fetched = pool.map(lambda p: (p[0], store.get_json(p[1])), pairs)
        for pid, paper in fetched:
            yield paper, pid, store.url(pid)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--s3", action="store_true",
                        help="Read paper.json artifacts (and metadata.json) "
                             "from S3_ARTIFACTS_URL instead of local folders")
    parser.add_argument("--roots", nargs="+", default=["markdown"],
                        help="Local folders to scan for <paper>/paper.json "
                             "(default: markdown; ignored with --s3)")
    parser.add_argument("--metadata", default="papers/metadata.json",
                        help="Download metadata for paper_id linkage "
                             "(default: papers/metadata.json)")
    parser.add_argument("--out-dir", default="index",
                        help="Output directory (default: index/)")
    parser.add_argument("--paper-ids", nargs="+", default=None,
                        help="Only flatten these paper_ids / folder names "
                             "(testing)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after this many papers (testing)")
    args = parser.parse_args()

    store = None
    if args.s3:
        from ingestion.s3store import ArtifactStore
        store = ArtifactStore()

    # Metadata: from the artifact store in --s3 mode (the backfill uploads
    # it), falling back to the local file either way.
    entries = []
    if store and store.exists("metadata.json"):
        entries = store.get_json(store.key("metadata.json"))
    elif (REPO_ROOT / args.metadata).exists():
        entries = json.loads((REPO_ROOT / args.metadata).read_text())
    else:
        print(f"Note: no metadata found; "
              "falling back to paper.json metadata for ids.")
    by_folder = _metadata_lookup(entries)

    only = set(args.paper_ids) if args.paper_ids else None
    source = (_iter_s3(store, only) if store
              else _iter_local(args.roots, by_folder))

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    n_records = n_papers = 0
    unmatched = []
    type_counts: dict[str, int] = {}
    with open(out_dir / "records.jsonl", "w") as rec_f, \
            open(out_dir / "papers.jsonl", "w") as pap_f:
        for paper, name, folder in source:
            if only and not store and name not in only:
                continue
            meta = by_folder.get(name)
            if meta:
                pid = meta["paper_id"]
            else:
                pm = paper["paper_metadata"]
                pid = (name if args.s3 else
                       paper_id(pm["title"], pm["venue"] or "", pm["year"]))
                unmatched.append(name)
            records = flatten_paper(paper, pid, meta)
            for r in records:
                rec_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1
            row = _paper_row(pid, paper, meta, folder)
            pap_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_records += len(records)
            n_papers += 1
            if n_papers % 100 == 0 or not args.s3:
                print(f"[{n_papers}] {pid}  {row['title'][:70]}  "
                      f"({n_records} records so far)")
            if args.limit and n_papers >= args.limit:
                break

    if not n_papers:
        sys.exit("No paper.json files found in "
                 + (store.url() if store else ", ".join(args.roots)))
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} papers had no metadata match "
              f"(ids taken from the S3 prefix / derived from paper.json): "
              f"{unmatched[:10]}{'...' if len(unmatched) > 10 else ''}")
    if _chunked:
        print(f"\nNote: {len(_chunked)} records exceeded {CHUNK_CHARS} chars "
              f"and were split: "
              f"{_chunked[:10]}{'...' if len(_chunked) > 10 else ''}")

    print("\nRecords per type:")
    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:22s} {n:>9,}")
    print(f"\nWrote {n_records:,} records from {n_papers:,} papers "
          f"to {out_dir / 'records.jsonl'}")


if __name__ == "__main__":
    main()
