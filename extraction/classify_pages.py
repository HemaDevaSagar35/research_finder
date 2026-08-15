"""Classify each page of a PDF as 'paper' or 'references' using an LLM.

For vision providers (openai, gemini) the page image is sent; for text-only
providers (deepseek) the page's extracted text is sent.

Env configuration (falls back to the main PROVIDER block when unset):
    CLASSIFY_PROVIDER   provider for classification (e.g. deepseek)
    CLASSIFY_MODEL      model for classification (e.g. deepseek-v4-flash)

Usage:
    uv run python -m extraction.classify_pages "papers/.../paper.pdf"
    uv run python -m extraction.classify_pages paper.pdf --out labels.json
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from llm_client import AsyncLLMClient

from .extract import _image_messages, page_count, render_page
from .pdf_to_markdown import IMAGE_PROVIDERS, page_text

PROMPT = """Classify this page of an academic paper.

Reply with exactly one word:
- "references" if the page consists mostly of the bibliography / reference
  list (entries of cited works).
- "paper" for everything else (title, abstract, body, figures, appendix, ...).
"""

LABELS = {"paper", "references"}


def _parse_label(raw: str) -> str:
    label = raw.strip().strip('."\'').lower()
    return label if label in LABELS else "paper"


def classify_pdf(pdf_path: Path, *,
                 provider: str | None = None,
                 model: str | None = None,
                 dpi: int = 100,
                 concurrency: int | None = None) -> dict[int, str]:
    """Return {page_number: 'paper' | 'references'} for every page."""
    provider = provider or os.environ.get("CLASSIFY_PROVIDER")
    model = model or os.environ.get("CLASSIFY_MODEL")
    client = AsyncLLMClient(provider, concurrency=concurrency)
    mode = "image" if client.provider in IMAGE_PROVIDERS else "text"
    print(f"Provider: {client.provider} | model: "
          f"{model or client.default_model} | mode: {mode}")

    total = page_count(pdf_path)

    # Note: reasoning stays enabled here (unlike extraction) — the one-word
    # output is cheap, and without reasoning the labels get unreliable.
    async def run() -> dict[int, str]:
        async def one(n: int) -> tuple[int, str]:
            if mode == "image":
                messages = _image_messages(render_page(pdf_path, n, dpi=dpi))
                messages[0]["content"][0]["text"] = PROMPT
            else:
                messages = [{"role": "user",
                             "content": PROMPT + "\nPage text:\n" + page_text(pdf_path, n)}]
            raw = await client.chat(messages=messages, model=model)
            return n, _parse_label(raw or "")

        results = await asyncio.gather(*(one(n) for n in range(1, total + 1)))
        return dict(sorted(results))

    return asyncio.run(run())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--out", default=None,
                        help="Write labels to this JSON file (default: print only)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider (default: CLASSIFY_PROVIDER or PROVIDER)")
    parser.add_argument("--model", default=None,
                        help="Model (default: CLASSIFY_MODEL or provider's model)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Concurrent LLM calls (default: LLM_CONCURRENCY or 8)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    labels = classify_pdf(pdf_path, provider=args.provider, model=args.model,
                          concurrency=args.concurrency)

    for n, label in labels.items():
        print(f"page {n:>3}: {label}")
    ref_pages = [n for n, label in labels.items() if label == "references"]
    print(f"\nReference pages: {ref_pages or 'none'}")

    if args.out:
        Path(args.out).write_text(json.dumps(labels, indent=2))
        print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
