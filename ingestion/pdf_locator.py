"""Find a paper's PDF on local disk or in S3_PAPERS_URL.

The corpus is not stored uniformly. The OpenReview / ECCV / SIGIR scrapers
write papers/<sanitized venueid>/<sanitized title>.pdf and tools/upload_s3.py
mirrored that layout to S3; the CVPR / ICLR / ACL-ARR batches were uploaded
as <some folder>/<paper_id>.pdf instead (e.g. papers/ICRL/ab24d789e57b.pdf).

Rather than probing S3 per paper, PdfLocator lists the prefix once (~20k
keys, a few seconds) and resolves each paper by, in order:

    1. the scraper-layout key            papers/<venueid>/<title>.pdf
    2. the metadata entry's own `s3_uri`  (ICLR batch records it)
    3. a <paper_id>.pdf filename anywhere under the prefix

Shared by ingestion/worker.py (extraction) and extraction/classify_ml.py.
Auth is ambient AWS credentials, same as ArtifactStore.
"""

import os
import re
import threading
from pathlib import Path

from indexing.ids import sanitize_filename
from ingestion.s3store import ArtifactStore


def scraper_relpath(meta: dict) -> str:
    """<sanitized venueid>/<sanitized title>.pdf — the scraper layout,
    relative to papers/ (local) or S3_PAPERS_URL (S3)."""
    return (f"{sanitize_filename(meta['venueid'])}/"
            f"{sanitize_filename(meta['title'])}.pdf")


def pdf_store() -> ArtifactStore | None:
    """ArtifactStore for S3_PAPERS_URL, or None when unset (local only)."""
    url = os.environ.get("S3_PAPERS_URL")
    return ArtifactStore(url) if url else None


class PdfLocator:
    def __init__(self, papers_root: Path, store: ArtifactStore | None = None):
        self.papers_root = papers_root
        self.store = store
        self._keys: set[str] | None = None
        self._by_id: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ index

    def _index(self) -> None:
        """List the S3 prefix once (thread-safe, lazy)."""
        if self._keys is not None or not self.store:
            return
        with self._lock:
            if self._keys is not None:
                return
            prefix = self.store.key() + "/" if self.store.key() else ""
            keys = set(self.store._list(prefix))
            by_id = {}
            for k in keys:
                stem = k.rsplit("/", 1)[-1]
                if re.fullmatch(r"[0-9a-f]{12}\.pdf", stem):
                    by_id.setdefault(stem[:-4], k)
            self._keys, self._by_id = keys, by_id
            print(f"Indexed {len(keys)} objects under {self.store.url()} "
                  f"({len(by_id)} named by paper_id)", flush=True)

    def s3_key(self, meta: dict) -> str | None:
        """S3 key of this paper's PDF, or None if not in the bucket."""
        if not self.store:
            return None
        self._index()
        assert self._keys is not None
        key = self.store.key(scraper_relpath(meta))
        if key in self._keys:
            return key
        uri = meta.get("s3_uri") or ""
        if uri.startswith(f"s3://{self.store.bucket}/"):
            key = uri.split("/", 3)[3]
            if key in self._keys:
                return key
        return self._by_id.get(meta["paper_id"])

    # ------------------------------------------------------------ lookup

    def local_path(self, meta: dict) -> Path | None:
        """Existing local PDF (scraper layout, or where a previous fetch
        put it), or None."""
        local = self.papers_root / scraper_relpath(meta)
        if local.exists():
            return local
        key = self.s3_key(meta) if self.store else None
        if key:
            local = self.papers_root / self._relpath(key)
            if local.exists():
                return local
        return None

    def available(self, meta: dict) -> bool:
        """True if the PDF exists locally or in S3 (no download)."""
        return self.local_path(meta) is not None or self.s3_key(meta) is not None

    def fetch(self, meta: dict) -> tuple[Path | None, bool]:
        """(local pdf path or None, downloaded?). Downloaded files land at
        papers/<same relative key as S3>, so they can be reused or removed
        by the caller once it is done with them."""
        local = self.local_path(meta)
        if local:
            return local, False
        key = self.s3_key(meta)
        if not key:
            return None, False
        local = self.papers_root / self._relpath(key)
        local.parent.mkdir(parents=True, exist_ok=True)
        self.store.s3.download_file(self.store.bucket, key, str(local))
        return local, True

    def _relpath(self, key: str) -> str:
        prefix = self.store.key()
        if prefix and key.startswith(prefix + "/"):
            return key[len(prefix) + 1:]
        return key
