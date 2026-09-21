"""Corpus-scale extraction: run every downloaded PDF through
markdown → paper.json → summary.md, pushing artifacts to S3 as they are
produced.

    worker.py     process ONE paper end-to-end (the unit of work; a future
                  upload API/queue consumer calls the same function)
    backfill.py   batch driver over papers/metadata.json with a SQLite
                  status manifest (resume, retry, progress)
    s3store.py    S3 artifact-store helpers (S3_ARTIFACTS_URL)

Indexing is deliberately a separate stage (indexing/) that reads paper.json
artifacts back from S3 — see docs/offline_ingestion_design.md.
"""
