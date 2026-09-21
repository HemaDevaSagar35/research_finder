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

Total LLM concurrency ≈ --workers × LLM_CONCURRENCY (page conversion inside
one paper is already concurrent), so keep --workers small.

Usage:
    uv run python -m ingestion.backfill --status          # progress summary
    uv run python -m ingestion.backfill --limit 5         # try 5 papers
    uv run python -m ingestion.backfill --venues ICML.cc/2026/Conference
    uv run python -m ingestion.backfill --workers 2
    uv run python -m ingestion.backfill --retry-failed
"""

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from ingestion.s3store import ArtifactStore
from ingestion.worker import load_metadata, pdf_path_for, process_paper

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    entries = load_metadata(REPO_ROOT / args.metadata)
    if args.venues:
        entries = [e for e in entries if e["venueid"] in args.venues]
    # metadata can legitimately contain duplicates across reruns; keep first
    seen, papers = set(), []
    for e in entries:
        if e["paper_id"] not in seen:
            seen.add(e["paper_id"])
            papers.append(e)

    statuses = dict(con.execute("SELECT paper_id, status FROM papers"))
    skip = {"done"} | (set() if args.retry_failed else {"failed"})
    papers_root = REPO_ROOT / args.papers_root
    todo, no_pdf = [], 0
    for meta in papers:
        if statuses.get(meta["paper_id"]) in skip:
            continue
        if not pdf_path_for(meta, papers_root).exists():
            no_pdf += 1
            set_status(con, meta, "no_pdf")
            continue
        todo.append(meta)
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(papers)} papers in metadata | "
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

    def run_one(meta: dict):
        process_paper(meta, markdown_root=markdown_root,
                      papers_root=papers_root, store=store)

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
            except BaseException as e:  # noqa: BLE001 — record and continue
                failed += 1
                set_status(con, meta, "failed", f"{type(e).__name__}: {e}")
                print(f"== [{done + failed}/{len(todo)}] FAILED "
                      f"({type(e).__name__}: {str(e)[:120]}): "
                      f"{meta['title'][:70]}")

    print(f"\nRun finished: {done} done, {failed} failed.")
    print_status(con)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
