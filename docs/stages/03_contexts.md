# Stage 3 — `contexts`

Design reference: architecture §1 Paper Representation and §2.4 Paper and Evidence Loading.

## Aim

Every paper in the corpus was analysed offline into a structured
`paper.json` (problem, method, results, limitations, future work, claims — see
`extraction/research_extract.py`). This stage loads that file for each
retrieved paper and reduces it to a compact *card* that the landscape builder
can read for fifteen papers at once without blowing its context budget. It
also pins the exact bytes it read: later stages re-verify every quote and
every evidence pointer against these hashes, so a paper that is re-extracted
mid-run cannot silently change what a direction was built on.

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

## Logic

**Step 1 — fetch the artifact.** For each retrieved paper the store looks in
its in-memory cache, then on local disk, then (if configured) downloads from
S3 into the local tree. The raw bytes are hashed and the JSON is validated
against `PaperAnalysis`, so a corrupt or outdated artifact is detected here
rather than when a later stage tries to quote from it. (`PaperStore.get`,
`reasoning/evidence.py:95`.)

**Why a missing artifact fails the run.** The retrieval index and the artifact
store are supposed to be two views of the same corpus. If a paper is
retrievable but has no `paper.json`, the two have drifted, and continuing
would mean the landscape silently describes fewer papers than the user was
told were retrieved. The stage therefore raises
`Cannot load retrieved paper ...` instead of skipping. (`pipeline.py:150`.)

**Step 2 — project to a card.** `paper.json` is large (per-claim evidence,
per-result tables, source locations for everything). The landscape builder
needs only the *statements*: what problem, which method components, which
findings, which limitations and assumptions, what future work the authors
suggest. The projection pulls those strings out and nothing else. Two choices
matter downstream:

- *Limitations keep their provenance.* Each limitation is recorded with a JSON
  pointer back into `paper.json` (`/limitations/author_stated/0`) and a flag
  for whether the authors stated it or the extractor inferred it. Later
  stages treat author-stated and model-inferred limitations differently when
  they decide what counts as evidence for a gap.
- *Missing is reported, not hidden.* If a section is absent, its path goes
  into `missing_fields`. A card with no limitations because the paper has
  none looks different from a card with no limitations because extraction
  failed, and the landscape builder can see which it is.

(`_project`, `landscape/paper_context.py:87`.)

**Step 3 — bundle with retrieval provenance.** The card is wrapped in a
`PaperContext` together with the records that made the paper match in stage 2
(`matched_records`). The landscape builder uses those records to anchor the
model's evidence quotes to concrete index records with page/section locations.
The artifact hash is stored alongside under `artifact_sha256`.

No model calls are made in this stage.

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

- `landscape` reads the cards; everything after it goes back to the full `paper.json` through the same `PaperStore`.
- `reasoning`, `opportunities`, `directions` and the novelty stages compare the hash of whatever they reload against the hash recorded when the evidence was first cited; a mismatch is reported as `stale_evidence` and the dependent item is withheld.

## Implementation vs design

§2.4 anticipated loading "paper and evidence"; the implementation loads only
`paper.json` here. Page markdown (`NN.md`) is loaded lazily by later stages
when they need to quote or verify text.
