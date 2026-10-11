"""uv run python -m critic --assessment FILE --root DIR --out FILE"""
import argparse
import asyncio
from pathlib import Path
from critic.pipeline import ResearchCritic, Settings
from novelty.assessment_schemas import NoveltyAssessmentResult
from reasoning.evidence import PaperStore


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('assessment', 'root', 'out'):
        p.add_argument('--' + name, required=True)
    for name in ('provider', 'model', 'review-model'):
        p.add_argument('--' + name)
    for name, value in Settings().model_dump().items():
        p.add_argument('--' + name.replace('_', '-'), type=type(value), default=value)
    return p


async def run(args):
    out = Path(args.out)
    if out.exists():
        raise ValueError('output already exists; choose a new file')
    novelty = NoveltyAssessmentResult.model_validate_json(Path(args.assessment).read_text())
    result = await ResearchCritic(PaperStore(Path(args.root)), provider=args.provider,
        model=args.model, review_model=args.review_model,
        settings=Settings(**{k: getattr(args, k) for k in Settings.model_fields})).run(novelty)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x') as f:
        f.write(result.model_dump_json(indent=2) + '\n')
    import json
    print(json.dumps(result.handoff(), indent=2))
    return all(c.critique is not None for c in result.candidates) and result.portfolio.assessment is not None


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(run(parser().parse_args())) else 1)
