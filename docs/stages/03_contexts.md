# Stage 3 — `contexts`

Design reference: architecture §1 Paper Representation and §2.4 Paper and Evidence Loading.

## Aim

Load `paper.json` for every retrieved paper and project it into a compact
`PaperCard` for the landscape builder. No extraction is performed; the stage
only reads existing artifacts, records their hashes, and surfaces which fields
were missing.

## Inputs

There is no standalone function; the stage body is the `'contexts'` branch of
`StageRunner.execute` (`research/pipeline.py:146`).

| Input | From | Contract |
|---|---|---|
| `state['retrieval'].papers` | stage 2 | Each `paper_id` must be loadable. |
| `self.store: PaperStore` | `StageRunner.__init__` (`pipeline.py:108`) | `PaperStore(config.root, store_factory=ArtifactStore if allow_s3 and S3_ARTIFACTS_URL else None)` — `reasoning/evidence.py:67`. |
| `PipelineConfig.root` (default `markdown/`) | | Artifact root: `<root>/<paper_id>/paper.json` and `<root>/<paper_id>/NN.md` pages. |
| `PipelineConfig.allow_s3` + env `S3_ARTIFACTS_URL` | | Enables lazy download of missing artifacts (`ingestion/s3store.py`). |

The artifact schema read here is `PaperAnalysis`
(`extraction/research_extract.py:882`, `schema_version: paper_analysis_v1`).
Fields consumed by the projection: `paper_metadata.{title,venue,year}`,
`research_problem.*`, `method.{high_level_idea,components[].name}`,
`key_results[].finding`, `interesting_findings[].finding`,
`limitations.{author_stated,inferred}[].limitation` (+ `source_locations`),
`future_work.*`, `claims_and_evidence[].claim`.

## Logic

1. For each retrieved paper, `PaperStore.get(paper_id)` (`reasoning/evidence.py:95`): cache → local file → optional S3 fetch; returns `LoadedPaper(status='loaded'|'missing'|'invalid', paper: dict, sha256, error)`. With `validate=True` the JSON is validated as `PaperAnalysis`.
2. Any status other than `loaded` → `ValueError('Cannot load retrieved paper ...')`. There is no soft skip; a missing or invalid artifact fails the stage (`pipeline.py:150`).
3. `_project(paper_id, paper)` (`landscape/paper_context.py:87`): pure field projection into `PaperCard`; builds `LimitationSource` rows with `value_path='/limitations/<section>/<i>'` and origin `author_stated` / `model_inferred`; collects `missing_fields` for absent sections.
4. Build `PaperContext(paper_id, card, missing_fields, matched_records=[r.model_dump() for r in paper.records])` and record `artifact_sha256[paper_id] = loaded.sha256`.

No LLM calls.

## Outputs

`Contexts` — `research/pipeline.py:68`.

| Field | Contract |
|---|---|
| `papers: dict[str, PaperContext]` | Keyed by `paper_id`; same key set as retrieval. |
| `artifact_sha256: dict[str, str]` | sha256 of the raw `paper.json` bytes per paper. |

`PaperContext` (`landscape/paper_context.py:76`, frozen): `paper_id`, `card:
PaperCard`, `matched_records: list[dict]`, `missing_fields: list[str]`.

`PaperCard` (`paper_context.py:42`): `title`, `venue`, `year`, `problem`,
`research_questions`, `hypotheses`, `method_summary`, `method_components`,
`findings`, `interesting_findings`, `limitations`, `inferred_limitations`,
`limitation_sources: list[LimitationSource]`, `assumptions`, `future_work`,
`claims`.

Persisted as `out/contexts.json`.

## What later stages rely on

- `landscape` consumes `contexts.papers` directly.
- `reasoning`, `opportunities`, `directions`, `novelty_*` reload artifacts through the same `PaperStore` and compare hashes against `artifact_sha256` / `paper_artifact_hashes`; a changed `paper.json` is reported as `stale_evidence`.
- `missing_fields` is informational and never hidden.

## Implementation vs design

§2.4 anticipated loading "paper and evidence"; the implementation loads only
`paper.json` here. Page markdown (`NN.md`) is loaded lazily by later stages
when they need to quote or verify text.
