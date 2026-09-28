"""Offline index over extracted paper.json files.

Pipeline (see docs/offline_ingestion_design.md):

    index_contract.py  which schema field goes where (TEXT/META/ONDEMAND);
                       --verify-flatten checks flatten.py against it
    flatten.py         paper.json -> typed records (index/records.jsonl)
    build_index.py     records -> BM25 + SQLite (shared) and per-variant
                       embeddings + FAISS under index/embeddings/<slug>/
    search.py          hybrid query CLI/library (vector + BM25, META filters,
                       rolled up to papers)
"""
