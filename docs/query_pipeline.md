# Initial query to final research portfolio

`python -m research` connects the existing modules into a single sequential run:

```text
query → query plan → initial retrieval → paper contexts → landscape
      → cross-paper reasoning → opportunities → directions
      → novelty search → complete shortlisted comparisons → novelty assessment
      → research critic → bounded refinement/revalidation → final portfolio
```

The scientific modules keep their existing validation, independent-review and
publication rules. The runner does not convert withheld results into approvals.
It is a CLI and callable async orchestration function, not an HTTP deployment.

## Run a query

From the repository directory, with the existing provider/embedding configuration
in `.env`:

```bash
uv run python -m research "Your research question" \
  --root /srv/research_finder/markdown \
  --index-dir /srv/research_finder/index \
  --out /home/hema/research_runs/my_query_run
```

The output directory must be new. The command runs real retrieval and provider
calls; its cost depends on the candidates, novelty shortlists and review/revision
rounds. It does not redownload or reingest the whole corpus.

- `--provider`, `--model`, `--review-model`: explicit provider/model overrides.
  The generation model applies to planning, landscape and downstream generation.
  With no override, existing stage-specific environment settings apply.
- `--papers`: initial retrieved seed-paper cap (default 15).
- `--backend opensearch`: use the existing configured OpenSearch backend instead
  of the local index. The CLI owns and closes the backend client.
- `--no-s3`: restrict source loading to the local corpus. Otherwise missing
  artifacts can be fetched through the configured `S3_ARTIFACTS_URL` fallback.
- `--concurrency`: set retrieval/reasoning concurrency and override the downstream
  stage concurrency settings. Landscape keeps its existing client limits.
- `--max-refinement-cycles 0|1|2`: default 2; 0 goes directly from critic to final
  output. Pending scientific work remains pending when refinement is disabled.
- `--min-directions`, `--max-directions`: desired minimum and selection cap,
  default 3 and 5. A shortfall is reported, not filled with unapproved candidates.

The initial retriever is the existing paper-level hybrid/RRF implementation;
this wiring does not add the still-deferred initial semantic reranker. Novelty
search retains its existing reviewed signatures and semantic reranking, over the
configured corpus rather than only the initial papers.

## Outputs

`final_portfolio.json` and `final_portfolio.md` are the section-17 deliverables
when the scientific pipeline reaches final selection. They include approved
section-16 reports when any candidate qualifies. `summary.json` distinguishes:

- `ready`: selected portfolio meets its minimum and has no pending candidates.
- `partial`: some candidates selected, with a shortfall or pending work.
- `blocked`: no approved candidates can advance yet.
- `empty`: initial retrieval returned no matches, or final selection is empty.
- `failed`: an exception interrupted execution; the stage and error are recorded.

If retrieval is empty or no reviewed directions are generated, the runner stops
with a summary and the completed checkpoints; it does not manufacture a final
candidate report. CLI exit codes are 0 for `ready`, 1 for a completed
partial/blocked/empty result, and 2 for configuration or execution failure.
Progress and library warnings go to stderr; the final summary goes to stdout.

Each stage also saves a checkpoint, for example `plan.json`, `retrieval.json`,
`contexts.json`, `landscape.json`, `directions.json`, `critic.json`, and
`portfolio.json`. Checkpoint files wrap the typed stage result in `result`, with
its digest and parent-checkpoint digest. The standalone `final_portfolio.json`
contains the ordinary `FinalPortfolio` directly. To use a checkpoint with an
older standalone stage CLI, extract its `result` object first.

The contexts checkpoint records the matched retrieval records, projected paper
cards and original artifact hashes. Missing/invalid retrieved papers fail the
run rather than being silently omitted. Later source-consuming stages continue
to enforce their own hash and evidence checks. Source references and scientific
history are retained through the final portfolio; no bibliography is invented.

## Resume

```bash
uv run python -m research --resume \
  --out /home/hema/research_runs/my_query_run
```

Resume reads configuration from the original manifest and validates every saved
checkpoint before making new calls. The query, configuration, code fingerprint,
and recorded provider/model/corpus-environment fingerprint must match. Corrupt
artifacts and gaps in the checkpoint sequence fail before new work. A process
lock prevents simultaneous writers. Stage files are published with atomic rename.

Only the missing suffix of stages runs. A completed blocked critic/refinement
result is reused as blocked, not silently retried with another allowance. An
interrupted stage that has not published a checkpoint runs again from its saved
inputs and can repeat provider calls; this is **stage-level**, not individual-call
or partial-refinement-cycle recovery. Existing specialized recovery tools remain
available for those cases. Keep the underlying corpus/index stable during a run
and resume: this runner does not snapshot the entire index or remote corpus.

