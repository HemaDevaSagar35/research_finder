# Offline Ingestion — Design Decisions & Status

Companion to `research_path_generator_architecture_refined.md`. That doc defines
the full system; this one records the concrete decisions made for the
**offline / ingestion** stage and where the implementation currently stands.

## Pipeline

```text
2026 PAPERS (scrapper/)
      │
      ▼
PDF → per-page Markdown          extraction/pdf_to_markdown.py
(references/appendix skipped      + extraction/classify_pages.py
 via page classification)
      │
      ├──────────────► summary.md    extraction/extract_summary.py
      ▼
paper.json                        extraction/research_extract.py
(PaperAnalysis schema — superset of the design's PaperCard)
      │
      ▼
Flatten into typed records
      │
      ▼
Vector (FAISS) + BM25 + metadata (SQLite) index
```

## Decisions

### 1. PaperCard is derived on the fly, not stored

`paper.json` (the `PaperAnalysis` schema in `extraction/research_extract.py`)
is a superset of the design's PaperCard. The compact PaperCard view used by the
landscape stages is a pure field projection of `paper.json`:

| PaperCard field | paper.json source |
|---|---|
| problem | `research_problem` |
| method | `method.high_level_idea` + `method.components` |
| evaluation | `datasets`, `baselines`, `metrics`, `experimental_setup` |
| findings | `key_results`, `interesting_findings` |
| boundaries | `limitations`, `future_work`, assumptions |
| claims | `claims_and_evidence` |

No LLM call, no separate storage. When the extraction schema improves,
PaperCards update for free.

### 2. paper.json is the indexed unit — flattened into typed records

Do **not** embed a whole `paper.json` as one blob (one vector averaging many
topics retrieves poorly). Instead, flatten each paper into small,
self-contained records, one per statement:

```json
{
  "record_id": "<paper_id>#key_results/2",
  "paper_id": "<paper_id>",
  "type": "key_result",
  "text": "...",
  "source_locations": [{"page": 7, "table": "Table 2"}]
}
```

Record types come straight from the schema: `key_result`, `claim`,
`limitation`, `research_gap`, `contribution`, `interesting_finding`,
`research_opportunity`, `ablation`, `high_level_summary`, etc.

Typed records are strictly better than generic section chunks for this system:
the Opportunity Miner retrieves over limitations/findings, the novelty RAG over
claims/methods, and hits always roll up to papers via `paper_id`
(the design rule: the retrieval unit is the paper, not a disconnected chunk).

### 3. Raw markdown pages are the evidence store (not vector-indexed)

Retrieval runs entirely over the flattened paper.json records. Verification
stages (cross-paper reasoning, candidate-vs-prior-work comparison) follow each
record's `source_locations` to `markdown/<paper_id>/NN.md` on demand. The
markdown is kept on disk but does not need its own vector index in V1.

### 4. paper_id = hash of normalized metadata (source-agnostic)

OpenReview note ids only exist for OpenReview venues; CVPR/arXiv/etc. won't
have them. Instead the canonical id is a deterministic hash computable from
metadata alone:

```python
import hashlib, re

def paper_id(title: str, venue: str, year: int) -> str:
    key = re.sub(r"[^a-z0-9]", "", title.lower())
    key += f"|{re.sub(r'[^a-z0-9]', '', venue.lower())}|{year}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]
```

- Aggressive title normalization (lowercase, strip all non-alphanumerics)
  absorbs LaTeX/casing/punctuation differences between sources.
- venue + year disambiguates duplicate titles; the same work at two venues is
  intentionally two ids (different PDFs, different page numbers).
- Never hash PDF bytes: the id must be computable before download, and a
  re-downloaded revision must not change the paper's identity.
- Compute the id at **download time** and record it in `metadata.json`; later
  stages read the recorded value instead of re-deriving it.
- Source-specific ids (`openreview_id`, `arxiv_id`, ...) are kept as ordinary
  metadata fields, not as the primary key.
- Everything about a paper lives in one id-named folder:

  ```text
  markdown/<paper_id>/
    01.md, 02.md, ...   # per-page markdown (evidence store)
    paper.json          # structured extraction
    summary.md          # detailed summary
  ```

  (`pdf_to_markdown --folder-name <paper_id>` sets the folder; the batch
  driver will pass it automatically. This also fixes the special-character
  mess in title-derived folder names. The flattener still resolves old
  title-named folders as a fallback.)

### 5. All index artifacts are local files, keyed by paper_id

```text
index/
  records.jsonl      # flattened statements, one JSON per line
  vectors.faiss      # embedding index over record texts
  bm25/              # lexical index (record texts + retrieval_tags)
  metadata.sqlite    # paper_id → title, authors, venue, year, tags, source ids
  index_meta.json    # embedding model name + dimension, build info
```

Scale check: ~6k papers × ~40 records ≈ 250k vectors — comfortably in-memory,
no vector-DB server needed.

`index_meta.json` is mandatory: queries must be embedded with the exact model
that built the index, so query code reads the model name from there rather
than local config.

### 6. The bundle is portable to contributors

A contributor needs `index/` + `markdown/` (+ `paper.json` files) to run
queries locally — a couple of GB. Raw PDFs are not needed for querying.
Requirements for portability:

- relative paths only (nothing absolute baked into records or the db);
- BM25 serialization in a plain file format (e.g. `bm25s`), not pickles;
- same embedding model available (API key, or local model auto-downloaded);
- share via zip / Hugging Face datasets / rsync — not git history.

## Status (2026-08-27)

| Piece | State |
|---|---|
| Scrapper (OpenReview, browser + API) | done; ICML 2026: 6,341 in metadata, ~2,584 PDFs downloaded (in progress) |
| PDF → Markdown (+ page classification) | done; run on 1 test paper (pages 03/05/08/10 failed, need rerun) |
| paper.json extraction (`PaperAnalysis`) | done; run on 1 test paper |
| summary.md extraction | done; run on 1 test paper |
| paper_id scheme + metadata linkage | done — `indexing/ids.py`; both scrapper scripts stamp `paper_id` into metadata.json; flattener falls back to computing it for older metadata |
| Flattener (paper.json → records) | done — `indexing/flatten.py` (~70 typed records per paper) |
| Vector + BM25 + metadata index | done — `indexing/build_index.py` (FAISS + bm25s + SQLite, embedding cache for incremental rebuilds) |
| Hybrid search CLI | done — `indexing/search.py` (RRF fusion, paper rollup) |
| Batch driver (corpus-scale, resumable) | not implemented |

Embeddings: `EMBED_PROVIDER` = `openai`, `gemini`, or `local` (fastembed with
`BAAI/bge-small-en-v1.5`, no API key needed — currently the default since only
a DeepSeek key is configured and DeepSeek has no embeddings API). The provider
and model that built the index are recorded in `index_meta.json` and reused
for queries; switching providers means re-embedding (cache is per-model).

Platform note: this machine is on macOS 13, which pins `faiss-cpu<1.10` and
`onnxruntime<1.22` (newer wheels require macOS 14).

Usage:

```bash
uv run python -m indexing.flatten --roots markdown   # paper.json -> records
uv run python -m indexing.build_index                # records -> index/
uv run python -m indexing.search "efficient MoE inference"
```

Remaining next step: the batch driver that walks all downloaded PDFs through
markdown → paper.json → summary.md with resume and failure tracking, naming
output folders by paper_id.
