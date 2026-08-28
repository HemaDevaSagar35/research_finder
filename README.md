# research_finder

Tools for finding and downloading research papers.

- `scrapper/` — downloads accepted papers (e.g. ICML 2026 spotlights) from
  OpenReview. See `scrapper/README.md`.
- `extraction/` — converts PDF pages (e.g. downloaded papers) to Markdown
  using a vision LLM via `llm_client`, then turns a paper's Markdown pages
  into a single machine-readable JSON record (for RAG):

  ```bash
  uv run python -m extraction.pdf_to_markdown paper.pdf --out-dir markdown
  uv run python -m extraction.research_extract "markdown/<paper_folder>"
  ```
- `indexing/` — flattens extracted `paper.json` files into typed retrieval
  records and builds a local hybrid index (FAISS vectors + BM25 + SQLite
  metadata). See `docs/offline_ingestion_design.md`:

  ```bash
  uv run python -m indexing.flatten --roots markdown
  uv run python -m indexing.build_index
  uv run python -m indexing.search "efficient MoE inference"
  ```
- `llm_client/` — unified client for OpenAI, Gemini, and DeepSeek chat APIs.
  Set `OPENAI_API_KEY` / `GEMINI_API_KEY` / `DEEPSEEK_API_KEY`, then:

  ```python
  from llm_client import chat
  print(chat("Say hi in one word.", model="gemini-2.5-flash"))
  ```

## Setup

Requires [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```
