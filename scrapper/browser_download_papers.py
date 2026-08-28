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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexing.ids import openreview_paper_id

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
        title = content.get("title", {}).get("value", "untitled")
        papers.append({
            "id": note["id"],
            "paper_id": openreview_paper_id(title, venueid),
            "title": title,
            "authors": content.get("authors", {}).get("value", []),
            "venue": content.get("venue", {}).get("value", ""),
            "venueid": venueid,
            "abstract": content.get("abstract", {}).get("value", ""),
            "forum_url": f"{SITE_BASE}/forum?id={note['id']}",
            "pdf_url": f"{SITE_BASE}/pdf?id={note['id']}",
        })
    return papers


# In-page worker pool: N workers pull urls from a shared queue, so a new
# download starts the moment any lane frees up (no batch barrier). Each
# finished PDF is streamed back to Python immediately via window.savePdf.
# HTTP 429 (rate limit) triggers a shared cooldown honoring Retry-After,
# with exponential backoff and unlimited retries for that url.
WORKER_POOL_JS = """async ({urls, workers, delayMs}) => {
    let next = 0;
    let pauseUntil = 0;
    const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
    const worker = async () => {
        while (next < urls.length) {
            const url = urls[next++];
            let attempt = 0;
            while (true) {
                const wait = pauseUntil - Date.now();
                if (wait > 0) await sleep(wait);
                if (delayMs) await sleep(delayMs);
                try {
                    const r = await fetch(url, {credentials: 'include'});
                    if (r.status === 429) {
                        attempt++;
                        const retryAfter = parseFloat(r.headers.get('retry-after'));
                        const backoff = retryAfter ? retryAfter * 1000
                            : Math.min(120000, 2000 * 2 ** Math.min(attempt, 6));
                        // pause ALL workers, not just this one
                        pauseUntil = Math.max(pauseUntil, Date.now() + backoff);
                        await window.reportRateLimit(backoff, attempt);
                        continue;
                    }
                    if (!r.ok) {
                        await window.savePdf(url, r.status, null, null);
                        break;
                    }
                    const bytes = new Uint8Array(await r.arrayBuffer());
                    let binary = '';
                    for (let j = 0; j < bytes.length; j += 0x8000) {
                        binary += String.fromCharCode(...bytes.subarray(j, j + 0x8000));
                    }
                    await window.savePdf(url, r.status, btoa(binary), null);
                    break;
                } catch (e) {
                    await window.savePdf(url, 0, null, String(e));
                    break;
                }
            }
        }
    };
    await Promise.all(Array.from({length: workers}, worker));
}"""


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
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Number of PDFs to download in parallel (default: 4)")
    parser.add_argument("--delay", type=float, default=0.25,
                        help="Seconds each worker waits before starting a "
                             "download, to stay under rate limits (default: 0.25)")
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

        # Skip papers already on disk, then hand the rest to the in-page
        # worker pool, which keeps --concurrency downloads in flight at once.
        pending = {}
        for paper in papers:
            venue_dir = out_dir / sanitize_filename(paper["venueid"])
            venue_dir.mkdir(parents=True, exist_ok=True)
            fname = venue_dir / f"{sanitize_filename(paper['title'])}.pdf"
            if not fname.exists():
                pending[paper["pdf_url"]] = (paper, fname)
        skipped = len(papers) - len(pending)
        if skipped:
            print(f"{skipped} papers already downloaded, {len(pending)} to go.")

        failed = []
        progress = {"done": skipped}

        def save_pdf(url, status, b64, error):
            paper, fname = pending[url]
            progress["done"] += 1
            try:
                if status != 200:
                    raise RuntimeError(error or f"HTTP {status}")
                data = base64.b64decode(b64)
                if data[:5] != b"%PDF-":
                    raise RuntimeError("response is not a PDF")
                fname.write_bytes(data)
                print(f"[{progress['done']}/{len(papers)}] Downloaded: {fname.name}")
            except Exception as e:
                failed.append(paper)
                print(f"[{progress['done']}/{len(papers)}] FAILED ({e}): "
                      f"{paper['title']}")

        def report_rate_limit(backoff_ms, attempt):
            print(f"Rate limited (HTTP 429); pausing all downloads for "
                  f"{backoff_ms / 1000:.0f}s (attempt {attempt})...")

        if pending:
            page.expose_function("savePdf", save_pdf)
            page.expose_function("reportRateLimit", report_rate_limit)
            page.evaluate(WORKER_POOL_JS, {
                "urls": list(pending.keys()),
                "workers": max(1, args.concurrency),
                "delayMs": int(args.delay * 1000),
            })

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
