"""Extract PDF pages to Markdown using a vision LLM.

Renders a page of a PDF (e.g. a downloaded paper) to an image, sends it to a
vision-capable model through llm_client, and returns the page content as
Markdown. Provider/model/keys come from .env (see .env.example); OpenAI and
Gemini models support image input, DeepSeek's chat models do not.

Library usage:
    from extraction import extract_page, extract_pdf

    md = extract_page("papers/.../paper.pdf", page_number=1)
    full_md = extract_pdf("papers/.../paper.pdf")            # all pages

CLI usage:
    uv run python -m extraction.extract paper.pdf                # all pages
    uv run python -m extraction.extract paper.pdf --pages 1-3,7
    uv run python -m extraction.extract paper.pdf --out paper.md
"""

import argparse
import asyncio
import base64
import re
import sys
from pathlib import Path

import pymupdf

from llm_client import AsyncLLMClient, LLMClient

PROMPT = """Convert this page of an academic paper to clean Markdown.

Rules:
- Preserve the reading order and structure: headings, paragraphs, lists.
- Write math in LaTeX: inline as $...$, display equations as $$...$$.
- Convert tables to Markdown tables.
- For figures, insert a short placeholder like: *[Figure N: caption text]*
- Skip page headers, footers, and page numbers.
- Output ONLY the Markdown content, no commentary and no code fences."""


def render_page(pdf_path: str | Path, page_number: int, dpi: int = 150) -> bytes:
    """Render one page (1-based) of a PDF to PNG bytes."""
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page_number <= len(doc):
            raise ValueError(f"Page {page_number} out of range: "
                             f"{pdf_path} has {len(doc)} pages.")
        page = doc[page_number - 1]
        return page.get_pixmap(dpi=dpi).tobytes("png")


def page_count(pdf_path: str | Path) -> int:
    with pymupdf.open(pdf_path) as doc:
        return len(doc)


def _image_messages(png_bytes: bytes) -> list[dict]:
    b64 = base64.b64encode(png_bytes).decode()
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }]


def _clean(markdown: str) -> str:
    """Strip a wrapping ```markdown fence if the model added one anyway."""
    text = markdown.strip()
    match = re.fullmatch(r"```(?:markdown|md)?\n(.*)\n```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def extract_page(pdf_path: str | Path, page_number: int, *,
                 dpi: int = 150,
                 provider: str | None = None,
                 model: str | None = None,
                 **kwargs) -> str:
    """Extract one page (1-based) of a PDF as Markdown."""
    png = render_page(pdf_path, page_number, dpi=dpi)
    client = LLMClient(provider)
    return _clean(client.chat(messages=_image_messages(png),
                              model=model, **kwargs))


def extract_pdf(pdf_path: str | Path, *,
                pages: list[int] | None = None,
                dpi: int = 150,
                provider: str | None = None,
                model: str | None = None,
                concurrency: int | None = None,
                **kwargs) -> str:
    """Extract several pages (default: all) concurrently and return them
    joined as one Markdown document, in page order."""
    pages = pages or list(range(1, page_count(pdf_path) + 1))

    async def run() -> list[str]:
        client = AsyncLLMClient(provider, concurrency=concurrency)

        async def one(page_number: int) -> str:
            png = render_page(pdf_path, page_number, dpi=dpi)
            return _clean(await client.chat(messages=_image_messages(png),
                                            model=model, **kwargs))

        return await asyncio.gather(*(one(n) for n in pages))

    return "\n\n".join(asyncio.run(run()))


def _parse_pages(spec: str) -> list[int]:
    """Parse a page spec like '1-3,7' into [1, 2, 3, 7]."""
    pages = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            pages.extend(range(int(lo), int(hi) + 1))
        else:
            pages.append(int(part))
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--pages", default=None,
                        help="Pages to extract, e.g. '2' or '1-3,7' "
                             "(default: all pages)")
    parser.add_argument("--out", default=None,
                        help="Write Markdown to this file (default: stdout)")
    parser.add_argument("--dpi", type=int, default=150,
                        help="Render resolution (default: 150)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider (default: PROVIDER from .env)")
    parser.add_argument("--model", default=None,
                        help="Model override (default: provider's model from .env)")
    args = parser.parse_args()

    pages = _parse_pages(args.pages) if args.pages else None
    markdown = extract_pdf(args.pdf, pages=pages, dpi=args.dpi,
                           provider=args.provider, model=args.model)

    if args.out:
        Path(args.out).write_text(markdown)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(markdown)


if __name__ == "__main__":
    main()
