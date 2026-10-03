# Landscape → reasoning integration

This implements the focused integration agreed for `feature/opportunity_miner`.
The reference is `research_path_generator_architecture_refined.md`, sections
2.5 and 3–6: retain evidence references, build a descriptive landscape, review
cross-paper implications, then mine opportunities.

## Shared boundary

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

## Reasoner behavior

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

## Validation and scope

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

## Live validation

A real-provider smoke test now exercises the library pipeline on three shared
corpus papers. See [results and limitations](live_landscape_reasoning_smoke.md):
the schema handoff and provenance checks passed; default review output limits
truncated the first run, and a focused follow-up required manual qualification.

## Concurrency

Independent graph construction and statement aggregation execute concurrently.
Cluster summarization uses concurrent async calls bounded by the existing
client semaphore; blocking embedding and similarity operations run off the
event loop. Failed siblings are drained before client cleanup. Reasoner tasks
already run concurrently with an atomic attempt budget; each task retains the
required draft → review dependency. The broader live run observed four calls
in flight and no truncation at the configured review output allowance.
