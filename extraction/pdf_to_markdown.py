"""Convert a PDF (e.g. a downloaded paper) into per-page Markdown files.

Creates markdown/<pdf_name>/ (spaces in the name become underscores) and
writes one file per page: 01.md, 02.md, ... Pages are processed concurrently
and already-existing page files are skipped, so reruns resume.

By default pages are first classified (see classify_pages.py) and extraction
stops at the first 'references' page (inclusive, so a conclusion sharing that
page is kept); later reference/appendix pages are skipped. Use --all-pages to
extract everything.

Two extraction modes (picked automatically from the provider):
- image: renders each page to an image for a vision model (openai, gemini)
- text:  extracts each page's text layer and has the LLM structure it as
         Markdown (works with text-only models like deepseek)

Usage:
    uv run python -m extraction.pdf_to_markdown "papers/.../paper.pdf"
    uv run python -m extraction.pdf_to_markdown paper.pdf --model deepseek-v4-flash
    uv run python -m extraction.pdf_to_markdown paper.pdf --mode image --provider gemini
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import pymupdf

from llm_client import AsyncLLMClient

from .extract import PROMPT as IMAGE_PROMPT
from .extract import _clean, _image_messages, page_count, render_page

TEXT_PROMPT = """Below is the raw text extracted from one page of an academic
paper. Reconstruct it as clean Markdown.

Rules:
- Restore the structure: headings, paragraphs, lists.
- Write math in LaTeX: inline as $...$, display equations as $$...$$.
- Reconstruct tables as Markdown tables where possible.
- For figures, insert a short placeholder like: *[Figure N: caption text]*
- Skip page headers, footers, page numbers, and other artifacts.
- Output ONLY the Markdown content, no commentary and no code fences.

Raw page text:
"""

IMAGE_PROVIDERS = {"openai", "gemini"}


def page_text(pdf_path: str | Path, page_number: int) -> str:
    with pymupdf.open(pdf_path) as doc:
        return doc[page_number - 1].get_text("text")


def convert_pdf(pdf_path: Path, out_root: Path, *,
                mode: str = "auto",
                provider: str | None = None,
                model: str | None = None,
                dpi: int = 150,
                concurrency: int | None = None,
                classify: bool = True) -> Path:
    provider = provider or os.environ.get("EXTRACT_PROVIDER")
    model = model or os.environ.get("EXTRACT_MODEL")
    client = AsyncLLMClient(provider, concurrency=concurrency)
    if mode == "auto":
        mode = "image" if client.provider in IMAGE_PROVIDERS else "text"
    print(f"Provider: {client.provider} | model: "
          f"{model or client.default_model} | mode: {mode}")

    out_dir = out_root / pdf_path.stem.replace(" ", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    total = page_count(pdf_path)

    # The paper ends where the references begin: classify pages and extract
    # only up to (and including) the first 'references' page, so a conclusion
    # sharing that page is not missed. Appendix pages after it are skipped.
    last_page = total
    if classify:
        from .classify_pages import classify_pdf
        print("Classifying pages to find where the references start...")
        labels = classify_pdf(pdf_path, concurrency=concurrency)
        ref_pages = [n for n, label in labels.items() if label == "references"]
        if ref_pages:
            last_page = ref_pages[0]
            print(f"References start on page {last_page}; extracting pages "
                  f"1-{last_page} of {total}.")
        else:
            print("No reference pages detected; extracting all pages.")

    width = max(2, len(str(total)))
    pages = []
    for n in range(1, last_page + 1):
        out_file = out_dir / f"{n:0{width}d}.md"
        if out_file.exists():
            print(f"[{n}/{last_page}] already exists, skipping: {out_file.name}")
        else:
            pages.append((n, out_file))

    # DeepSeek models reason by default and can burn the whole token budget
    # "thinking" and return empty content; this task needs no reasoning.
    extra = {"reasoning_effort": "none"} if client.provider == "deepseek" else {}

    async def run() -> None:
        async def one(n: int, out_file: Path) -> None:
            if mode == "image":
                messages = _image_messages(render_page(pdf_path, n, dpi=dpi))
            else:
                messages = [{"role": "user",
                             "content": TEXT_PROMPT + page_text(pdf_path, n)}]
            try:
                markdown = _clean(await client.chat(messages=messages,
                                                    model=model, **extra) or "")
                if not markdown:
                    raise RuntimeError("model returned empty content")
                out_file.write_text(markdown)
                print(f"[{n}/{last_page}] wrote {out_file.name}")
            except Exception as e:
                print(f"[{n}/{last_page}] FAILED: {e}")
                raise

        results = await asyncio.gather(*(one(n, f) for n, f in pages),
                                       return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            sys.exit(f"{len(errors)} of {len(pages)} pages failed; "
                     f"rerun to retry the missing ones.")

    if pages:
        asyncio.run(run())
    print(f"Done: {out_dir}")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--out-root",
                        default=str(Path(__file__).resolve().parent.parent / "markdown"),
                        help="Root output directory (default: markdown/ at repo root)")
    parser.add_argument("--mode", choices=["auto", "image", "text"], default="auto",
                        help="Extraction mode (default: auto — image for "
                             "openai/gemini, text otherwise)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider (default: PROVIDER from .env)")
    parser.add_argument("--model", default=None,
                        help="Model override (default: provider's model from .env)")
    parser.add_argument("--dpi", type=int, default=150,
                        help="Render resolution for image mode (default: 150)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Concurrent LLM calls (default: LLM_CONCURRENCY or 8)")
    parser.add_argument("--all-pages", action="store_true",
                        help="Skip classification and extract every page, "
                             "including references and appendix")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    convert_pdf(pdf_path, Path(args.out_root), mode=args.mode,
                provider=args.provider, model=args.model,
                dpi=args.dpi, concurrency=args.concurrency,
                classify=not args.all_pages)


if __name__ == "__main__":
    main()
