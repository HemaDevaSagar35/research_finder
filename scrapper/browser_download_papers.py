"""Download accepted papers from OpenReview using a real browser (no account needed).

OpenReview blocks plain HTTP clients with a Cloudflare check, so this drives
your installed Chrome via Playwright with automation markers hidden. The
Cloudflare check passes on its own, then the OpenReview API is called from
inside the page to list papers and fetch PDFs.

Takes any number of conferences (--venueids) and acceptance categories
(--venues); downloads everything accepted by default. PDFs are organized as
papers/<venueid>/, e.g. papers/ICML.cc_2026_Conference/.

Usage:
    python browser_download_papers.py                          # everything accepted
    python browser_download_papers.py --venues spotlight       # one category
    python browser_download_papers.py --venues spotlight oral  # several categories
    python browser_download_papers.py --venueids ICML.cc/2026/Conference NeurIPS.cc/2025/Conference
    python browser_download_papers.py --limit 5                # for testing
"""

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

API_BASE = "https://api2.openreview.net"
SITE_BASE = "https://openreview.net"
PAGE_SIZE = 1000


def sanitize_filename(name: str, max_len: int = 150) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name[:max_len].rstrip(" .") or "untitled"


def in_page_fetch(page, url: str) -> dict:
    return page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: 'include'});
            return {status: r.status, text: await r.text()};
        }""", url)


def wait_for_challenge(page, venueid: str, timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            probe = in_page_fetch(
                page, f"{API_BASE}/notes?content.venueid={venueid}&limit=1")
            if probe["status"] == 200:
                return
        except Exception:
            pass  # page may be mid-navigation (challenge redirect); retry
        print("Waiting for the Cloudflare check to pass (if a 'Verify you are "
              "human' checkbox is shown, please click it)...")
        time.sleep(3)
    raise RuntimeError("Cloudflare check did not pass in time.")


def list_papers(page, venueid: str) -> list[dict]:
    notes, offset = [], 0
    while True:
        resp = in_page_fetch(
            page, f"{API_BASE}/notes?content.venueid={venueid}"
                  f"&limit={PAGE_SIZE}&offset={offset}")
        if resp["status"] != 200:
            raise RuntimeError(f"API request failed: {resp['status']} "
                               f"{resp['text'][:300]}")
        batch = json.loads(resp["text"]).get("notes", [])
        notes.extend(batch)
        print(f"[{venueid}] fetched {len(notes)} accepted papers so far...")
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    papers = []
    for note in notes:
        content = note.get("content", {})
        papers.append({
            "id": note["id"],
            "title": content.get("title", {}).get("value", "untitled"),
            "authors": content.get("authors", {}).get("value", []),
            "venue": content.get("venue", {}).get("value", ""),
            "venueid": venueid,
            "abstract": content.get("abstract", {}).get("value", ""),
            "forum_url": f"{SITE_BASE}/forum?id={note['id']}",
            "pdf_url": f"{SITE_BASE}/pdf?id={note['id']}",
        })
    return papers


def download_pdf(page, url: str) -> bytes:
    result = page.evaluate(
        """async (url) => {
            const r = await fetch(url, {credentials: 'include'});
            if (!r.ok) return {status: r.status};
            const bytes = new Uint8Array(await r.arrayBuffer());
            let binary = '';
            for (let i = 0; i < bytes.length; i += 0x8000) {
                binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
            }
            return {status: r.status, b64: btoa(binary)};
        }""", url)
    if result["status"] != 200:
        raise RuntimeError(f"HTTP {result['status']}")
    data = base64.b64decode(result["b64"])
    if data[:5] != b"%PDF-":
        raise RuntimeError("response is not a PDF")
    return data


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
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # Installed Chrome with automation-control disabled passes the
        # Cloudflare check hands-free; bundled Chromium usually gets blocked.
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            browser = p.chromium.launch(headless=False, channel="chrome",
                                        args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=False, args=launch_args)
        page = browser.new_page()

        print("Opening OpenReview to pass the Cloudflare check...")
        page.goto(f"{SITE_BASE}/group?id={args.venueids[0]}",
                  wait_until="domcontentloaded")
        wait_for_challenge(page, args.venueids[0])
        print("Check passed. Listing accepted papers...")

        papers = []
        for venueid in args.venueids:
            papers.extend(list_papers(page, venueid))

        total = len(papers)
        if args.venues:
            # accept both space- and comma-separated lists
            wanted = [v.lower()
                      for arg in args.venues for v in arg.split(",") if v]
            papers = [p_ for p_ in papers
                      if any(w in p_["venue"].lower() for w in wanted)]
        print(f"{len(papers)} of {total} papers match venues "
              f"{args.venues or 'ALL'}.")
        if args.limit:
            papers = papers[:args.limit]
            print(f"Limiting to first {len(papers)} papers.")

        (out_dir / "metadata.json").write_text(json.dumps(papers, indent=2))
        print(f"Saved metadata to {out_dir / 'metadata.json'}")

        failed = []
        for i, paper in enumerate(papers, 1):
            venue_dir = out_dir / sanitize_filename(paper["venueid"])
            venue_dir.mkdir(parents=True, exist_ok=True)
            fname = venue_dir / f"{sanitize_filename(paper['title'])}.pdf"
            if fname.exists():
                print(f"[{i}/{len(papers)}] Already downloaded: {fname.name}")
                continue
            try:
                fname.write_bytes(download_pdf(page, paper["pdf_url"]))
                print(f"[{i}/{len(papers)}] Downloaded: {fname.name}")
            except Exception as e:
                failed.append(paper)
                print(f"[{i}/{len(papers)}] FAILED ({e}): {paper['title']}")
            time.sleep(0.5)  # be polite to the server

        browser.close()

    print(f"\nDone. {len(papers) - len(failed)} downloaded, {len(failed)} failed, "
          f"saved in {out_dir.resolve()}")
    if failed:
        print("Failed papers:")
        for paper in failed:
            print(f"  - {paper['title']} ({paper['forum_url']})")
        sys.exit(1)


if __name__ == "__main__":
    main()