Changing code or research settings requires a new run directory. A fully
completed resume validates saved artifacts and regenerates the final exports
without new scientific calls.

## Per-stage budgets

`--config settings.json` accepts `PipelineConfig` fields. CLI flags override
those values. The reasoner and downstream stages retain their existing per-stage
budgets by default; this is not one global token/call budget. Landscape and query
planning retain their existing native limits. A larger candidate pool can exhaust
native defaults, so configure budgets explicitly for larger runs, for example:

```json
{
  "opportunities": {"max_candidates_per_batch": 8, "max_calls": 80},
  "directions": {"max_calls": 80},
  "novelty_search": {"max_calls": 200, "candidate_k": 100, "rerank_k": 20},
  "comparison": {"max_calls": 3000},
  "assessment": {"max_calls": 100},
  "critic": {"max_calls": 100},
  "refinement": {"max_calls": 3000}
}
```

These are ceilings, not required spending or a guarantee that every review will
finish. No shortlisted paper is sampled out by the comparison runner. If a
candidate's scientific revision requires fresh search, the refinement loop uses
the configured novelty-search limits and shares its existing refinement call
budget. Review failures and exhausted budgets remain explicit upstream results.

## Python API

```python
from pathlib import Path
from research.pipeline import PipelineConfig, run_pipeline

summary = await run_pipeline(
    PipelineConfig(query="Your research question",
                   root=Path("/srv/research_finder/markdown"),
                   index_dir=Path("/srv/research_finder/index")),
    Path("/home/hema/research_runs/my_query_run"),
)
```

One runner owns one local/S3 `PaperStore` and one lazily initialized retriever.
Retrieval workers are drained before the backend closes, including on exceptions.
The reasoner's injected client is explicitly closed. Other scientific stages keep
their existing owned-client lifecycle.

## Validation — 2026-10-10

The runner tests exercise query/setting dispatch, actual direction-through-critic
and portfolio stages with synthetic model responses, unchanged resume without
new calls, interruption and suffix-only recovery, configuration/code/checkpoint
mismatches, empty retrieval, missing source artifacts, exclusive output directories,
run locking and propagation of novelty settings into refinement.

These are offline integration tests. They do not establish a successful new
real-provider query-to-approved-portfolio run, nor resolve the previous real
candidate's H4/E4 review issues.

Final regression: **626 tests plus 13 subtests passed**, with one existing
faiss/NumPy deprecation warning. The focused runner/refinement suite passed
41 tests. CLI help and `git diff --check` pass. No new live provider run was
performed as part of implementing this wiring.

### Bibliography enrichment

Section-17 exports now include readable paper titles, authors, year, venue and
public/PDF links. With the local backend, the runner automatically reads
`INDEX_DIR/papers.jsonl` and the sibling `papers/metadata.json` when present.
Additional or OpenSearch-side catalogs can be supplied with repeatable
`--metadata PATH` flags or `metadata_paths` in configuration. Explicit catalogs
have priority. Extracted `paper_metadata` fills missing fields when its artifact
hash matches the scientific inputs. Missing metadata stays explicit; it does not
change scientific decisions. See [bibliography details](final_portfolio.md#readable-paper-references--2026-10-10).

Concurrency defaults to 100 for the E2E runner and downstream stage settings.
`--concurrency 100` overrides both the runner and all nested stage concurrency
settings, including when a configuration file contains older lower values.
The runner also passes its concurrency to landscape extraction, normalization,
and aggregation clients. These landscape clients have independent limits.
Stages still wait for their predecessors to finish; concurrency permits up to
100 independent LLM requests per stage client, rather than forcing 100 requests
when fewer tasks are ready. Call budgets and research coverage are unchanged.

Stage `max_calls` defaults to `null` (unlimited). Omit it or set it to JSON
`null` to process work without a total LLM-attempt cap. An explicit nonnegative
integer still enforces a budget; `0` permits no calls. Attempt totals remain
recorded, with `calls.budget: null` for unlimited runs. Refinement shares this
allowance across its cycles. Per-item repair limits, concurrency, thread and
candidate limits, and refinement-cycle limits remain separate and unchanged.

When `--out` is omitted, the CLI creates a 16-character SHA-256 prefix folder
from the effective query and current nanosecond timestamp. The parent is
`research_runs` beside the repository (here `/home/hema/research_runs`),
independent of the shell's working directory. The full output path is printed
at startup. Explicit `--out` still overrides this; `--resume` requires the
original `--out` path. Existing directories are never overwritten by a new run.
