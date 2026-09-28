"""Run the extraction worker across the whole downloaded corpus, resumably.

Plain CLI, no deployment needed: run it on whatever machine has the PDFs
(laptop or EC2, inside tmux). Reads papers/metadata.json, resolves each
paper's PDF, and runs ingestion.worker.process_paper for N papers at a time.

Per-paper status lives in a SQLite manifest (ingestion_status.sqlite):
    done      all stages finished (skipped on reruns)
    failed    some stage raised (reprocessed only with --retry-failed)
    no_pdf    metadata entry whose PDF was never downloaded

The manifest is a convenience for fast skips and failure visibility — every
stage is also file-resumable on its own (existing pages / paper.json /
summary.md / S3 objects are skipped), so losing the manifest never re-spends
LLM calls.

Account-level LLM errors (no credits, bad key: HTTP 401/402/403) stop the
whole run with exit code 2 instead of marking paper after paper failed; the
affected papers keep their status, so after topping up just rerun the same
command.

Total LLM concurrency ≈ --workers × LLM_CONCURRENCY (page conversion inside
one paper is already concurrent), so keep --workers small.

Prioritising by research area: by default every paper in metadata.json is
extracted. With papers/ml_classification.json (from extraction.classify_ml)
present, --areas restricts a run to papers whose classification lists any of
the given areas, and --ml-only drops NON_ML papers. Because the manifest
skips finished papers, "some areas first, then everything" is just two
runs: one with --areas, then one without.

Usage:
    uv run python -m ingestion.backfill --status          # progress summary
    uv run python -m ingestion.backfill --limit 5         # try 5 papers
    uv run python -m ingestion.backfill --venues ICML.cc/2026/Conference
    uv run python -m ingestion.backfill --workers 2
    uv run python -m ingestion.backfill --retry-failed
    uv run python -m ingestion.backfill --areas "LLM Inference and Serving" \
        "Mixture-of-Experts Systems"                      # PRIMARY areas only
    uv run python -m ingestion.backfill --areas "Agents / Agentic Systems" \
        --include-secondary
    uv run python -m ingestion.backfill --ml-only         # everything but NON_ML
    uv run python -m ingestion.backfill --list-areas
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from ingestion.s3store import ArtifactStore
from ingestion.worker import load_metadata, pdf_locator, process_paper
from llm_client import errors as llm_errors
from llm_client import usage

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------ classification filters

def load_classification(path: Path) -> dict[str, dict]:
    """paper_id -> record from extraction.classify_ml's output."""
    return json.loads(path.read_text()) if path.exists() else {}


