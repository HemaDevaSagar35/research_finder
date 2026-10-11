"""uv run python -m novelty --directions FILE --root DIR --index-dir DIR --out FILE"""
import argparse
import asyncio
import json
from pathlib import Path

from directions.schemas import GenerationResult
from novelty.pipeline import NoveltySearcher, Settings
from reasoning.evidence import PaperStore
from research.retrieval import LocalBackend, OpenSearchBackend, MultiQueryRetriever


async def run(args):
    generation = GenerationResult.model_validate_json(Path(args.directions).read_text())
    extras = json.loads(Path(args.extra_pages).read_text()) if args.extra_pages else None
    if Path(args.out).exists():
        raise ValueError('output already exists; choose a new file')
    client = None
    if args.backend == 'local':
        backend = LocalBackend(Path(args.index_dir), embedding=args.embedding, use_vector=not args.no_vector)
        corpus = str(Path(args.index_dir).resolve())
    else:
        from indexing.opensearch_index import get_client
        client = get_client()
        backend, corpus = OpenSearchBackend(client), 'configured-opensearch-index'
    try:
        async with MultiQueryRetriever(backend, concurrency=args.concurrency) as retriever:
            searcher = NoveltySearcher(PaperStore(Path(args.root)), retriever, provider=args.provider,
                model=args.model, review_model=args.review_model, corpus_id=corpus,
                retrieval_mode='bm25_only' if args.no_vector else 'hybrid',
                settings=Settings(**{k: getattr(args, k) for k in Settings.model_fields}))
            result = await searcher.run(generation, extra_pages=extras)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.model_dump_json(indent=2)+'\n')
        return bool(result.candidates) and all(c.signature and not c.diagnostic and
            all(s.status == 'complete' for s in c.searches) for c in result.candidates)
    finally:
        if client is not None:
            client.close()
        else:
            backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('directions', 'root', 'out'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--backend', choices=['local', 'opensearch'], default='local')
    parser.add_argument('--index-dir', default='index')
    parser.add_argument('--embedding')
    parser.add_argument('--no-vector', action='store_true', help='Explicit local BM25-only mode; not a hybrid-search validation.')
    parser.add_argument('--extra-pages', help='JSON mapping of source paper IDs to additional available page numbers.')
    for name in ('provider', 'model', 'review-model'):
        parser.add_argument('--'+name)
    for key, value in Settings().model_dump().items():
        parser.add_argument('--'+key.replace('_', '-'), type=type(value), default=value)
    args = parser.parse_args()
    if args.backend == 'opensearch' and (args.no_vector or args.embedding):
        parser.error('--no-vector/--embedding require the local backend')
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == '__main__':
    main()
