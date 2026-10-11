# Landscape Builder (`landscape/`)

This is the single module document for the contributor's existing Landscape
Builder, our subsequent integration/attribution changes, and their validation.
The contributor's implementation arrived through merge `1e3a634` from
`feature/landscape-builder`; we did not build this module from scratch in the
Opportunity Miner work. The historical design below is preserved. The dated
[implementation history](#implementation-history) names our edits and explains
which earlier behavior they changed.

## Landscape → reasoning integration

This implements the focused integration agreed for `feature/opportunity_miner`.
The reference is `research_path_generator_architecture_refined.md`, sections
2.5 and 3–6: retain evidence references, build a descriptive landscape, review
cross-paper implications, then mine opportunities.

### Shared boundary

`landscape.schemas.Landscape` is the canonical public input for the reasoner.
Keep the builder's groups, aggregated_findings, recurring_limitations,
common_assumptions, relationships, contradictions (a/b), and underexplored_regimes.
Extraction, normalization, clustering, aggregation, and contradiction detection
remain unchanged. `schema_version=landscape_builder_v1` distinguishes this shape
from the historical reasoner-only `landscape_v1` fixture format.

Add code-assigned `item_id` values to statements, relationships, contradictions,
and sparse concepts. IDs are deterministic for the same content regardless of
list ordering; they are not promises of identity across changed LLM statements.
Explicit IDs survive serialization. Validate uniqueness and all paper/concept
references at the boundary.

Relationships gain `evidence`: a list of `{paper_id, record_id,
source_locations}`. Each entry preserves the record's own locations and paper.
The existing pooled `evidence_record_ids` and `source_locations` remain for
compatibility, but consumers must use `evidence` for attribution. Missing matches
produce no fabricated reference. Supporting papers may have no resolved record.
Old pooled locations cannot safely be assigned to papers retrospectively.

### Reasoner behavior

The reasoner schedules directly from this contract. Relationships use their
endpoint groups for bounded paper expansion. Aggregated statements use only
explicit supporting papers: no inferred concept associations are added.
Contradiction sides use the existing a/b relationships, retain balanced paper
allocation, and are reviewed as potential conflicts, not proven contradictions.
Underexplored entries retain support counts; their statements explicitly refer
to the selected landscape corpus, never global novelty.

Accepted results' `refines` values resolve to shared landscape item IDs, and
`landscape_ref` hashes the actual input, not a lossy transformed copy. Existing
drafting, evidence resolution, page review, budgets, and acceptance remain intact.
Historical reasoner fixtures remain supported explicitly as `LegacyLandscape`;
they are not the builder's target contract.

### Validation and scope

Offline integration tests must run the real builder orchestration with mocked
model-dependent extraction/normalization/aggregation, serialize its output, and
feed that exact output to the real reasoner with synthetic papers and FakeChat.
Check per-paper/per-record page attribution, stable IDs, reference validation,
all task categories, no fabricated concept expansion, and accepted-result links.
Run the existing regression suite as well.

A small live run is a separate quality check: passing fake-model tests does not
establish scientific support quality. Do not change credentials or write shared
research data as part of this integration. Rich coverage, contradiction
restructuring, and statement-to-concept inference are deferred. Opportunity
Miner implementation is outside this change.

### Live validation

A real-provider smoke test now exercises the library pipeline on three shared
corpus papers. See [results and limitations](cross_paper_reasoner.md#integration-live-validation):
the schema handoff and provenance checks passed; default review output limits
truncated the first run, and a focused follow-up required manual qualification.

### Concurrency

Independent graph construction and statement aggregation execute concurrently.
Cluster summarization uses concurrent async calls bounded by the existing
client semaphore; blocking embedding and similarity operations run off the
event loop. Failed siblings are drained before client cleanup. Reasoner tasks
already run concurrently with an atomic attempt budget; each task retains the
required draft → review dependency. The broader live run observed four calls
in flight and no truncation at the configured review output allowance.

### Limitation attribution

`PaperCard.limitation_sources` preserves each nonempty limitation from its
original `paper.json` array: paper ID, `author_stated` or `model_inferred`, exact
record JSON pointer, source statement, and source locations. Array positions
are captured before filtering; they are not reconstructed from compact cards.
The path/category pairing is validated in code. A legacy card can retain its
known category with an unknown path and empty locations; no pointer is invented.

`AggregatedItem.limitation_sources` retains every contributing source in a
limitation cluster. Supporting paper membership is validated. A mixed cluster
keeps mixed sources; it has no aggregate-level author-stated classification.
Clustering still uses the original statement text and kind. Independent summary
calls remain asynchronous. Summarization prompts now distinguish extraction
interpretations from author attribution.

The reasoner's draft receives these landscape sources as context, not new
citable evidence IDs. Actual evidence-bundle and output evidence origins are
derived in code from their `paper.json` paths, and origin labels accompany
candidate evidence into the page review. Review remains mandatory; source labels
are classifications made by the original extractor, not proof of what authors
said. Neither a label nor a wording rule guarantees model behavior: the reviewer
must still reject unsupported attribution. Prompt version is now
`cross_paper_prompt_v2_attribution`.

Old landscape files have no newly recovered source pointers until rebuilt from
paper artifacts. They remain loadable with an empty `limitation_sources` list;
the reasoner still derives origins for fresh evidence loaded from paper.json.

Attribution regression and live rerun results:
[limitation attribution validation](cross_paper_reasoner.md#attribution-validation).


<a id="implementation-history"></a>

## Implementation history — existing contributor module, modified during integration

### Baseline: contributor implementation merged at `1e3a634`

The existing builder loaded paper contexts, extracted typed relations, normalized
concepts, grouped them, assembled relationships, aggregated findings/limitations/
assumptions, and detected graph contradictions and sparsely supported concepts.
Its flat `Landscape` collections were already implemented. We retained those
collections and the underlying extraction, clustering and graph rules.

### `365f351` — shared contract, attributable evidence and concurrent work

The reasoner originally had its own incompatible landscape input shape. The
approach was to make the builder's existing public shape canonical, add stable
references and provenance, and adapt the reasoner to it.

| Existing file / symbol edited | Previous behavior → change | Why |
| --- | --- | --- |
| `landscape/schemas.py` — `Landscape`, item models, new `stable_id()` / `EvidenceReference` | Added `landscape_builder_v1`, deterministic item IDs, per-paper/per-record evidence, and paper/concept/item reference validation. Contradiction sides must match actual relationships. | Downstream results can refer to exact builder items and resolve evidence ownership. |
| `landscape/evidence.py` — new `resolve_references()` | Pooled record IDs/page locations → additionally retain each matched record's paper and locations together. Existing pooled fields remain for compatibility. | Avoid attributing pooled pages to the wrong paper; unmatched text does not create evidence. |
| `landscape/builder.py` — `build_landscape()`, extracted `_build_graph()` | Graph work followed by aggregation → run independent graph and aggregation branches concurrently, draining both before propagating errors. Relationship assembly now carries resolved evidence objects. | Preserve the builder's computation while overlapping independent work and retaining provenance. |
| `landscape/aggregation.py` — `aggregate_findings()` | Sequential cluster summaries → concurrent summaries through the client's semaphore, preserving result order and draining failures. Embedding/clustering run with `asyncio.to_thread`. | Avoid unnecessary sequential model calls and event-loop blocking. |
| `landscape/normalize.py` — `normalize_concepts()` | Blocking embedding/similarity operations → offload with `asyncio.to_thread`. | Allow independent asynchronous work to proceed. |

IDs are deterministic for unchanged content, not persistent identities across
changed model wording. Explicit IDs survive serialization. These edits did not
replace how concepts are extracted, merged or clustered, or how graph conflicts
are detected. They changed orchestration and the contract around those results.

### `708f47a` — preserve author-stated versus inferred limitation sources

The earlier aggregation flattened both kinds of limitation into plain strings.
Paper membership survived, but downstream code could lose whether a limitation
was inferred or attributed to the authors. That changed as follows:

| Existing file / symbol edited | Change |
| --- | --- |
| `landscape/paper_context.py` — `PaperCard`, `_project()` | Added `limitation_sources`: paper ID, extraction origin, exact original array-index JSON pointer, statement and source locations. Capture indices before filtering empty strings. |
| `landscape/schemas.py` — new `LimitationSource`, `limitation_origin()`, `AggregatedItem` | Validate origin/path consistency and source-paper membership. Aggregates retain individual contributing sources. |
| `landscape/aggregation.py` — `RawStatement`, `_collect_statements()`, `_summarize_cluster()` | Carry provenance through collection and clustering; pass origin labels to summaries and retain mixed origins separately. Legacy cards preserve category without inventing pointers. Prompts qualify inferred claims. |

The clustering still uses the same statement text and kind. The summarization
prompt changed attribution handling; this is more than a documentation change.
An `author_stated` extraction label is not proof of an author's statement:
original-page review remains the reasoner's/miner's responsibility. Old saved
landscapes must be rebuilt to recover source pointers absent from their data.

### Validation and current boundaries

`tests/test_landscape_reasoning_integration.py` exercises the real builder
orchestration with mocked model steps, serialization into the reasoner, reference
validation, task categories and exact lineage. `tests/test_landscape_concurrency.py`
checks overlapping branches/summaries, ordering and cleanup.
`tests/test_limitation_attribution.py` checks source indices, mixed attribution,
legacy paths and transmission into reasoning/page review.

Historical live integration and attribution results are retained in the
[Cross-Paper Reasoner document](cross_paper_reasoner.md#integration-live-validation)
and its [attribution validation section](cross_paper_reasoner.md#attribution-validation).
The latest entire-repository suite passes 159 tests plus 13 subtests; that count
is not a separate landscape-only test count. No production landscape code was
changed by `847b910`, `54082b1` or `62de9cd`, which implemented and refined the
miner and adjusted reasoning/client allowances.

The earlier statement that Opportunity Miner implementation was outside the
integration change describes `365f351` only. The new downstream miner was later
implemented at `847b910`; see [its module document](opportunity_miner.md).
