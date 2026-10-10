"""Run an initial research query through reviewed final portfolio output."""
import argparse
from contextlib import redirect_stdout
import asyncio
import json
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

from research.pipeline import PipelineConfig, run_pipeline


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('query', nargs='?', help='Initial research question; omit when resuming')
    p.add_argument('--out', required=True, type=Path, help='New run directory, or the original directory with --resume')
    p.add_argument('--resume', action='store_true', help='Reuse validated completed stage checkpoints')
    p.add_argument('--config', type=Path, help='JSON PipelineConfig overrides, including per-stage call/token budgets')
    p.add_argument('--root', type=Path, help='Local paper.json/Markdown corpus; S3 fallback is enabled unless disabled')
    p.add_argument('--index-dir', type=Path)
    p.add_argument('--backend', choices=('local', 'opensearch'))
    p.add_argument('--embedding')
    p.add_argument('--metadata', type=Path, action='append', dest='metadata_paths', help='Additional bibliography catalog; repeat in priority order')
    p.add_argument('--provider')
    p.add_argument('--model', help='Override the generation model in every stage')
    p.add_argument('--review-model')
    p.add_argument('--no-s3', action='store_true')
    p.add_argument('--concurrency', type=int)
    p.add_argument('--papers', type=int, dest='initial_papers')
    p.add_argument('--max-refinement-cycles', type=int, choices=(0,1,2))
    p.add_argument('--min-directions', type=int)
    p.add_argument('--max-directions', type=int)
    return p


def configuration(args):
    data = {}
    if args.resume:
        data = json.loads((args.out/'manifest.json').read_text())['config']
    if args.config:
        data.update(json.loads(args.config.read_text()))
    for key in ('query','root','index_dir','backend','embedding','provider','model','review_model',
                'initial_papers','max_refinement_cycles','concurrency','metadata_paths'):
        value = getattr(args, key)
        if value is not None:
            data[key] = value
    if args.no_s3:
        data['allow_s3'] = False
    selection = dict(data.get('selection', {}))
    for key in ('min_directions','max_directions'):
        value = getattr(args, key)
        if value is not None:
            selection[key] = value
    data['selection'] = selection
    if args.concurrency is not None:
        for name in ('opportunities','directions','novelty_search','comparison','assessment','critic','refinement'):
            data[name] = {**data.get(name, {}), 'concurrency': args.concurrency}
    return PipelineConfig.model_validate(data)


class StageProgress:
    """Timestamped stage events with monotonic wall-clock durations."""
    def __init__(self):
        self.started = {}

    def __call__(self, stage, status):
        now = time.perf_counter()
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        detail = ''
        if status == 'running':
            self.started[stage] = now
        elif status in ('completed', 'failed'):
            start = self.started.pop(stage, None)
            if start is not None:
                detail = f' ({now - start:.2f}s)'
        elif status == 'reused':
            detail = ' (checkpoint; not rerun)'
        print(f'[{stamp}] {stage}: {status}{detail}', file=sys.stderr, flush=True)


def main():
    args = parser().parse_args()
    try:
        config = configuration(args)
        progress = StageProgress()
        with redirect_stdout(sys.stderr):
            summary = asyncio.run(run_pipeline(config, args.out, resume=args.resume, progress=progress))
        print(json.dumps(summary, indent=2))
        return 0 if summary['status'] == 'ready' else 1
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
