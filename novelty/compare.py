"""uv run python -m novelty.compare --directions FILE --search FILE --root DIR --out FILE"""
import argparse
import asyncio
from pathlib import Path
from directions.schemas import GenerationResult
from novelty.schemas import NoveltySearchResult
from novelty.comparison import NoveltyComparator, Settings
from reasoning.evidence import PaperStore


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('directions', 'search', 'root', 'out'):
        p.add_argument('--'+name, required=True)
    for name in ('provider', 'model', 'review-model'):
        p.add_argument('--'+name)
    p.add_argument('--paper-id', action='append', help='Explicit subset; every omitted shortlisted pair remains recorded as skipped.')
    for name, value in Settings().model_dump().items():
        p.add_argument('--'+name.replace('_','-'), type=type(value), default=value)
    return p


async def run(args):
    out = Path(args.out)
    if out.exists():
        raise ValueError('output already exists; choose a new file')
    result = await NoveltyComparator(PaperStore(Path(args.root)), provider=args.provider,
        model=args.model, review_model=args.review_model,
        settings=Settings(**{k:getattr(args,k) for k in Settings.model_fields})).run(
            GenerationResult.model_validate_json(Path(args.directions).read_text()),
            NoveltySearchResult.model_validate_json(Path(args.search).read_text()), paper_ids=args.paper_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x') as f:
        f.write(result.model_dump_json(indent=2)+'\n')
    selected = [p for c in result.candidates for p in c.papers if p.status != 'skipped']
    return bool(selected) and all(p.status == 'complete' for p in selected) and all(not c.diagnostic for c in result.candidates)


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(run(parser().parse_args())) else 1)
