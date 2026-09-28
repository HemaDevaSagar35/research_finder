# Index contract

Source of truth: `indexing/index_contract.py`. Verify with

    uv run python -m indexing.index_contract                  # coverage check
    uv run python -m indexing.index_contract --verify-flatten # + flatten test
    uv run python -m indexing.index_contract --table          # per-type table

The coverage check fails if any `PaperAnalysis` leaf is unassigned or if the
contract names a path the schema no longer has. `--verify-flatten` builds a
synthetic paper with a unique sentinel in every leaf, runs
`flatten.flatten_paper`, and asserts each TEXT sentinel appears in a record of
the declared type, each META value is on the record meta / paper row, and no
ONDEMAND sentinel leaks into the index. When the extraction schema changes,
these two checks tell you exactly what the flatten must pick up.

## Principle

The index is for **finding** papers. `paper.json` (S3, by `paper_id`) is for
**reasoning** about them. Every leaf field is assigned to exactly one primary
bucket:

| bucket | what happens | query-addressable? |
|---|---|---|
| **TEXT** | joined into a typed record's `text`; embedded + BM25 | yes (semantic + lexical) |
| **META** | structured field on the record or the paper doc; filter / facet / boost | yes (exact match / range) |
| **ONDEMAND** | not indexed; downstream stages load `paper.json` for the retrieved `paper_id`s | no — but one hop away |

A TEXT field can *also* be kept as a META facet (`ALSO_META`) — e.g. dataset
names are embedded inside a `dataset` record and kept as `papers.dataset_names`
for filtering.

## Counts (schema v1, 222 leaves)

| | leaves |
|---|---|
| TEXT (embedded + BM25) | **170** (24 of these also kept as META facets) |
| META only (filters/facets) | **25** |
| ONDEMAND (`paper.json` by id) | **27** |
| **query-addressable = TEXT + META** | **195 / 222** |
| record types | 36 (flatten v1 had 19) |

## Record envelope

Every record, regardless of type:

```json
{
  "record_id": "<paper_id>#<type>/<n>[#p<k>]",
  "paper_id":  "0a1a3b2fff54",
  "type":      "summary",
  "text":      "<title>. <labelled statement ...>",
  "source_locations": [...],
  "meta": {
    "year": 2026, "venue": "COLM 2026",
    "research_areas": ["Model Compression", "Mixture-of-Experts", ...],
    "...per-type record META fields (severity, claim_type, ...)"
  }
}
```

`text` is what gets embedded and BM25'd. The paper `title` is prepended to
every record so a statement retrieved on its own still identifies its paper
to the embedding model and to the reader. `meta.year/venue/research_areas`
are denormalised from the paper doc so record-level filters work without a
join (needed for OpenSearch `knn` + `filter`).

Paper doc (`papers.jsonl` / `<prefix>-papers`): `paper_id, title, authors,
venue, venueid, year, paper_type, identifiers, abstract, urls, research_areas,
keywords, number_of_runs, code_available, code_url, data_available, data_url,
model_checkpoints_available`, plus facet arrays `model_names, dataset_names,
baseline_names, metric_names, hardware` and the 13 `retrieval_tags.*` lists.

## Record types

Cardinality: `1` = one record per paper, `N` = one per list item,
`N/item` = one per string in a `list[str]` (each string is its own statement).

