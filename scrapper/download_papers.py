"""Download accepted papers from OpenReview venues via the official API.

Requires an OpenReview account. Set credentials via environment variables:

    export OPENREVIEW_USERNAME="you@example.com"
    export OPENREVIEW_PASSWORD="your-password"

Takes any number of conferences (--venueids) and acceptance categories
(--venues); downloads everything accepted by default. PDFs are organized as
papers/<venueid>/, e.g. papers/ICML.cc_2026_Conference/.

Usage:
    python download_papers.py                          # everything accepted
    python download_papers.py --venues spotlight       # one category
    python download_papers.py --venues spotlight oral  # several categories
    python download_papers.py --venueids ICML.cc/2026/Conference NeurIPS.cc/2025/Conference
    python download_papers.py --limit 5                # for testing
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import openreview

API_BASE = "https://api2.openreview.net"
SITE_BASE = "https://openreview.net"


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name[:max_len].rstrip(" .") or "untitled"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--venueids", nargs="+", default=["ICML.cc/2026/Conference"],
                        help="One or more OpenReview venue ids "
                             "(default: ICML.cc/2026/Conference)")
    parser.add_argument("--venues", nargs="*", default=None,
                        help="Acceptance categories to keep, matched as "
                             "case-insensitive substrings of the paper's venue, "
                             "e.g. --venues spotlight oral. Default: all.")
    parser.add_argument("--out",
                        default=str(Path(__file__).resolve().parent.parent / "papers"),
                        help="Output directory (default: papers/ at the repo root)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after downloading this many papers (for testing)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Number of PDFs to download in parallel (default: 8)")
    args = parser.parse_args()

    username = os.environ.get("OPENREVIEW_USERNAME")
    password = os.environ.get("OPENREVIEW_PASSWORD")
    if not username or not password:
        sys.exit("Set OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD environment variables.")

    print("Logging in to OpenReview...")
    client = openreview.api.OpenReviewClient(
        baseurl=API_BASE, username=username, password=password)

    papers = []
    for venueid in args.venueids:
        print(f"[{venueid}] listing accepted papers (this can take a minute)...")
        notes = client.get_all_notes(content={"venueid": venueid})
        print(f"[{venueid}] found {len(notes)} accepted papers.")
        for note in notes:
            content = note.content
            papers.append({
                "id": note.id,
                "title": content.get("title", {}).get("value", "untitled"),
                "authors": content.get("authors", {}).get("value", []),
                "venue": content.get("venue", {}).get("value", ""),
                "venueid": venueid,
                "abstract": content.get("abstract", {}).get("value", ""),
                "forum_url": f"{SITE_BASE}/forum?id={note.id}",
                "pdf_url": f"{SITE_BASE}/pdf?id={note.id}",
            })

    total = len(papers)
    if args.venues:
        # accept both space- and comma-separated lists
        wanted = [v.lower()
                  for arg in args.venues for v in arg.split(",") if v]
        papers = [p for p in papers
                  if any(w in p["venue"].lower() for w in wanted)]
    print(f"{len(papers)} of {total} papers match venues {args.venues or 'ALL'}.")
    if args.limit:
        papers = papers[:args.limit]
        print(f"Limiting to first {len(papers)} papers.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(json.dumps(papers, indent=2))
    print(f"Saved metadata to {out_dir / 'metadata.json'}")

    pending = []
    for paper in papers:
        venue_dir = out_dir / sanitize_filename(paper["venueid"])
        venue_dir.mkdir(parents=True, exist_ok=True)
        fname = venue_dir / f"{sanitize_filename(paper['title'])}.pdf"
        if not fname.exists():
            pending.append((paper, fname))
    skipped = len(papers) - len(pending)
    if skipped:
        print(f"{skipped} papers already downloaded, {len(pending)} to go.")

    def fetch_one(paper: dict, fname: Path) -> None:
        data = client.get_attachment(paper["id"], "pdf")
        if data[:5] != b"%PDF-":
            raise RuntimeError("response is not a PDF")
        fname.write_bytes(data)

    failed, done = [], skipped
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(fetch_one, paper, fname): (paper, fname)
                   for paper, fname in pending}
        for future in as_completed(futures):
            paper, fname = futures[future]
            done += 1
            try:
                future.result()
                print(f"[{done}/{len(papers)}] Downloaded: {fname.name}")
            except Exception as e:
                failed.append(paper)
                print(f"[{done}/{len(papers)}] FAILED ({e}): {paper['title']}")

    print(f"\nDone. {len(papers) - len(failed)} downloaded, {len(failed)} failed, "
          f"saved in {out_dir.resolve()}")
    if failed:
        print("Failed papers:")
        for paper in failed:
            print(f"  - {paper['title']} ({paper['forum_url']})")
        sys.exit(1)


if __name__ == "__main__":
    main()