def _norm_area(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def resolve_areas(wanted: list[str], known: list[str]) -> list[str]:
    """Map user-typed area names onto the canonical ones found in the
    classification file: exact (case/punctuation-insensitive), then the
    classifier's aliases ('moe', 'rag', 'agents', ...), then a unique
    substring match. Exits on no match or an ambiguous one."""
    from extraction.classify_ml import alias_area  # alias table lives there

    out = []
    for w in wanted:
        nw = _norm_area(w)
        hits = [k for k in known if _norm_area(k) == nw]
        if not hits:
            alias = alias_area(w)
            hits = [alias] if alias in known else []
        if not hits:
            hits = [k for k in known if nw in _norm_area(k)]
        if len(hits) != 1:
            sys.exit(f"--areas {w!r}: {'no match' if not hits else 'ambiguous'} "
                     f"among {known}")
        if hits[0] not in out:
            out.append(hits[0])
    return out


def known_areas(classification: dict[str, dict]) -> list[str]:
    seen: dict[str, None] = {}
    for rec in classification.values():
        for a in rec.get("research_areas", []):
            seen.setdefault(a["area"])
    return list(seen)


def in_areas(rec: dict | None, areas: set[str], include_secondary: bool) -> bool:
    if not rec:
        return False
    for a in rec.get("research_areas", []):
        if a["area"] in areas and (include_secondary
                                   or a.get("relevance") == "PRIMARY"):
            return True
    return False


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            paper_id TEXT PRIMARY KEY,
            title TEXT, venueid TEXT,
            status TEXT, error TEXT, updated_at TEXT
        )""")
    return con


def set_status(con: sqlite3.Connection, meta: dict, status: str,
               error: str | None = None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?)",
        (meta["paper_id"], meta["title"], meta["venueid"], status,
         error, time.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()


def print_status(con: sqlite3.Connection) -> None:
    counts = dict(con.execute(
        "SELECT status, COUNT(*) FROM papers GROUP BY status"))
    print("Manifest:", json.dumps(counts, indent=2) if counts else "empty")
    failures = con.execute(
        "SELECT paper_id, title, error FROM papers WHERE status='failed' "
        "ORDER BY updated_at DESC LIMIT 10").fetchall()
    if failures:
        print("\nMost recent failures:")
        for pid, title, error in failures:
            print(f"  {pid}  {title[:60]}\n      {str(error)[:160]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--metadata", default="papers/metadata.json")
    parser.add_argument("--markdown-root", default="markdown")
    parser.add_argument("--papers-root", default="papers")
    parser.add_argument("--status-db", default="ingestion_status.sqlite")
    parser.add_argument("--venues", nargs="+", default=None,
                        help="Only these venueids (default: all)")
    parser.add_argument("--classification",
                        default="papers/ml_classification.json",
                        help="extraction.classify_ml output used by --areas / "
                             "--ml-only (default: papers/ml_classification.json)")
    parser.add_argument("--areas", nargs="+", default=None, metavar="AREA",
                        help="Only papers classified into any of these research "
                             "areas (names as in the classification file; "
                             "case-insensitive, substrings OK). Default: all")
    parser.add_argument("--include-secondary", action="store_true",
                        help="With --areas, also match SECONDARY relevance "
                             "(default: PRIMARY only)")
    parser.add_argument("--ml-only", action="store_true",
                        help="Skip papers classified NON_ML (unclassified papers "
                             "are kept)")
    parser.add_argument("--list-areas", action="store_true",
                        help="Print the research areas in the classification "
                             "file with paper counts, and exit")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most this many papers this run")
    parser.add_argument("--workers", type=int, default=2,
                        help="Papers processed concurrently (default: 2)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Also reprocess papers marked failed")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip the S3 upload stages")
    parser.add_argument("--status", action="store_true",
                        help="Print the manifest summary and exit")
    args = parser.parse_args()

    con = open_db(REPO_ROOT / args.status_db)
    if args.status:
        print_status(con)
        return

    classification: dict[str, dict] = {}
    if args.areas or args.ml_only or args.list_areas:
        cls_path = REPO_ROOT / args.classification
        classification = load_classification(cls_path)
        if not classification:
            sys.exit(f"Classification file not found or empty: {cls_path} "
                     f"(run `uv run python -m extraction.classify_ml` first).")
    if args.list_areas:
        counts: dict[str, list[int]] = {}
        for rec in classification.values():
            for a in rec.get("research_areas", []):
                c = counts.setdefault(a["area"], [0, 0])
                c[0 if a.get("relevance") == "PRIMARY" else 1] += 1
        print(f"{'area':<40} {'PRIMARY':>8} {'SECONDARY':>10}")
        for area, (p, s) in sorted(counts.items(), key=lambda kv: -kv[1][0]):
            print(f"{area:<40} {p:>8} {s:>10}")
        return

    entries = load_metadata(REPO_ROOT / args.metadata)
    if args.venues:
        entries = [e for e in entries if e["venueid"] in args.venues]
    # metadata can legitimately contain duplicates across reruns; keep first
    seen, papers = set(), []
    for e in entries:
        if e["paper_id"] not in seen:
            seen.add(e["paper_id"])
            papers.append(e)

    selection = []
    if args.ml_only:
        before = len(papers)
        papers = [p for p in papers
                  if classification.get(p["paper_id"], {}).get("label") != "NON_ML"]
        selection.append(f"--ml-only dropped {before - len(papers)} NON_ML")
    if args.areas:
        areas = resolve_areas(args.areas, known_areas(classification))
        before = len(papers)
        papers = [p for p in papers
                  if in_areas(classification.get(p["paper_id"]), set(areas),
                              args.include_secondary)]
        selection.append(f"--areas {areas} "
                         f"({'PRIMARY+SECONDARY' if args.include_secondary else 'PRIMARY'})"
                         f" kept {len(papers)} of {before}")
    if selection:
        print(" | ".join(selection))

    statuses = dict(con.execute("SELECT paper_id, status FROM papers"))
    skip = {"done"} | (set() if args.retry_failed else {"failed"})
    papers_root = REPO_ROOT / args.papers_root
    # Finds PDFs locally or in S3_PAPERS_URL (either layout); one listing.
    locator = pdf_locator(papers_root)
    todo, no_pdf = [], 0
    for meta in papers:
        if statuses.get(meta["paper_id"]) in skip:
            continue
        if not locator.available(meta):
            no_pdf += 1
            set_status(con, meta, "no_pdf")
            continue
        todo.append(meta)
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(papers)} papers selected | "
          f"{sum(1 for s in statuses.values() if s == 'done')} done | "
          f"{no_pdf} without PDFs | {len(todo)} to process now "
          f"({args.workers} workers)")
    if not todo:
        print_status(con)
        return

    store = None
    if not args.no_upload:
        store = ArtifactStore()
        store.upload_file(REPO_ROOT / args.metadata, "metadata.json")
        print(f"Uploaded metadata.json to {store.url('metadata.json')}")

    markdown_root = REPO_ROOT / args.markdown_root
    done = failed = 0
    started = time.time()
    usage.reset()

    def run_one(meta: dict):
        process_paper(meta, markdown_root=markdown_root,
                      papers_root=papers_root, store=store, locator=locator)

    fatal: BaseException | None = None
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, meta): meta for meta in todo}
        for future in as_completed(futures):
            meta = futures[future]
            try:
                future.result()
                done += 1
                set_status(con, meta, "done")
                print(f"== [{done + failed}/{len(todo)}] done: "
                      f"{meta['title'][:70]}")
            except FileNotFoundError as e:
                failed += 1
                set_status(con, meta, "no_pdf", str(e))
                print(f"== [{done + failed}/{len(todo)}] NO PDF: "
                      f"{meta['title'][:70]}")
            except BaseException as e:  # noqa: BLE001 — record and continue
                if llm_errors.is_fatal(e):
                    # Not this paper's fault: leave its status alone so a
                    # plain rerun picks it up, and stop feeding the pool.
                    fatal = e
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                failed += 1
                set_status(con, meta, "failed", f"{type(e).__name__}: {e}")
                print(f"== [{done + failed}/{len(todo)}] FAILED "
                      f"({type(e).__name__}: {str(e)[:120]}): "
                      f"{meta['title'][:70]}")
        # (the `with` waits here for the up-to-`workers` papers already
        # running; after a fatal error they fail fast and are not recorded)

    if fatal is not None:
        print(f"\n!! STOPPED: the LLM provider is rejecting requests — "
              f"{llm_errors.describe(fatal)}\n"
              f"   {done} done, {failed} failed before this. Papers that were "
              f"in flight keep their previous status; completed pages are on "
              f"disk/S3, so fix the account (credits / API key) and rerun the "
              f"same command — it resumes where it stopped.")

    elapsed = time.time() - started
    print(f"\nRun finished: {done} done, {failed} failed in {elapsed / 60:.1f} min"
          + (f" ({elapsed / done:.0f} s/paper wall-clock at {args.workers} workers)"
             if done else ""))
    print("\nLLM usage this run:")
    print(usage.report(per_unit=done or None))
    print_status(con)
    if fatal is not None:
        sys.exit(2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
