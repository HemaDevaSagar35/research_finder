"""Flatten paper.json files into typed retrieval records.

Scans markdown root folders for <paper_folder>/paper.json, links each folder
back to the download metadata (papers/metadata.json) to get its canonical
paper_id, and emits:

    index/records.jsonl   one self-contained statement per line:
                          {record_id, paper_id, type, text, source_locations}
    index/papers.jsonl    one row per paper: id, title, authors, venue, year,
                          abstract, urls, folder (evidence location), tags

Records are what gets embedded / BM25-indexed. Small and single-topic on
purpose: one vector per statement retrieves far better than one vector per
paper. Types mirror the PaperAnalysis schema (key_result, claim, limitation,
research_gap, ...), so downstream stages can retrieve over specific kinds of
statements.

Usage:
    uv run python -m indexing.flatten                       # scan markdown/
    uv run python -m indexing.flatten --roots markdown_test # other roots
"""

import argparse
import json
import sys
from pathlib import Path

from .ids import markdown_folder_name, openreview_paper_id, paper_id

REPO_ROOT = Path(__file__).resolve().parent.parent


def _txt(*parts) -> str:
    """Join labeled sentence parts, skipping empties.

    Each part is a string, a (label, value) pair, or None. Values may be
    lists (joined with '; '). Empty/None values are dropped.
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


def flatten_paper(paper: dict, pid: str) -> list[dict]:
    """Turn one PaperAnalysis dict into a list of typed records."""
    records = []

    def add(rtype: str, text: str, srcs: list | None = None) -> None:
        text = text.strip()
        if not text:
            return
        n = sum(1 for r in records if r["type"] == rtype)
        records.append({
            "record_id": f"{pid}#{rtype}/{n}",
            "paper_id": pid,
            "type": rtype,
            "text": text,
            "source_locations": srcs or [],
        })

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
        ("Why difficult", p["why_difficult"]),
        ("Research questions", p["research_questions"]),
        ("Hypotheses", p["hypotheses"])), p["source_locations"])

    g = paper["research_gap"]
    add("research_gap", _txt(
        g["gap_description"],
        ("Prior work", g["what_existed_before"]),
        ("Limitations of prior work", g["limitations_of_prior_work"]),
        ("Why insufficient", g["why_existing_methods_are_insufficient"])),
        g["source_locations"])

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
            ("Type", c["contribution_type"]),
            ("Novelty", c["novelty_claim"]),
            ("What existed before", c["what_existed_before"]),
            ("What this changes", c["what_this_paper_changes"]),
            ("Why it matters", c["why_it_matters"])), c["source_locations"])

    m = paper["method"]
    add("method", _txt(
        m["high_level_idea"],
        ("Architecture", m["architecture"]["description"]),
        ("Information flow", m["architecture"]["information_flow"])),
        m["architecture"]["source_locations"])
    for comp in m["components"]:
        add("method_component", _txt(
            ("Component", comp["name"]),
            ("Purpose", comp["purpose"]),
            ("Operations", comp["operations"]),
            comp["technical_details"]), comp["source_locations"])

    for e in paper["experiments"]:
        add("experiment", _txt(
            ("Experiment", e["experiment_name"]),
            ("Research question", e["research_question"]),
            ("Setup", e["setup"]),
            ("Key result", e["key_result"]),
            ("Conclusion", e["authors_conclusion"])), e["source_locations"])

    for a in paper["ablations"]:
        add("ablation", _txt(
            ("Ablation", a["component_or_variable"]),
            ("Change", a["change"]),
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
            f["source_locations"])

    for fc in paper["failure_cases"]:
        add("failure_case", _txt(
            ("Failure", fc["failure"]),
            ("Conditions", fc["conditions"]),
            ("Observed behavior", fc["observed_behavior"]),
            ("Possible cause", fc["possible_cause"]),
            ("Implication", fc["implication"])), fc["source_locations"])

    for lim in paper["limitations"]["author_stated"]:
        add("limitation_author", _txt(
            lim["limitation"], ("Impact", lim["impact"])),
            lim["source_locations"])
    for lim in paper["limitations"]["inferred"]:
        add("limitation_inferred", _txt(
            lim["limitation"],
            ("Reasoning", lim["reasoning"]),
            ("Evidence", lim["evidence"])), lim["source_locations"])

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
            op["supporting_source_locations"])

    for cl in paper["claims_and_evidence"]:
        add("claim", _txt(
            cl["claim"],
            ("Evidence", cl["evidence"]),
            ("Evidence strength", cl["evidence_strength"])),
            cl["source_locations"])

    tags = paper["retrieval_tags"]
    add("tags", "; ".join(t for values in tags.values() for t in values))

    return records


def _paper_row(pid: str, paper: dict, meta: dict | None, folder: Path) -> dict:
    """One papers.jsonl row, merging download metadata with extracted fields."""
    pm = paper["paper_metadata"]
    return {
        "paper_id": pid,
        "title": (meta or {}).get("title") or pm["title"],
        "authors": (meta or {}).get("authors") or pm["authors"],
        "venue": (meta or {}).get("venue") or pm["venue"],
        "venueid": (meta or {}).get("venueid"),
        "year": pm["year"],
        "abstract": (meta or {}).get("abstract"),
        "openreview_id": (meta or {}).get("id"),
        "forum_url": (meta or {}).get("forum_url"),
        "folder": str(folder.relative_to(REPO_ROOT)),
        "research_areas": pm["research_areas"],
        "keywords": pm["keywords"],
        "retrieval_tags": paper["retrieval_tags"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--roots", nargs="+", default=["markdown"],
                        help="Folders to scan for <paper>/paper.json "
                             "(default: markdown)")
    parser.add_argument("--metadata", default="papers/metadata.json",
                        help="Download metadata for paper_id linkage "
                             "(default: papers/metadata.json)")
    parser.add_argument("--out-dir", default="index",
                        help="Output directory (default: index/)")
    args = parser.parse_args()

    # Folder-name -> metadata entry. Folders are preferably named by paper_id
    # directly; title-derived names (the same sanitization the scrapper used
    # to name PDFs) are kept as a fallback for folders made before that.
    by_folder: dict[str, dict] = {}
    meta_path = REPO_ROOT / args.metadata
    if meta_path.exists():
        for entry in json.loads(meta_path.read_text()):
            pid = entry.get("paper_id") or openreview_paper_id(
                entry["title"], entry["venueid"])
            entry["paper_id"] = pid
            by_folder[pid] = entry
            by_folder[markdown_folder_name(entry["title"])] = entry
    else:
        print(f"Note: no metadata at {meta_path}; "
              "falling back to paper.json metadata for ids.")

    all_records, all_papers, unmatched = [], [], []
    for root in args.roots:
        root_path = (REPO_ROOT / root) if not Path(root).is_absolute() else Path(root)
        for pj in sorted(root_path.glob("*/paper.json")):
            paper = json.loads(pj.read_text())
            meta = by_folder.get(pj.parent.name)
            if meta:
                pid = meta["paper_id"]
            else:
                pm = paper["paper_metadata"]
                pid = paper_id(pm["title"], pm["venue"] or "", pm["year"])
                unmatched.append(pj.parent.name)
            all_records.extend(flatten_paper(paper, pid))
            all_papers.append(_paper_row(pid, paper, meta, pj.parent))
            print(f"{pid}  {all_papers[-1]['title']}  "
                  f"({len(all_records)} records so far)")

    if not all_papers:
        sys.exit("No paper.json files found under: " + ", ".join(args.roots))
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} folders had no metadata match "
              f"(ids derived from paper.json instead): {unmatched}")

    out_dir = REPO_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "records.jsonl", "w") as f:
        for r in all_records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(out_dir / "papers.jsonl", "w") as f:
        for row in all_papers:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"\nWrote {len(all_records)} records from {len(all_papers)} papers "
          f"to {out_dir / 'records.jsonl'}")


if __name__ == "__main__":
    main()
