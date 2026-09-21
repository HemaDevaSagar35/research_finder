"""AWS OpenSearch backend: create indices, bulk-load, and hybrid-search.

Replaces the local FAISS + BM25 + SQLite triple with one backend: OpenSearch
does BM25 natively (its default text scoring), vector search via knn_vector
fields, and metadata filtering. Two indices:

    <prefix>-records  one doc per flattened record (indexing/flatten.py);
                      _id = record_id, so re-indexing a paper is an upsert
    <prefix>-papers   one doc per paper (metadata rollup); _id = paper_id

The embedding provider/model/dimension are recorded in the records index
mapping _meta at creation time; queries always embed with that same model.
Switching embedding models means recreating the index (--create errors on a
mismatch rather than silently mixing vector spaces).

Hybrid search runs a BM25 match and a k-NN query separately and fuses them
client-side with reciprocal-rank fusion — OpenSearch's server-side hybrid
pipelines are version-sensitive and unsupported on Serverless, and RRF on
two result lists behaves the same for our sizes. Note: a *managed* domain is
recommended over Serverless, which disallows client-chosen _id and would
break idempotent re-indexing.

Auth: endpoints on amazonaws.com use SigV4 with your AWS credentials
(AWS_PROFILE / AWS_REGION respected); anything else (e.g. local docker) uses
OPENSEARCH_USER/OPENSEARCH_PASSWORD, or no auth when unset.

Usage:
    uv run python -m indexing.opensearch_index --create
    uv run python -m indexing.opensearch_index --load-local     # bulk-load index/
    uv run python -m indexing.opensearch_index --search "kv cache compression"
    uv run python -m indexing.opensearch_index --delete-paper <paper_id>
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
from dotenv import load_dotenv
from opensearchpy import OpenSearch, RequestsHttpConnection, helpers

from .embeddings import embed_config, embed_query, embed_texts

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
BULK_CHUNK = 500
RRF_K = 60  # standard reciprocal-rank-fusion constant


def _prefix() -> str:
    return os.environ.get("OPENSEARCH_INDEX_PREFIX", "research")


def records_index() -> str:
    return f"{_prefix()}-records"


def papers_index() -> str:
    return f"{_prefix()}-papers"


def get_client() -> OpenSearch:
    endpoint = os.environ.get("OPENSEARCH_ENDPOINT")
    if not endpoint:
        sys.exit("OPENSEARCH_ENDPOINT is not set (see .env.example).")
    url = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    use_ssl = url.scheme != "http"
    host, port = url.hostname, url.port or (443 if use_ssl else 9200)

    if host.endswith("amazonaws.com"):
        import boto3
        from opensearchpy import AWSV4SignerAuth
        service = "aoss" if ".aoss." in host else "es"
        session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"))
        parts = host.split(".")
        region = (os.environ.get("AWS_REGION") or session.region_name
                  or (parts[-4] if len(parts) >= 4 else None))
        if not region:
            sys.exit("Could not determine the AWS region; set AWS_REGION.")
        auth = AWSV4SignerAuth(session.get_credentials(), region, service)
    elif os.environ.get("OPENSEARCH_USER"):
        auth = (os.environ["OPENSEARCH_USER"],
                os.environ.get("OPENSEARCH_PASSWORD", ""))
    else:
        auth = None

    return OpenSearch(
        hosts=[{"host": host, "port": port}],
        http_auth=auth, use_ssl=use_ssl, verify_certs=use_ssl,
        connection_class=RequestsHttpConnection, timeout=60)


def _records_body(provider: str, model: str, dim: int) -> dict:
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "_meta": {"embedding_provider": provider,
                      "embedding_model": model,
                      "embedding_dim": dim},
            "properties": {
                "paper_id": {"type": "keyword"},
                "type": {"type": "keyword"},
                "text": {"type": "text"},
                "embedding": {
                    "type": "knn_vector", "dimension": dim,
                    "method": {"name": "hnsw", "engine": "lucene",
                               "space_type": "cosinesimil"}},
                "title": {"type": "text"},
                "venue": {"type": "keyword"},
                "venueid": {"type": "keyword"},
                "year": {"type": "integer"},
                # evidence pointers, returned but not searched
                "source_locations": {"type": "object", "enabled": False},
            },
        },
    }


_PAPERS_BODY = {
    "mappings": {"properties": {
        "paper_id": {"type": "keyword"},
        "title": {"type": "text"},
        "authors": {"type": "keyword"},
        "venue": {"type": "keyword"},
        "venueid": {"type": "keyword"},
        "year": {"type": "integer"},
        "abstract": {"type": "text"},
        "openreview_id": {"type": "keyword"},
        "forum_url": {"type": "keyword"},
        "folder": {"type": "keyword"},
        "research_areas": {"type": "keyword"},
        "keywords": {"type": "keyword"},
        "retrieval_tags": {"type": "object", "enabled": False},
    }},
}


def index_meta(client: OpenSearch) -> dict:
    """Embedding provider/model/dim the records index was created with."""
    mapping = client.indices.get_mapping(index=records_index())
    return mapping[list(mapping)[0]]["mappings"]["_meta"]


def ensure_indices(client: OpenSearch, provider: str, model: str,
                   dim: int) -> None:
    """Create both indices if missing; refuse an embedding-model mismatch."""
    if client.indices.exists(index=records_index()):
        meta = index_meta(client)
        if (meta["embedding_provider"], meta["embedding_model"]) != (provider, model):
            sys.exit(f"Index {records_index()} was built with "
                     f"{meta['embedding_provider']}/{meta['embedding_model']} "
                     f"but current config is {provider}/{model}. Delete and "
                     f"recreate the index to switch models.")
    else:
        client.indices.create(index=records_index(),
                              body=_records_body(provider, model, dim))
        print(f"Created index {records_index()} (dim {dim}, "
              f"{provider}/{model}).")
    if not client.indices.exists(index=papers_index()):
        client.indices.create(index=papers_index(), body=_PAPERS_BODY)
        print(f"Created index {papers_index()}.")


def _record_doc(rec: dict, vector, paper_row: dict) -> dict:
    """A records-index doc: the flattened record + its embedding, plus paper
    fields denormalized for filter/display without a join."""
    return {
        "paper_id": rec["paper_id"],
        "type": rec["type"],
        "text": rec["text"],
        "embedding": [float(x) for x in vector],
        "title": paper_row.get("title"),
        "venue": paper_row.get("venue"),
        "venueid": paper_row.get("venueid"),
        "year": paper_row.get("year"),
        "source_locations": rec.get("source_locations") or [],
    }


def index_paper(client: OpenSearch, paper_row: dict, records: list[dict],
                vectors) -> None:
    """Upsert one paper: its metadata doc and all of its record docs.
    Deterministic _ids make this idempotent; stale records from a previous
    extraction of the same paper are removed first."""
    pid = paper_row["paper_id"]
    client.delete_by_query(
        index=records_index(), ignore_unavailable=True,
        body={"query": {"term": {"paper_id": pid}}})
    actions = [{"_index": records_index(), "_id": rec["record_id"],
                "_source": _record_doc(rec, vec, paper_row)}
               for rec, vec in zip(records, vectors)]
    actions.append({"_index": papers_index(), "_id": pid,
                    "_source": paper_row})
    helpers.bulk(client, actions, chunk_size=BULK_CHUNK)


def delete_paper(client: OpenSearch, pid: str) -> None:
    client.delete_by_query(
        index=records_index(), ignore_unavailable=True,
        body={"query": {"term": {"paper_id": pid}}})
    client.delete(index=papers_index(), id=pid, ignore=[404])
    print(f"Deleted paper {pid} from both indices.")


def _filters(types=None, venues=None, year=None) -> list[dict]:
    out = []
    if types:
        out.append({"terms": {"type": types}})
    if venues:
        out.append({"terms": {"venueid": venues}})
    if year:
        out.append({"term": {"year": year}})
    return out


def search(client: OpenSearch, query: str, k: int = 10,
           types=None, venues=None, year=None) -> list[dict]:
    """Hybrid search: BM25 + k-NN, fused with reciprocal-rank fusion."""
    meta = index_meta(client)
    vector = embed_query(query, meta["embedding_provider"],
                         meta["embedding_model"])[0]
    filters = _filters(types, venues, year)
    fetch = max(50, k * 5)  # deep result lists make RRF meaningful

    bm25 = client.search(index=records_index(), body={
        "size": fetch, "_source": {"excludes": ["embedding"]},
        "query": {"bool": {"must": [{"match": {"text": query}}],
                           "filter": filters}}})
    knn = client.search(index=records_index(), body={
        "size": fetch, "_source": {"excludes": ["embedding"]},
        "query": {"bool": {
            "must": [{"knn": {"embedding": {
                "vector": [float(x) for x in vector], "k": fetch}}}],
            "filter": filters}}})

    scores: dict[str, float] = defaultdict(float)
    sources: dict[str, dict] = {}
    for resp in (bm25, knn):
        for rank, hit in enumerate(resp["hits"]["hits"]):
            scores[hit["_id"]] += 1.0 / (RRF_K + rank + 1)
            sources[hit["_id"]] = hit["_source"]
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    return [{"record_id": rid, "rrf_score": scores[rid], **sources[rid]}
            for rid in top]


def load_local(client: OpenSearch, index_dir: Path) -> None:
    """Bulk-load the locally built index/ artifacts (flatten + build_index
    output) — the fast first fill that reuses cached embeddings."""
    records = [json.loads(l) for l in
               (index_dir / "records.jsonl").read_text().splitlines() if l]
    papers = [json.loads(l) for l in
              (index_dir / "papers.jsonl").read_text().splitlines() if l]
    vectors = np.load(index_dir / "embeddings.npy")
    ids = json.loads((index_dir / "embedding_ids.json").read_text())
    by_rid = {rid: vectors[i] for i, rid in enumerate(ids["record_ids"])}
    missing = [r["record_id"] for r in records if r["record_id"] not in by_rid]
    if missing:
        sys.exit(f"{len(missing)} records have no cached embedding (e.g. "
                 f"{missing[0]}); run indexing.build_index first.")

    meta_path = index_dir / "index_meta.json"
    if meta_path.exists():
        m = json.loads(meta_path.read_text())
        provider, model = m["embedding_provider"], m["embedding_model"]
    else:
        provider, model = embed_config()
    ensure_indices(client, provider, model, int(vectors.shape[1]))

    rows = {p["paper_id"]: p for p in papers}
    actions = ([{"_index": records_index(), "_id": r["record_id"],
                 "_source": _record_doc(r, by_rid[r["record_id"]],
                                        rows.get(r["paper_id"], {}))}
                for r in records] +
               [{"_index": papers_index(), "_id": p["paper_id"],
                 "_source": p} for p in papers])
    done = 0
    for i in range(0, len(actions), BULK_CHUNK):
        helpers.bulk(client, actions[i:i + BULK_CHUNK])
        done += len(actions[i:i + BULK_CHUNK])
        print(f"  indexed {done}/{len(actions)} docs")
    print(f"Loaded {len(records)} records and {len(papers)} papers "
          f"into {records_index()} / {papers_index()}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--create", action="store_true",
                        help="Create the indices (dim probed from the "
                             "configured embedding model)")
    parser.add_argument("--load-local", action="store_true",
                        help="Bulk-load local index/ artifacts")
    parser.add_argument("--index-dir", default="index",
                        help="Local index dir for --load-local (default: index/)")
    parser.add_argument("--search", default=None, help="Hybrid search query")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--types", nargs="+", default=None,
                        help="Filter by record type(s)")
    parser.add_argument("--venues", nargs="+", default=None,
                        help="Filter by venueid(s)")
    parser.add_argument("--delete-paper", default=None,
                        help="Remove one paper (by paper_id) from the index")
    args = parser.parse_args()

    client = get_client()
    if args.create:
        provider, model = embed_config()
        dim = embed_texts(["dimension probe"], provider, model,
                          progress=False).shape[1]
        ensure_indices(client, provider, model, int(dim))
    if args.load_local:
        load_local(client, REPO_ROOT / args.index_dir)
    if args.delete_paper:
        delete_paper(client, args.delete_paper)
    if args.search:
        for hit in search(client, args.search, k=args.k,
                          types=args.types, venues=args.venues):
            print(f"\n[{hit['rrf_score']:.4f}] {hit['type']}  "
                  f"{hit['title']}  ({hit['venue']})")
            print(f"  {hit['text'][:300]}")
    if not any([args.create, args.load_local, args.delete_paper, args.search]):
        counts = {i: client.count(index=i, ignore_unavailable=True).get("count", 0)
                  for i in (records_index(), papers_index())}
        print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
