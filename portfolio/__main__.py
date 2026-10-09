"""Render saved reviewed artifacts: python -m portfolio --critic FILE --out FILE."""
import argparse
import json
from pathlib import Path
from critic.schemas import CriticResult
from critic.refinement_loop import RefinementResult
from portfolio import build_portfolio, SelectionSettings
from portfolio.render import markdown


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--critic', type=Path)
    source.add_argument('--refinement', type=Path)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--markdown', type=Path)
    p.add_argument('--min-directions', type=int, default=3)
    p.add_argument('--max-directions', type=int, default=5)
    return p


def run(args):
    paths = [p for p in (args.out, args.markdown) if p is not None]
    if len({p.resolve() for p in paths}) != len(paths):
        raise ValueError('JSON and Markdown outputs must differ')
    for path in paths:
        if path.exists():
            raise ValueError(f'output already exists: {path}')
    source = (CriticResult.model_validate_json(args.critic.read_text()) if args.critic else
              RefinementResult.model_validate_json(args.refinement.read_text()))
    result = build_portfolio(source, SelectionSettings(min_directions=args.min_directions,
                                                       max_directions=args.max_directions))
    outputs = [result.model_dump_json(indent=2) + '\n']
    if args.markdown:
        outputs.append(markdown(result))
    for path, content in zip(paths, outputs):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x') as stream:
            stream.write(content)
    print(json.dumps(dict(status=result.status, counts=result.counts,
                         selection_shortfall=result.selection_shortfall), indent=2))
    return result.status == 'ready'


if __name__ == '__main__':
    raise SystemExit(0 if run(parser().parse_args()) else 1)
