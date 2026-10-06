"""Full saved-candidate critic validation with raw requests, responses and provenance."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from critic.pipeline import ResearchCritic, Settings, transport
from critic.evidence import packet_for
from critic.schemas import CriticResult
from llm_client import AsyncLLMClient, ChatResult
from novelty.assessment_schemas import NoveltyAssessmentResult
from reasoning.budget import estimate_tokens
from reasoning.evidence import PaperStore
from tools.validate_novelty_search import save


async def main(args):
    novelty = NoveltyAssessmentResult.model_validate_json(Path(args.assessment).read_text())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    save(out/'manifest.json', vars(args))
    store = PaperStore(Path(args.root))
    settings = Settings(max_calls=args.max_calls, concurrency=args.concurrency,
        max_input_tokens=args.max_input_tokens, max_output_tokens=args.max_output_tokens)
    if args.prepare_only:
        critic = ResearchCritic(store, settings=settings)
        packets = []
        for d in novelty.inputs.generation.directions:
            support, _ = await critic.io._context(d, {})
            packet = packet_for(novelty, d, {k: support[k] for k in ('paper_artifacts', 'pages')})
            packed = transport(packet)
            packets.append(dict(direction_id=d.direction_id, targets=len(packet['coverage']),
                comparisons=len(packet['evidence']), source_passages=len(packet['source_passages']),
                estimated_packet_tokens=estimate_tokens(json.dumps(packed, ensure_ascii=False, separators=(',', ':')))))
        save(out/'prepared.json', packets)
        print(json.dumps(packets, indent=2))
        return True
    events = []
    checkpoints = []
    if args.replay_dir:
        for request_path in sorted((Path(args.replay_dir)/'calls').glob('*.request.json')):
            response_path = request_path.with_name(request_path.name.replace('.request.json', '.response.json'))
            if response_path.exists():
                response = ChatResult(**json.loads(response_path.read_text()))
                if not response.truncated:
                    checkpoints.append((json.loads(request_path.read_text()), response, str(response_path)))
    client = AsyncLLMClient(args.provider, concurrency=args.concurrency, max_retries=0, timeout=600)
    async def chat(**kw):
        req = json.loads(kw['messages'][1]['content'])
        event = dict(id=len(events), task=req['task'], start=time.time())
        events.append(event)
        save(out/'calls'/f"{event['id']:03d}.request.json", kw)
        try:
            reused = next(((saved, path) for request, saved, path in checkpoints
                if all(request.get(key) == kw.get(key) for key in ('messages', 'model', 'response_format'))), None)
            if reused:
                response, checkpoint_path = reused
                event.update(checkpoint_replay=True, checkpoint_path=checkpoint_path)
            else:
                response = await client.chat_result(**kw)
            save(out/'calls'/f"{event['id']:03d}.response.json", asdict(response))
            event.update(model=response.model, finish_reason=response.finish_reason)
            return response
        except Exception as exc:
            event['error'] = str(exc)
            raise
        finally:
            event['end'] = time.time()
            save(out/'calls.json', events)
            print(json.dumps(event), flush=True)
    try:
        result = await ResearchCritic(store, chat, review_chat=chat, provider=client.provider,
            model=client.default_model, settings=settings).run(novelty)
        save(out/'result.json', result.model_dump())
        saved = CriticResult.model_validate_json((out/'result.json').read_text())
        summary = dict(handoff=saved.handoff(), calls=saved.calls, usage=saved.usage,
            api_calls=sum(not e.get('checkpoint_replay', False) for e in events),
            checkpoint_replays=sum(e.get('checkpoint_replay', False) for e in events),
            candidates=[dict(direction_id=c.direction_id, published=c.critique is not None,
                targets=[dict(target_id=t.target_id, action=t.action, rationale=t.rationale,
                    learning_if_negative=t.learning_if_negative) for t in c.critique.targets] if c.critique else [],
                revisions=[r.model_dump() for r in c.critique.revisions] if c.critique else [],
                diagnostic=c.diagnostic, reviews=[r.report.decision if r.report else 'error' for r in c.reviews]) for c in saved.candidates])
        save(out/'summary.json', summary)
        print(json.dumps(summary, indent=2))
        return all(c.critique is not None for c in saved.candidates) and saved.portfolio.assessment is not None
    finally:
        await client.raw.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('assessment', 'root', 'out'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--provider', default='deepseek')
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--replay-dir', help='Reuse successful calls with identical messages/model/format; output reservation may differ.')
    p.add_argument('--max-calls', type=int, default=40)
    p.add_argument('--concurrency', type=int, default=4)
    p.add_argument('--max-input-tokens', type=int, default=1000000)
    p.add_argument('--max-output-tokens', type=int, default=32768)
    raise SystemExit(0 if asyncio.run(main(p.parse_args())) else 1)