| record type | per paper | text fields | record META |
|---|---|---|---|
| `summary` | 1 | one_sentence_summary, general_problem, specific_problem, proposed_solution, main_result, why_it_matters | — |
| `problem` | 1 | general_problem, specific_problem, motivation, why_difficult | — |
| `research_question` | N/item | research_questions | — |
| `hypothesis` | N/item | hypotheses | — |
| `research_gap` | 1 | gap_description, what_existed_before, limitations_of_prior_work, why_existing_methods_are_insufficient, evidence_for_gap | gap_explicitly_claimed_by_authors |
| `prior_work` | N | method_or_family, description, strengths, limitations, relationship_to_current_paper | — |
| `contribution` | N | contribution, novelty_claim, what_existed_before, what_this_paper_changes, why_it_matters | contribution_type, explicitly_claimed_by_authors |
| `method` | 1 | high_level_idea, architecture.description, model_components, information_flow | — |
| `pipeline_step` | N | step, name, description, inputs, outputs | — |
| `method_component` | N | name, purpose, inputs, operations, outputs, technical_details | novel_component |
| `objective` | N | name, description, purpose | — |
| `equation` | N | meaning, importance | — |
| `training` | 1 | training_procedure, training_data, preprocessing, optimization, initialization, fine_tuning_strategy, regularization, compute_requirements | — |
| `inference` | 1 | procedure, decoding_or_sampling, complexity, latency_considerations, throughput_considerations, memory_considerations | — |
| `systems` | 1 | time_complexity, space_complexity, flops, memory, memory_bandwidth, communication_cost, latency, throughput, other | — |
| `model` | N | name, role, architecture, parameter_count | — |
| `dataset` | N | name, purpose, size, task, domain, important_characteristics | — |
| `baseline` | N | name, description, why_selected, relationship_to_proposed_method | — |
| `metric` | N | name, description, what_it_measures | — |
| `evaluation_setup` | 1 | hardware, software, statistical_testing, other_details | — |
| `experiment` | N | experiment_name, research_question, hypothesis_being_tested, setup, comparison, datasets, models, baselines, metrics, key_result, authors_conclusion | evidence_supports_conclusion (+ datasets/models/baselines/metrics as keyword arrays) |
| `ablation` | N | component_or_variable, change, motivation, baseline_setting, modified_setting, result, quantitative_change, interpretation | — |
| `scaling` | N | variable, values_tested, observed_trend, interpretation | — |
| `key_result` | N | finding, quantitative_result, comparison, importance | — |
| `interesting_finding` | N | finding, why_interesting, possible_implication | expected_or_unexpected, authors_discuss_implication |
| `figure_table` | N | identifier, what_it_shows, key_observation, importance | type |
| `failure_case` | N | failure, conditions, observed_behavior, possible_cause, implication | — |
| `limitation_author` | N | limitation, impact | — |
| `limitation_inferred` | N | limitation, reasoning, evidence | severity |
| `future_work_author` | N | direction, motivation | — |
| `research_opportunity` | N | observation_or_limitation, research_question, why_it_matters, potential_approach | confidence |
| `claim` | N | claim, evidence, reasoning | claim_type, evidence_strength |
| `reproducibility` | 1 | required_resources, missing_information, reproducibility_assessment | — |
| `paper_card` | 1 | main_strengths, main_weaknesses, most_important_contribution, most_important_result, most_important_limitation, what_the_paper_demonstrates, what_the_paper_does_not_demonstrate | — |
| `open_question` | 1 | most_interesting_open_question | — |
| `tags` | 1 | keywords + all 13 retrieval_tags lists | — |

New in v2 vs flatten v1: `research_question`, `hypothesis`, `pipeline_step`,
`objective`, `equation`, `training`, `inference`, `systems`, `model`,
`dataset`, `baseline`, `metric`, `evaluation_setup`, `figure_table`,
`reproducibility`, `paper_card`, `open_question` (17 types), plus the title
prefix and the `meta` envelope on every record.

## ONDEMAND (27) — never searched, read from `paper.json`

Name/value tables, equations, result rows, and bookkeeping. No user or
planner query is phrased against these; the Comparator, Experiment Generator
and Landscape Builder read them after retrieval.

- `prior_work[].citations_mentioned_in_paper`
- `method.objectives_and_losses[].equation`
- `method.important_equations[].{equation_identifier, equation, variables[].symbol, variables[].meaning}`
- `method.training.important_hyperparameters[].{name, value}`
- `models[].configuration[].{name, value}`, `models[].pretrained`
- `datasets[].{train_split, validation_split, test_split}`
- `metrics[].better_direction`
- `experimental_setup.training_configuration[].{name, value}`
- `experimental_setup.inference_configuration[].{name, value}`
- `experimental_setup.random_seeds`
- `experiments[].results[].{method, metric, value, raw_value, unit, dataset_or_setting}`
- `reproducibility.reproduction_steps`

## META only (25)

Paper doc: `authors, year, venue, paper_type, identifiers[].{name,value},
research_areas, number_of_runs, code_available, code_url, data_available,
data_url, model_checkpoints_available`.

Record: `gap_explicitly_claimed_by_authors, contribution_type,
explicitly_claimed_by_authors, novel_component, evidence_supports_conclusion,
expected_or_unexpected, authors_discuss_implication, figure_table.type,
severity, confidence, claim_type, evidence_strength`.

These are what let the Opportunity Miner ask for e.g.
`type=interesting_finding AND expected_or_unexpected=unexpected` or
`type=limitation_inferred AND severity=high` instead of hoping the embedding
picks it up.

## Open items

- `CHUNK_CHARS` is now 4000 (v1's 2000 split `summary` records into two
  vectors). On the full corpus 1,346 records still split — almost all
  `paper_card` (avg 2,929 chars). If that matters, split `paper_card` into a
  strengths/contribution/result record and a weaknesses/limitation/not-
  demonstrated record rather than raising the limit further.
- Corpus after flatten v2: 983,126 records / 7,225 papers, ~169M tokens →
  ≈ $1.70 per full Qwen3-Embedding-8B pass on DeepInfra; 4.0 GB float32 at
  1024 dims.
- Facet *derivation* (normalised method families, bottleneck classes,
  deployment regime) is not an extraction field and is deliberately out of
  this contract; it belongs to the Landscape Builder stage.
