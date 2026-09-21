"""Extract ONE paper end-to-end: PDF → markdown → paper.json → summary.md,
uploading artifacts to S3 as soon as they are produced.

This function is the unit of work for all ingestion: the backfill driver
(ingestion/backfill.py) calls it for every downloaded paper, and a future
upload API / queue consumer will call the same function per uploaded PDF.
No deployment is required — running it directly (laptop, EC2 + tmux) is the
intended mode.

Stages (each skipped when its artifact already exists, so reruns resume):

    1. markdown/<paper_id>/NN.md      extraction.pdf_to_markdown (per-page resume)
    2. upload pages to S3             pages carry ~90% of the LLM cost; make
                                      them durable before the later stages
    3. markdown/<paper_id>/paper.json extraction.research_extract
    4. markdown/<paper_id>/summary.md extraction.extract_summary
    5. upload paper.json + summary.md to S3

Indexing (flatten → embed → OpenSearch) is deliberately NOT here: it reads
paper.json back from S3 in a separate pass so indexing choices can change
without re-running extraction. See docs/offline_ingestion_design.md.

Usage:
    uv run python -m ingestion.worker <paper_id>
    uv run python -m ingestion.worker <paper_id> --no-upload
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from extraction.extract_summary import summarize
from extraction.pdf_to_markdown import convert_pdf
from extraction.research_extract import extract_research
from indexing.ids import sanitize_filename
from ingestion.s3store import ArtifactStore

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


def pdf_path_for(meta: dict, papers_root: Path) -> Path:
    """Where the scrapers put this paper's PDF:
    papers/<sanitized venueid>/<sanitized title>.pdf"""
    return (papers_root / sanitize_filename(meta["venueid"])
            / f"{sanitize_filename(meta['title'])}.pdf")


def process_paper(meta: dict, *,
                  markdown_root: Path,
                  papers_root: Path,
                  store: ArtifactStore | None = None) -> None:
    """Run all extraction stages for one paper. Raises on failure; safe to
    rerun — completed stages are skipped."""
    pid = meta["paper_id"]
    folder = markdown_root / pid

    pdf = pdf_path_for(meta, papers_root)
    if not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    # 1. PDF -> per-page markdown (resumes page-wise; exits nonzero when
    # some pages fail, which we surface as an ordinary failure).
    try:
        convert_pdf(pdf, markdown_root, folder_name=pid)
    except SystemExit as e:
        raise RuntimeError(f"pdf_to_markdown: {e}") from None

    # 2. Pages to S3 before the (single-call, failure-prone) LLM stages.
    if store:
        n = store.upload_folder(folder, pid)
        if n:
            print(f"[{pid}] uploaded {n} files to {store.url(pid)}")

    # 3. Structured extraction -> paper.json
    paper_json = folder / "paper.json"
    if not paper_json.exists():
        record = extract_research(folder)
        paper_json.write_text(json.dumps(record, indent=2, ensure_ascii=False))
        print(f"[{pid}] wrote paper.json")

    # 4. Detailed summary -> summary.md
    summary_md = folder / "summary.md"
    if not summary_md.exists():
        summary_md.write_text(summarize(folder) + "\n")
        print(f"[{pid}] wrote summary.md")

    # 5. Remaining artifacts to S3.
    if store:
        n = store.upload_folder(folder, pid)
        if n:
            print(f"[{pid}] uploaded {n} files to {store.url(pid)}")


def load_metadata(path: Path) -> list[dict]:
    entries = json.loads(path.read_text())
    missing = [e["title"] for e in entries if not e.get("paper_id")]
    if missing:
        sys.exit(f"{len(missing)} metadata entries have no paper_id "
                 f"(e.g. {missing[0]!r}); re-run the scraper or backfill ids.")
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paper_id", help="paper_id from papers/metadata.json")
    parser.add_argument("--metadata", default="papers/metadata.json")
    parser.add_argument("--markdown-root", default="markdown")
    parser.add_argument("--papers-root", default="papers")
    parser.add_argument("--no-upload", action="store_true",
                        help="Skip the S3 upload stages")
    args = parser.parse_args()

    entries = load_metadata(REPO_ROOT / args.metadata)
    meta = next((e for e in entries if e["paper_id"] == args.paper_id), None)
    if not meta:
        sys.exit(f"paper_id {args.paper_id} not found in {args.metadata}")

    store = None if args.no_upload else ArtifactStore()
    process_paper(meta,
                  markdown_root=REPO_ROOT / args.markdown_root,
                  papers_root=REPO_ROOT / args.papers_root,
                  store=store)
    print(f"Done: {meta['title']}")


if __name__ == "__main__":
    main()
