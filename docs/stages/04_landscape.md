# Stage 4 — `landscape`

Design reference: architecture §3 How the Research Landscape Is Built (3.1–3.4) and §4 Landscape Output.

## Aim

Describe what the retrieved corpus looks like: normalised concepts grouped by
facet, evidence-backed relationships between concepts, aggregated findings /
limitations / assumptions, contradictions, and under-explored regimes. It says
nothing about what is unresolved; that is `reasoning`.

## Inputs

Entry point: `build_landscape(topic, contexts, *, extraction_provider=None, normalize_provider=None, aggregation_provider=None, model=None, concurrency=None) -> Landscape`
— `landscape/builder.py:124`. Called from `research/pipeline.py:157` with
`c.query`, `state['contexts'].papers`, and `c.provider` for all three provider
slots, `c.model`, `c.concurrency`.

| Argument | Contract |
|---|---|
| `topic: str` | The user query. |
| `contexts: dict[str, PaperContext]` | Output of stage 3; only `.card` is used for extraction, `matched_records` for quote resolution. |
| providers / `model` | Default from `LANDSCAPE_EXTRACTION_*`, `LANDSCAPE_NORMALIZE_*`, `LANDSCAPE_AGGREGATION_*` env vars when the pipeline passes `None`. |

Embedding models for similarity: `LANDSCAPE_NORMALIZE_EMBED_PROVIDER/MODEL`,
`LANDSCAPE_AGGREGATION_EMBED_PROVIDER/MODEL` (default provider `local`).
Constant `SIMILARITY_THRESHOLD = 0.80` in both `landscape/normalize.py` and
`landscape/aggregation.py`.

LLM request/response models: `RawExtraction{mentions: RawMention[], triples: RawTriple[]}`
(`landscape/extraction.py:30-48`), `EquivalenceJudgment{pairs}` (`normalize.py:46`),
`ClusterSummary{statement}` (`aggregation.py:50`).

## Logic

`build_landscape` runs two branches concurrently (`builder.py:135`) and
assembles the result; any exception in either branch is re-raised.

Branch A — graph (`_build_graph`, `builder.py:158`):

1. `extract_from_papers` (`extraction.py:150`): one LLM call per paper with `SYSTEM_PROMPT` (`extraction.py:55`) → `RawExtraction`. `_validate_evidence` (`extraction.py:104`) drops any mention/triple whose `evidence_quote` is not a substring of the card text or whose facet/relation is outside `VALID_FACETS` / `VALID_RELATIONS` (`landscape/schemas.py:16-25`). A failed extraction is logged and the paper is skipped; it is not fatal.
2. `_resolve_facets` (`builder.py:76`): first-seen facet per concept text; triple endpoints default to `method`.
3. `normalize_concepts` (`normalize.py:141`): exact-label dedup → embed → same-facet pairs with cosine ≥ 0.80 → one LLM `EquivalenceJudgment` call over all candidate pairs (`SYSTEM_PROMPT`, `normalize.py:55`) → union-find merge; `concept_id = _slugify(shortest label)`. A missing pair verdict raises `NormalizationError`.
4. Groups: `concept_id → {paper_ids}` including endpoint-only concepts (`builder.py:198`).
5. Relationships merged on `(source, relation, target)`; evidence quotes resolved to `matched_records` and source locations by `resolve_evidence` (`landscape/evidence.py:18`); unmatched quotes leave empty evidence, never fabricated.
6. `_find_contradictions` (`builder.py:89`): same `(source, target)` with `CONTRADICTS` or an opposed pair in `OPPOSED_RELATION_PAIRS`.
7. `_find_underexplored` (`builder.py:108`): `mention_count < 2` and `max_relation_support < 2`.

Branch B — aggregation (`aggregate_findings`, `aggregation.py:131`):

1. Collect statements by kind (`finding`, `limitation`, `assumption`) from cards.
2. Cluster same-kind statements at cosine ≥ 0.80.
3. Clusters of size 1 keep the original text with no LLM call; larger clusters get one `ClusterSummary` call (`SYSTEM_PROMPT`, `aggregation.py:55`). Parse failure raises `AggregationError`.
4. Limitations carry their `limitation_sources` through.

Assembly: `Landscape(...)`; `Landscape.identify_and_validate`
(`landscape/schemas.py:167`) assigns `item_id`s with `stable_id` and raises
on duplicate ids, unknown supporting papers, or inconsistent contradiction
sides.

## Outputs

`Landscape` — `landscape/schemas.py:152` (`schema_version='landscape_builder_v1'`).

| Field | Contract |
|---|---|
| `topic` | Query. |
| `paper_ids: list[str]` | Sorted corpus inventory; the universe for all supporting-paper checks downstream. |
| `groups: list[ConceptEntry]` | `concept_id`, `facet`, `label`, `paper_ids` (≥1). |
| `relationships: list[Relationship]` | `item_id`, `source`, `relation`, `target`, `supporting_papers`, `evidence: EvidenceReference[]`, `evidence_record_ids`, `source_locations`. |
| `aggregated_findings` / `recurring_limitations` / `common_assumptions: list[AggregatedItem]` | `item_id`, `statement`, `supporting_papers`, `limitation_sources`. |
| `contradictions: list[Contradiction]` | `item_id`, `a`, `b` (full relationships). |
| `underexplored_regimes: list[UnderexploredConcept]` | `item_id`, `concept_id`, `facet`, `label`, `mention_count`, `max_relation_support`. |

`item_id = f"{kind}_" + sha256(json.dumps(parts, sort_keys=True))[:24]`
(`schemas.py:50`); parts are the statement + sorted supporting papers for
aggregated items, `(source, relation, target)` for relationships.

Persisted as `out/landscape.json`.

## What later stages rely on

- `reasoning` schedules one thread per relationship, aggregated item, under-explored regime and contradiction, and cites their `item_id`s in `refines`.
- `opportunities` and `directions` validate that every referenced item exists in this landscape and that `landscape_ref.sha256 = digest(landscape)`.

## Implementation vs design

Follows §3.1–3.4 closely. Differences: facet vocabulary is fixed in code
(`VALID_FACETS`), concept equivalence is decided by a single batched LLM call
rather than pairwise calls, and no minimum-support filter is applied to
aggregated items (single-paper clusters are kept).
