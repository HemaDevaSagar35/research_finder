"""Download a conference's papers from the ACM Digital Library via dblp.

For venues that review on OpenReview but never publish accepted papers there
(e.g. SIGIR 2026), dblp's table-of-contents API lists every paper with its
DOI, and the PDF is open access on dl.acm.org (ACM is fully open access since
Jan 2026). Both sites are bot-protected, so this drives your installed Chrome
via Playwright like browser_download_papers.py.

Cloudflare on dl.acm.org blocks JS fetch()/API requests outright (TLS and
request fingerprinting) but allows real browser navigations, so PDFs are
downloaded by navigating to them with Chrome's built-in PDF viewer disabled,
which turns each navigation into a captured download. This is sequential by
nature — which is also the polite pace: ACM's terms prohibit aggressive
systematic downloading and they do block IPs for it. Don't lower --delay.

PDFs land in papers/<venueid>/ and entries are MERGED into
papers/metadata.json by paper_id (other venues preserved).

Usage:
    uv run scrapper/acm_download_papers.py                  # SIGIR 2026
    uv run scrapper/acm_download_papers.py --limit 3        # test
    uv run scrapper/acm_download_papers.py \
        --toc db/conf/sigir/sigir2026.bht \
        --venueid ACM.org/SIGIR/2026/Conference --venue-name "SIGIR 2026"
"""

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexing.ids import openreview_paper_id
from scrapper.browser_download_papers import in_page_fetch, sanitize_filename
from scrapper.virtual_download_papers import merge_metadata

DBLP_API = "https://dblp.org/search/publ/api"
ACM_BASE = "https://dl.acm.org"

DEFAULT_TOC = "db/conf/sigir/sigir2026.bht"
DEFAULT_VENUEID = "ACM.org/SIGIR/2026/Conference"
DEFAULT_VENUE_NAME = "SIGIR 2026"


