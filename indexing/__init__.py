"""Offline index over extracted paper.json files.

Pipeline (see docs/offline_ingestion_design.md):

    flatten.py      paper.json -> typed records (index/records.jsonl)
    build_index.py  records -> embeddings + FAISS + BM25 + SQLite metadata
    search.py       hybrid query CLI (vector + BM25, rolled up to papers)
"""
