# Implementation Status

Living document: what exists, what's in progress, what's next. The *why*
behind decisions lives in `offline_ingestion_design.md`; this page is the
*what*. Update this file whenever a component is added or materially changed.

Last updated: 2026-09-20

## Implemented

### Corpus acquisition (`scrapper/`)
| Component | What it does | Run |
|---|---|---|
| `browser_download_papers.py` | OpenReview venues behind Cloudflare, via your Chrome (ICML, MLSys, COLM, ...) | `uv run scrapper/browser_download_papers.py --venueid ICML.cc/2026/Conference` |
| `download_papers.py` | OpenReview venues via the plain API | same, for venues without bot protection |
| `virtual_download_papers.py` | ECVA-style virtual conference sites (ECCV 2026) | `uv run scrapper/virtual_download_papers.py` |
| `acm_download_papers.py` | dblp TOC listing + ACM DL open-access PDFs (SIGIR 2026) — browser-navigation downloads to beat Cloudflare's fetch blocking | `uv run scrapper/acm_download_papers.py` |
| `probe_venueid.py` | Diagnose a venue's real OpenReview venueids (or that it publishes none) | `uv run scrapper/probe_venueid.py ACM.org/SIGIR/2026/Conference` |

All scrapers stamp a hash-based `paper_id` into `papers/metadata.json`
(merged across venues) and name PDFs `papers/<venueid>/<title>.pdf`.
Corpus so far: ICML, ECCV, SIGIR downloaded (~10.8k metadata entries);
MLSys/COLM listed. `tools/upload_s3.py` bulk-uploads PDFs to S3.

### Extraction (`extraction/`) — per-paper, validated on 1 test paper
| Component | What it does |
|---|---|
| `pdf_to_markdown.py` | PDF → per-page markdown (LLM), page classification stops at references, per-page resume |
| `research_extract.py` | pages → `paper.json` (`PaperAnalysis` schema) with client-side schema repair |
| `extract_summary.py` | pages → detailed `summary.md` |

### Ingestion machinery (`ingestion/`) — written, not yet run at scale
| Component | What it does | Run |
|---|---|---|
| `worker.py` | ONE paper end-to-end: PDF → pages → S3 → paper.json → summary.md → S3. Unit of work for backfill now and the upload service later | `uv run python -m ingestion.worker <paper_id>` |
| `backfill.py` | Batch driver: all of metadata.json, N workers, SQLite status manifest (`done`/`failed`/`no_pdf`), resume + `--retry-failed` | `uv run python -m ingestion.backfill --workers 2` |
| `s3store.py` | S3 artifact store helpers (`S3_ARTIFACTS_URL`), skip-if-exists uploads | library |

### Indexing (`indexing/`) — local path validated on 1 paper; OpenSearch path written, untested against a real domain
| Component | What it does | Run |
|---|---|---|
| `ids.py` | `paper_id` hashing + filename helpers | library |
| `flatten.py` | paper.json → typed records (`records.jsonl` + `papers.jsonl`); streams from S3 with `--s3` or local folders; sentence-aligned chunking at `CHUNK_CHARS` | `uv run python -m indexing.flatten --s3` |
| `embeddings.py` | Embedding providers: openai / gemini / local (fastembed, no key) | library |
| `build_index.py` | records → embeddings (record_id-keyed cache) + local FAISS/BM25/SQLite | `uv run python -m indexing.build_index` |
| `search.py` | Local hybrid search CLI (RRF) | `uv run python -m indexing.search "query"` |
| `opensearch_index.py` | AWS OpenSearch backend: index creation (embedding model pinned in mapping), bulk load from local artifacts, per-paper upsert, hybrid BM25+kNN search with client-side RRF | `uv run python -m indexing.opensearch_index --create --load-local` |

## Architecture decisions in force
(rationale in `offline_ingestion_design.md`)
- Extraction and indexing are **separate stages**; S3 is the contract between
  them (indexing reads `paper.json` from S3, never local disk, in production).
- Artifacts upload to S3 **as soon as produced** (pages before the
  single-call extraction stages) — local disk is a working cache.
- Only flattened `paper.json` records are embedded/indexed; markdown pages
  are the evidence store; `summary.md` is not indexed (revisit if needed).
- OpenSearch replaces FAISS+BM25+SQLite in deployment; the local triple stays
  for dev. Managed domain, not Serverless (client `_id` needed).
- No SQS for the backfill; queue only arrives with the upload service.

## Next up (in order)
1. **Run the extraction backfill** on the corpus (EC2 or laptop, tmux):
   small `--limit` first, inspect artifacts, then let it run. This is the
   long LLM crunch — everything else is hours, this is days/weeks.
2. Inspect early `paper.json` output → settle **embedding provider** and any
   flatten/chunking tweaks (decision currently open: local bge vs API).
3. Provision the **OpenSearch domain**; `--create` the indices; bulk load
   via `flatten --s3` → `build_index` (embeddings) → `--load-local`.
4. Sanity-check retrieval quality with `opensearch_index --search`.

## Pinned / deferred
- **User-upload service** (API + SQS + same worker): pinned until backfill
  works. Design note: user PDFs have no scraper metadata, so the title must
  be extracted (first page, cheap LLM call) before `paper_id` can be minted.
- Indexing `summary.md` sections as extra records — only if retrieval
  quality shows narrative-context gaps.
- Online stage (research-direction generation) — separate design docs.
