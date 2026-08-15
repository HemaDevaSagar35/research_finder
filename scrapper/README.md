# scrapper

Downloads accepted papers from OpenReview venues (default: all ICML 2026
accepted papers). Two scripts:

- `browser_download_papers.py` — no account needed. Drives your installed
  Chrome via Playwright to pass OpenReview's Cloudflare check, then downloads
  through the browser session.
- `download_papers.py` — uses the official `openreview-py` client. Requires an
  OpenReview account (authenticated API requests skip the Cloudflare check).

Both accept a list of conferences and acceptance categories, and organize PDFs
as `papers/<venueid>/` at the repo root (e.g.
`papers/ICML.cc_2026_Conference/`), plus a `metadata.json` with titles,
authors, abstracts, links, and each paper's acceptance category. Re-running
skips already-downloaded PDFs.

## Setup

From the repo root:

```bash
uv sync
```

For `download_papers.py` also set:

```bash
export OPENREVIEW_USERNAME="you@example.com"
export OPENREVIEW_PASSWORD="your-password"
```

## Usage

```bash
# Everything accepted at ICML 2026 (spotlight + regular)
uv run scrapper/browser_download_papers.py

# Test with just a few papers
uv run scrapper/browser_download_papers.py --limit 3

# Only certain categories
uv run scrapper/browser_download_papers.py --venues spotlight
uv run scrapper/browser_download_papers.py --venues spotlight oral

# Multiple conferences at once
uv run scrapper/browser_download_papers.py \
    --venueids ICML.cc/2026/Conference NeurIPS.cc/2025/Conference

# Custom output directory
uv run scrapper/browser_download_papers.py --out scrapper/papers
```

`download_papers.py` takes the exact same arguments.

The venue id is the `id=` part of an OpenReview group URL, e.g.
`https://openreview.net/group?id=ICML.cc/2026/Conference` → `ICML.cc/2026/Conference`.
