"""Bounded live signature/review/full-index retrieval/rerank validation.

Only this new stage runs; upstream directions are saved inputs. Stores raw calls
and explicit coverage. A retrieval shortlist is not a novelty decision.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from directions.schemas import GenerationResult
from llm_client import AsyncLLMClient, ChatResult
from novelty.pipeline import NoveltySearcher, Settings
from novelty.schemas import NoveltySearchResult
from reasoning.evidence import PaperStore
from research.retrieval import LocalBackend, MultiQueryRetriever


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2)+'\n')


async def main(args):
    generation = GenerationResult.model_validate_json(Path(args.directions).read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    save(out/'input.json', generation.model_dump())
    events, active, peak = [], 0, 0
    reused = json.loads(Path(args.reuse_signature_call).read_text()) if args.reuse_signature_call else None
    client = AsyncLLMClient('deepseek', concurrency=4, max_retries=0, timeout=300)
    backend = LocalBackend(Path(args.index_dir), use_vector=not args.no_vector)

    async def chat(**kw):
        nonlocal active, peak
        req = json.loads(kw['messages'][1]['content'])
        if reused and req['task'] == 'create_signature':
            original = json.loads(reused['request']['messages'][1]['content'])
            if original['payload'] != req['payload']:
                raise ValueError('saved signature call context differs from current input')
            value = reused['response']
            return ChatResult(value['text'], value['finish_reason'], value['model'])
        event = {'id': len(events), 'task': req['task'], 'start': time.time()}
        events.append(event)
        active += 1
        peak = max(peak, active)
        try:
            response = await client.chat_result(**kw)
            event.update(model=response.model, finish_reason=response.finish_reason)
            save(out/'calls'/f"{event['id']:03d}.json", {'request': kw, 'response': asdict(response)})
            return response
        except Exception as exc:
            event['error'] = type(exc).__name__
            raise
        finally:
            active -= 1
            event['end'] = time.time()
            save(out/'calls.json', {'events': events, 'peak': peak})

    try:
        async with MultiQueryRetriever(backend, concurrency=4) as retriever:
            runner = NoveltySearcher(PaperStore(Path(args.root)), retriever, chat,
                provider=client.provider, model=client.default_model, corpus_id=str(Path(args.index_dir).resolve()),
                retrieval_mode='bm25_only' if args.no_vector else 'hybrid',
                settings=Settings(max_calls=args.max_calls, max_queries=2, candidate_k=20, rerank_k=5))
            result = await runner.run(generation)
        result.run['validation_retrieval_mode'] = 'bm25_only' if args.no_vector else 'hybrid'
        save(out/'result.json', result.model_dump())
        saved = NoveltySearchResult.model_validate_json((out/'result.json').read_text())
        seeds = {p for d in generation.directions for p in d.paper_artifact_hashes}
        summary = {'api_calls': len(events), 'peak_api_concurrency': peak, 'calls': result.calls,
            'usage': result.usage, 'retrieval_mode': result.run['validation_retrieval_mode'],
            'candidates': [{'direction_id': c.direction_id, 'diagnostic': c.diagnostic,
                'review_decisions': [r.report.decision if r.report else 'error' for r in c.reviews],
                'targets': [{'target_id': s.target_id, 'status': s.status,
                    'retrieved': len(s.retrieval.papers) if s.retrieval else 0, 'ranked': len(s.ranked),
                    'ranked_outside_seed': sum(p.paper_id not in seeds for p in s.ranked),
                    'diagnostic': s.diagnostic} for s in c.searches]} for c in saved.candidates],
            'reused_signature_call': args.reuse_signature_call,
            'semantic_inspection': 'pending', 'novelty': 'not_assessed'}
        save(out/'summary.json', summary)
        print(json.dumps(summary), flush=True)
        return bool(saved.candidates) and all(c.signature and not c.diagnostic and
            all(s.status == 'complete' for s in c.searches) for c in saved.candidates)
    finally:
        backend.close()
        await client.raw.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directions', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--root', default='/srv/research_finder/markdown')
    parser.add_argument('--index-dir', default='/srv/research_finder/index')
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--no-vector', action='store_true')
    parser.add_argument('--reuse-signature-call', help='Reuse a saved raw creation response with identical context; review/revision/search remain live.')
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)
