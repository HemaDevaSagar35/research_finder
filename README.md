# research_finder

Tools for finding and downloading research papers.

- `scrapper/` — downloads accepted papers (e.g. ICML 2026 spotlights) from
  OpenReview. See `scrapper/README.md`.
- `extraction/` — converts PDF pages (e.g. downloaded papers) to Markdown
  using a vision LLM via `llm_client`:

  ```bash
  uv run python -m extraction.extract paper.pdf --pages 1-3 --out paper.md
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
