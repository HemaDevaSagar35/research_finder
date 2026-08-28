"""Canonical paper ids and filename helpers.

The paper_id is a deterministic hash of normalized (title, venue, year), so it
is source-agnostic (OpenReview, CVF, arXiv, ...) and recomputable from
metadata alone. See docs/offline_ingestion_design.md for the rationale.
"""

import hashlib
import re


def _norm(text: str) -> str:
    """Lowercase and strip everything but letters/digits, so LaTeX, casing,
    and punctuation differences between sources don't change the id."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def paper_id(title: str, venue: str, year: int | str | None) -> str:
    key = f"{_norm(title)}|{_norm(venue)}|{year if year is not None else ''}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def openreview_paper_id(title: str, venueid: str) -> str:
    """paper_id for an OpenReview entry, e.g. venueid 'ICML.cc/2026/Conference'."""
    match = re.search(r"(19|20)\d{2}", venueid)
    return paper_id(title, venueid, match.group(0) if match else None)


def sanitize_filename(name: str, max_len: int = 150) -> str:
    """Same function the scrapper uses to name PDFs; kept here as the single
    source of truth for linking metadata titles back to folders on disk."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name[:max_len].rstrip(" .") or "untitled"


def markdown_folder_name(title: str) -> str:
    """Folder name pdf_to_markdown.py produces for a paper downloaded by the
    scrapper: PDF stem (sanitized title) with spaces turned to underscores."""
    return sanitize_filename(title).replace(" ", "_")
