"""Download accepted papers from a conference "virtual site" (eventhosts).

Some conferences (ECCV, CVPR, NeurIPS, ...) don't expose accepted papers via
the OpenReview API, but their virtual sites serve a static JSON with every
accepted paper and a direct PDF link on media.eventhosts.cc — no Cloudflare,
so plain threaded HTTP works (no browser needed).

Defaults target ECCV 2026. PDFs land in papers/<venueid>/ exactly like the
OpenReview scrappers, and the entries are MERGED into papers/metadata.json
(keyed by paper_id), so other venues' metadata is preserved.

Usage:
    uv run scrapper/virtual_download_papers.py                 # ECCV 2026
    uv run scrapper/virtual_download_papers.py --limit 3       # test
    uv run scrapper/virtual_download_papers.py \
        --json-url https://<site>/static/virtual/data/<conf>-orals-posters.json \
        --abstracts-url https://<site>/static/virtual/data/<conf>-abstracts.json \
        --site https://<site> --venueid <venueid> --venue-name "<Conf Year>"
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexing.ids import openreview_paper_id

DEFAULT_SITE = "https://eccv.ecva.net"
DEFAULT_JSON = f"{DEFAULT_SITE}/static/virtual/data/eccv-2026-orals-posters.json"
DEFAULT_ABSTRACTS = f"{DEFAULT_SITE}/static/virtual/data/eccv-2026-abstracts.json"
DEFAULT_VENUEID = "thecvf.com/ECCV/2026/Conference"
DEFAULT_VENUE_NAME = "ECCV 2026"

HEADERS = {"User-Agent": "Mozilla/5.0 (research corpus downloader)"}


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name[:max_len].rstrip(" .") or "untitled"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def list_papers(json_url: str, abstracts_url: str, site: str,
                venueid: str, venue_name: str) -> list[dict]:
    events = json.loads(get(json_url))["results"]
    try:
        abstracts = json.loads(get(abstracts_url))
    except Exception:
        abstracts = {}

    # Posters are the papers; Oral/Spotlight entries are extra sessions of
    # the same paper, linked via related_events_ids.
    special = {e["id"]: e["eventtype"] for e in events
               if e["eventtype"] != "Poster"}
    papers = []
    for e in events:
        if e["eventtype"] != "Poster":
            continue
        kinds = {special.get(rid) for rid in e.get("related_events_ids", [])}
        category = ("oral" if "Oral" in kinds
                    else "spotlight" if "Spotlight" in kinds else "poster")
        papers.append({
            "id": e["uid"],
            "paper_id": openreview_paper_id(e["name"], venueid),
            "title": e["name"],
            "authors": [a["fullname"] for a in e["authors"]],
            "venue": f"{venue_name} {category}",
            "venueid": venueid,
            "abstract": (e.get("abstract")
                         or abstracts.get(str(e["id"])) or ""),
            "forum_url": site + e["virtualsite_url"],
            "pdf_url": e["paper_pdf_url"],
        })
    return papers


def merge_metadata(meta_path: Path, papers: list[dict]) -> None:
    existing = json.loads(meta_path.read_text()) if meta_path.exists() else []
    ours = {p["paper_id"] for p in papers}
    merged = [e for e in existing
              if (e.get("paper_id") or "") not in ours] + papers
    meta_path.write_text(json.dumps(merged, indent=2))
    print(f"metadata.json: {len(existing)} existing -> {len(merged)} entries "
          f"({len(papers)} from this venue).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json-url", default=DEFAULT_JSON)
    parser.add_argument("--abstracts-url", default=DEFAULT_ABSTRACTS)
    parser.add_argument("--site", default=DEFAULT_SITE)
    parser.add_argument("--venueid", default=DEFAULT_VENUEID,
                        help="Canonical venue id used for paper_id hashing "
                             "and the output folder name")
    parser.add_argument("--venue-name", default=DEFAULT_VENUE_NAME)
    parser.add_argument("--out",
                        default=str(Path(__file__).resolve().parent.parent / "papers"),
                        help="Output directory (default: papers/ at repo root)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after this many papers (for testing)")
    parser.add_argument("--concurrency", type=int, default=8,
                        help="Parallel downloads (default: 8)")
    args = parser.parse_args()

    print(f"Listing papers from {args.json_url} ...")
    papers = list_papers(args.json_url, args.abstracts_url, args.site,
                         args.venueid, args.venue_name)
    print(f"{len(papers)} accepted papers found.")
    if args.limit:
        papers = papers[:args.limit]
        print(f"Limiting to first {len(papers)} papers.")

    out_dir = Path(args.out)
    venue_dir = out_dir / sanitize_filename(args.venueid)
    venue_dir.mkdir(parents=True, exist_ok=True)
    merge_metadata(out_dir / "metadata.json", papers)

    pending = []
    for paper in papers:
        fname = venue_dir / f"{sanitize_filename(paper['title'])}.pdf"
        if not fname.exists():
            pending.append((paper, fname))
    skipped = len(papers) - len(pending)
    if skipped:
        print(f"{skipped} papers already downloaded, {len(pending)} to go.")

    def fetch_one(paper: dict, fname: Path) -> None:
        for attempt in range(3):
            try:
                data = get(paper["pdf_url"])
                if data[:5] != b"%PDF-":
                    raise RuntimeError("response is not a PDF")
                fname.write_bytes(data)
                return
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

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

    print(f"\nDone. {len(papers) - len(failed)} downloaded, {len(failed)} "
          f"failed, saved in {venue_dir.resolve()}")
    if failed:
        print("Failed papers:")
        for paper in failed:
            print(f"  - {paper['title']} ({paper['pdf_url']})")
        sys.exit(1)


if __name__ == "__main__":
    main()
