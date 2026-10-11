# Pipeline stages

One file per stage of the query-to-portfolio pipeline. Each file answers four
questions in the same order: what the stage is for, what it takes in, what it
does (and where), and what it produces. Everything is written from the code on
the current branch; the design reference for most stages is
`docs/research_path_generator_architecture_refined.md` (section numbers are
cited as "design §N"), and each stage file notes where the implementation
departs from it.

## Orchestration

`research/pipeline.py` owns the stage order and checkpointing.

| Symbol | Where | Role |
|---|---|---|
| `PipelineConfig` | `research/pipeline.py:39` | One validated config for the whole run; stage settings are nested fields (`novelty_search`, `comparison`, `assessment`, `critic`, `selection`, ...). |
| `STAGES` | `research/pipeline.py:73` | Ordered `{stage_name: result_model}`; the model is used to validate both fresh results and saved checkpoints. |
| `StageRunner.execute(name, state)` | `research/pipeline.py:138` | Dispatches one stage; `state` holds every prior stage's validated result. `common = dict(provider, model, review_model)` is passed to every LLM stage. |
| `run_pipeline(config, out, resume=...)` | `research/pipeline.py:222` | Runs `[s for s in STAGES if s != 'refinement']` in order, writes `out/<stage>.json`, stops early on empty retrieval or no directions, then writes `final_portfolio.*`, `stage16.*`, `summary.json`. |
| `research/recover.py` | | Replays `comparison → assessment → critic → portfolio` from a saved run (`--from-run`, `--direction`, `--paper`, `--start-at`). |

Checkpoint format for every `out/<stage>.json`:

```json
{"stage": "...", "parent_sha256": "...", "result_sha256": "...", "result": { ... }}
```

`parent_sha256` chains to the previous checkpoint (the first parent is the
digest of `manifest.json`), `result_sha256 = digest(result)` where `digest` is
`sha256(json.dumps(data, sort_keys=True))` (`opportunities/miner.py:96`).
Resume validates the whole chain before any provider work.

## Stage order and files

| # | Stage | Result model | Doc |
|---|---|---|---|
| 1 | `plan` | `QueryPlan` | [01_plan.md](01_plan.md) |
| 2 | `retrieval` | `RetrievalResult` | [02_retrieval.md](02_retrieval.md) |
| 3 | `contexts` | `Contexts` | [03_contexts.md](03_contexts.md) |
| 4 | `landscape` | `Landscape` | [04_landscape.md](04_landscape.md) |
| 5 | `reasoning` | `CrossPaperReasoning` | [05_reasoning.md](05_reasoning.md) |
| 6 | `opportunities` | `MiningResult` | [06_opportunities.md](06_opportunities.md) |
| 7 | `directions` | `GenerationResult` | [07_directions.md](07_directions.md) |
| 8 | `novelty_search` | `NoveltySearchResult` | [08_novelty_search.md](08_novelty_search.md) |
| 9 | `comparison` | `NoveltyComparisonResult` | [09_comparison.md](09_comparison.md) |
| 10 | `assessment` | `NoveltyAssessmentResult` | [10_assessment.md](10_assessment.md) |
| 11 | `critic` (+ hypothesis reviews) | `CriticResult`, `hypothesis_reviews.json` | [11_critic.md](11_critic.md) |
| 12 | `refinement` (not run by `run_pipeline`) | `RefinementResult` | [12_refinement.md](12_refinement.md) |
| 13 | `portfolio` | `FinalPortfolio` | [13_portfolio.md](13_portfolio.md) |
| 14 | stage 16 publication (post-portfolio) | `stage16.json` | [14_stage16.md](14_stage16.md) |

Early exits in `run_pipeline`: `retrieval` with no papers → `summary.status =
'empty'`; `directions` with no directions → `'blocked'` (if diagnostics) or
`'empty'`. Everything after is skipped.

## Identifier conventions used across stages

| ID | Format | Minted in |
|---|---|---|
| `paper_id` | 12 hex chars, sha256 of normalized title/venue/year | `indexing/ids.py` |
| `record_id` | `<paper_id>#<type>/<n>[#p<k>]` | index contract (`docs/index_contract.md`) |
| landscape `item_id` | `<kind>_<24 hex>` via `stable_id` | `landscape/schemas.py:50` |
| `thread_id` / `evidence_id` | `t002` / `t002-e001` | `reasoning/cross_paper.py`, `reasoning/evidence.py` |
| finding / observation / tension | `t002-f01` / `t002-o01` / `t002-x01` | `reasoning/cross_paper.py` |
| namespaced evidence | `observation:t002-o01/t002-e001` | `opportunities/miner.py:207` |
| `page_id` | `<paper_id>#p<N>` | reasoning review, used everywhere after |
| `opportunity_id` | `op-<batch:03d>-<cand:03d>` | `opportunities/miner.py:344` |
| `direction_id` / `hypothesis_id` / `experiment_id` | `dir-001` / `dir-001-h01` / `dir-001-e01` | `directions/generator.py:350` |
| `passage_id` | `<page_id>:<12 hex>` | `novelty/comparison_schemas.py:14` |

## Reading an LLM stage

All LLM stages share the same shape: a *draft* call producing a strict pydantic
model, an independent *review* call on the draft, at most one format repair per
call (resend with `invalid_output` + `validation_errors`), and at most two
draft/review rounds. Reviews that do not `pass` withhold the result and leave
a `diagnostic` string; nothing is promoted by code. Provider/model come from
`PipelineConfig.provider/model/review_model`, else `<STAGE>_PROVIDER` /
`<STAGE>_MODEL` / `<STAGE>_REVIEW_MODEL`, else `PROVIDER`; transport details
are in `docs/llm_client.md`.
