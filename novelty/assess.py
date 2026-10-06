"""uv run python -m novelty.assess --directions FILE --search FILE --comparisons FILE --out FILE"""
import argparse
import asyncio
import json
from pathlib import Path

from directions.schemas import GenerationResult
from novelty.schemas import NoveltySearchResult
from novelty.comparison_schemas import NoveltyComparisonResult
from novelty.assessment_schemas import Invalidations
from novelty.assessment import NoveltyAssessor, Settings


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('directions', 'search', 'comparisons', 'out'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--invalidations', help='Explicit novelty_invalidations_v1 manifest bound to the comparison digest.')
    for name in ('provider', 'model', 'review-model'):
        p.add_argument('--' + name)
    for name, value in Settings().model_dump().items():
        p.add_argument('--' + name.replace('_', '-'), type=type(value), default=value)
    return p


async def run(args):
    out = Path(args.out)
    if out.exists():
        raise ValueError('output already exists; choose a new file')
    result = await NoveltyAssessor(provider=args.provider, model=args.model, review_model=args.review_model,
        settings=Settings(**{k: getattr(args, k) for k in Settings.model_fields})).run(
            GenerationResult.model_validate_json(Path(args.directions).read_text()),
            NoveltySearchResult.model_validate_json(Path(args.search).read_text()),
            NoveltyComparisonResult.model_validate_json(Path(args.comparisons).read_text()),
            invalidations=Invalidations.model_validate_json(Path(args.invalidations).read_text()) if args.invalidations else None)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x') as f:
        f.write(result.model_dump_json(indent=2) + '\n')
    directions = {d.direction_id: d for d in result.inputs.generation.directions}
    print(json.dumps([dict(direction_id=c.direction_id, outcomes=c.outcomes(),
        refinement=c.refinement_handoff(directions[c.direction_id]), diagnostic=c.diagnostic) for c in result.candidates], indent=2))
    # Success means reviewed synthesis, not a conclusive novelty verdict.
    return bool(result.candidates) and all(c.assessment is not None for c in result.candidates)


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(run(parser().parse_args())) else 1)