def wait_for_site(page, url: str, probe_js: str, timeout_s: int = 180) -> None:
    """Load a bot-protected site and wait until in-page requests succeed."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    except Exception as e:
        # A slow/challenged load isn't fatal; the probe loop below decides.
        print(f"(page load slow: {e.__class__.__name__}; probing anyway)")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if page.evaluate(probe_js):
                return
        except Exception:
            pass  # mid-navigation (challenge redirect); retry
        print("Waiting for the bot check to pass (click any 'Verify you are "
              "human' checkbox you see)...")
        time.sleep(3)
    raise RuntimeError(f"Bot check did not pass in time for {url}")


def list_papers(page, toc: str, venueid: str, venue_name: str) -> list[dict]:
    """List the venue's papers from dblp's TOC search API."""
    papers, offset = [], 0
    while True:
        url = (f"{DBLP_API}?q=toc%3A{toc.replace('/', '%2F')}%3A"
               f"&h=1000&f={offset}&format=json")
        for attempt in range(8):
            try:
                resp = in_page_fetch(page, url)
            except Exception as e:  # context lost (challenge redirect etc.)
                resp = {"status": 0, "text": str(e)}
            if resp["status"] == 200:
                break
            # dblp rate-limits the API hard; a 429 can also invalidate the
            # page's bot clearance, so wait it out and reload the page.
            wait = 30 * (attempt + 1)
            print(f"dblp not serving (HTTP {resp['status']}); waiting "
                  f"{wait}s and reloading...")
            time.sleep(wait)
            try:
                page.goto("https://dblp.org/db/conf/sigir/index.html",
                          wait_until="domcontentloaded", timeout=90_000)
                time.sleep(5)  # let the Anubis proof-of-work finish
            except Exception:
                pass
        else:
            raise RuntimeError("dblp kept rate-limiting; try again later.")
        hits = json.loads(resp["text"])["result"]["hits"]
        batch = hits.get("hit", [])
        for hit in batch:
            info = hit["info"]
            if info.get("type") == "Editorship":  # proceedings frontmatter
                continue
            doi = info.get("doi")
            if not doi:
                print(f"  no DOI, skipping: {info.get('title', '?')}")
                continue
            authors = info.get("authors", {}).get("author", [])
            if isinstance(authors, dict):
                authors = [authors]
            papers.append({
                "id": info.get("key"),
                "paper_id": openreview_paper_id(info["title"], venueid),
                "title": info["title"].rstrip("."),
                "authors": [a["text"] for a in authors],
                "venue": venue_name,
                "venueid": venueid,
                "abstract": "",
                "forum_url": f"https://doi.org/{doi}",
                "pdf_url": f"{ACM_BASE}/doi/pdf/{doi}",
            })
        offset += len(batch)
        print(f"[dblp {toc}] listed {offset} of {hits['@total']} entries...")
        if offset >= int(hits["@total"]) or not batch:
            break
        time.sleep(10)  # stay under dblp's API rate limit between pages
    return papers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--toc", default=DEFAULT_TOC,
                        help=f"dblp TOC key (default: {DEFAULT_TOC})")
    parser.add_argument("--venueid", default=DEFAULT_VENUEID,
                        help="Canonical venue id for paper_id hashing and "
                             "the output folder name")
    parser.add_argument("--venue-name", default=DEFAULT_VENUE_NAME)
    parser.add_argument("--out",
                        default=str(Path(__file__).resolve().parent.parent / "papers"),
                        help="Output directory (default: papers/ at repo root)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after this many papers (for testing)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between downloads (default: 2.0 — be "
                             "gentle, ACM blocks bulk downloaders)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    venue_dir = out_dir / sanitize_filename(args.venueid)
    venue_dir.mkdir(parents=True, exist_ok=True)

    # Fresh Chrome profile with the built-in PDF viewer disabled, so that
    # navigating to a PDF fires a download event instead of rendering it.
    # Recreated every run: Chrome rewrites Preferences on exit and can drop
    # the setting, which silently breaks the download events.
    profile_dir = Path(tempfile.gettempdir()) / "acm_download_profile"
    shutil.rmtree(profile_dir, ignore_errors=True)
    prefs = profile_dir / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps(
        {"plugins": {"always_open_pdf_externally": True}}))

    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir), headless=False, channel="chrome",
                args=launch_args, accept_downloads=True)
        except Exception:
            context = p.chromium.launch_persistent_context(
                str(profile_dir), headless=False, args=launch_args,
                accept_downloads=True)
        page = context.pages[0] if context.pages else context.new_page()

        # Cache the dblp listing so reruns don't re-hit its rate-limited API.
        list_cache = venue_dir / "_dblp_list.json"
        if list_cache.exists():
            papers = json.loads(list_cache.read_text())
            print(f"Loaded {len(papers)} papers from {list_cache} "
                  f"(delete it to re-list from dblp).")
        else:
            # dblp's Anubis check serves its challenge page with HTTP 200,
            # so "status 200" is no pass signal — require parseable JSON.
            print("Opening dblp (bot check runs automatically, "
                  "takes a few s)...")
            wait_for_site(page, "https://dblp.org/db/conf/sigir/index.html",
                          f"fetch('{DBLP_API}?q=test&h=1&format=json')"
                          ".then(r => r.json()).then(() => true)"
                          ".catch(() => false)")
            papers = list_papers(page, args.toc, args.venueid,
                                 args.venue_name)
            list_cache.write_text(json.dumps(papers, indent=1))
        print(f"{len(papers)} papers with DOIs.")
        if args.limit:
            papers = papers[:args.limit]
            print(f"Limiting to first {len(papers)} papers.")

        merge_metadata(out_dir / "metadata.json", papers)

        pending = {}
        for paper in papers:
            fname = venue_dir / f"{sanitize_filename(paper['title'])}.pdf"
            if not fname.exists():
                pending[paper["pdf_url"]] = (paper, fname)
        skipped = len(papers) - len(pending)
        if skipped:
            print(f"{skipped} papers already downloaded, {len(pending)} to go.")

        failed = []
        if pending:
            print("Opening the ACM Digital Library (click the checkbox if "
                  "a Cloudflare challenge appears)...")
            wait_for_site(
                page, ACM_BASE,
                f"fetch('{ACM_BASE}/', {{method: 'HEAD'}})"
                ".then(r => r.status < 400)")

            done = skipped
            for url, (paper, fname) in pending.items():
                done += 1
                for attempt in range(1, 5):
                    try:
                        with page.expect_download(timeout=45_000) as dl:
                            try:
                                # Navigation aborts when the download starts;
                                # that error is expected.
                                page.goto(url)
                            except Exception:
                                pass
                        dl.value.save_as(fname)
                        with open(fname, "rb") as f:
                            if f.read(5) != b"%PDF-":
                                raise RuntimeError("response is not a PDF")
                        print(f"[{done}/{len(papers)}] Downloaded: "
                              f"{fname.name}")
                        break
                    except Exception as e:
                        fname.unlink(missing_ok=True)
                        if attempt < 4:
                            # Usually a Cloudflare interstitial: wait for a
                            # human to click it, then retry the same paper.
                            print(f"[{done}/{len(papers)}] no download yet "
                                  f"(attempt {attempt}) — if the browser "
                                  f"shows a challenge, click it; retrying "
                                  f"in 30s: {paper['title']}")
                            time.sleep(30)
                        else:
                            failed.append(paper)
                            print(f"[{done}/{len(papers)}] FAILED ({e}): "
                                  f"{paper['title']}")
                time.sleep(args.delay)
        context.close()

    print(f"\nDone. {len(papers) - len(failed)} downloaded, {len(failed)} "
          f"failed, saved in {venue_dir.resolve()}")
    if failed:
        print("Failed papers (rerun to retry):")
        for paper in failed:
            print(f"  - {paper['title']} ({paper['forum_url']})")
        sys.exit(1)


if __name__ == "__main__":
    main()
