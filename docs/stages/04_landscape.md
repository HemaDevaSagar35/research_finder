# Stage 4 — `landscape`

Design reference: architecture §3 How the Research Landscape Is Built (3.1–3.4) and §4 Landscape Output.

## Aim

Before anyone can say what is *missing* from an area, something has to say
what is *there*. This stage builds that map from the fifteen cards: which
concepts the papers work with (grouped by facet — problem, method, dataset,
metric, ...), which concepts the papers connect and how ("X improves Y",
"X contradicts Y"), which findings, limitations and assumptions recur across
papers, where two papers disagree, and which concepts are mentioned but barely
studied. It is descriptive only. The questions "is this gap real?" and "is this
unresolved?" belong to `reasoning` and `opportunities`.

## Inputs

Entry point: `build_landscape(topic, contexts, *, extraction_provider=None, normalize_provider=None, aggregation_provider=None, model=None, concurrency=None) -> Landscape`
— `landscape/builder.py:124`. Called from `research/pipeline.py:157` with
`c.query`, `state['contexts'].papers`, and `c.provider` for all three provider
slots, `c.model`, `c.concurrency`.

| Argument | Contract |
|---|---|
| `topic: str` | The user query. |
| `contexts: dict[str, PaperContext]` | Output of stage 3; `.card` is what the model reads, `matched_records` anchors quotes. |
| providers / `model` | Default from `LANDSCAPE_EXTRACTION_*`, `LANDSCAPE_NORMALIZE_*`, `LANDSCAPE_AGGREGATION_*` env vars when the pipeline passes `None`. |

Embedding models for similarity: `LANDSCAPE_NORMALIZE_EMBED_PROVIDER/MODEL`,
`LANDSCAPE_AGGREGATION_EMBED_PROVIDER/MODEL` (default provider `local`).
Constant `SIMILARITY_THRESHOLD = 0.80` in both `landscape/normalize.py` and
`landscape/aggregation.py`.

## Logic

The build has two independent halves that run concurrently and are joined at
the end: a **concept graph** (what the papers talk about and how those things
relate) and **aggregated statements** (what the papers say, merged across
papers). (`build_landscape`, `builder.py:124`.)

### Half A — the concept graph

**A1. Pull concepts and relations out of each card.** One model call per paper
reads the card and returns two lists: *mentions* (a concept name, its facet,
and the exact sentence from the card that mentions it) and *triples* (concept
→ relation → concept, again with a quote). The quote is the whole point: the
code checks that every quote is literally a substring of the card and that
the facet and relation come from the fixed vocabularies; anything that fails
is dropped. This makes it impossible for the model to invent a concept the
paper does not mention. A paper whose extraction call fails is logged and
left out of the graph — one bad paper should not kill the stage.
(`extract_from_papers`, `landscape/extraction.py:150`; `_validate_evidence`,
`:104`; vocabularies `VALID_FACETS`, `VALID_RELATIONS`, `landscape/schemas.py:16-25`.)

**A2. Decide which concept names mean the same thing.** Fifteen papers will
call the same idea "RLHF", "reinforcement learning from human feedback" and
"preference-based fine-tuning". If those stay separate the graph fragments
and nothing recurs. Normalisation first embeds every distinct label and
finds pairs within the same facet whose cosine similarity is ≥ 0.80 — the
cheap filter — then asks the model, in one call, to say yes/no for each
candidate pair. Pairs the model confirms are merged with union-find; the
merged concept takes the shortest label as its id. The embedding step keeps
the model's job small; the model step prevents false merges between things
that are merely lexically close. (`normalize_concepts`,
`landscape/normalize.py:141`; `_candidate_pairs`, `:95`.)

**A3. Build groups and relationships on the normalised ids.** Each concept id
becomes a group with the set of papers that mention it. Each triple becomes
a relationship; triples from different papers with the same
`(source, relation, target)` are merged, so a relationship's
`supporting_papers` tells you how many papers assert it. For every
relationship the code tries to resolve the quote back to a retrieval record
and page/section location so the claim can be checked later; if the quote
does not match any record, the evidence list is left empty rather than
guessed. (`_build_graph`, `builder.py:158`; `resolve_evidence`,
`landscape/evidence.py:18`.)

**A4. Derive contradictions and under-explored concepts.** These are pure
graph queries, not model judgments. A *contradiction* is two relationships on
the same `(source, target)` where one is `CONTRADICTS` or the two relations
are in the opposed-pairs table (e.g. `IMPROVES` vs `DEGRADES`). An
*under-explored* concept is one mentioned by fewer than two papers and
appearing in no relationship supported by two or more papers — a thing the
area names but does not study. (`_find_contradictions`, `builder.py:89`;
`_find_underexplored`, `:108`.)

### Half B — aggregated statements

**B1. Collect free-text statements by kind.** Findings, limitations and
assumptions are taken from the cards as sentences, each tagged with its
paper and (for limitations) its provenance pointer.

**B2. Cluster and merge.** Statements of the same kind are embedded and
clustered at the same 0.80 threshold. A cluster of one keeps its original
sentence verbatim — no model call, no paraphrase risk. A cluster of several
statements is sent to the model once, asking for a single sentence that is
true of all of them; the result becomes an `AggregatedItem` whose
`supporting_papers` lists every paper in the cluster. A recurring limitation
is therefore "the same limitation, said by N papers", which is the raw
material for an opportunity later. (`aggregate_findings`,
`landscape/aggregation.py:131`; `_cluster`, `:68`; `_summarize_cluster`, `:113`.)

### Join and validate

The two halves are assembled into one `Landscape`. Validation assigns every
item a content-addressed id (`stable_id` over the statement and supporting
papers, so the same item gets the same id across runs) and raises if an item
cites a paper outside the corpus, if a contradiction's sides are not real
relationships, or if ids collide. (`Landscape.identify_and_validate`,
`landscape/schemas.py:167`; `stable_id`, `:50`.)

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

Persisted as `out/landscape.json`.

## What later stages rely on

- `reasoning` opens one investigation ("thread") per relationship, aggregated item, under-explored concept and contradiction, and records which landscape `item_id` each conclusion refines.
- `opportunities` and `directions` check that every item they reference exists here and that `landscape_ref.sha256 = digest(landscape)`; the landscape is the fixed frame everything later is measured against.

## Implementation vs design

Follows §3.1–3.4 closely. Differences: facet vocabulary is fixed in code
(`VALID_FACETS`), concept equivalence is decided by a single batched model
call rather than pairwise calls, and no minimum-support filter is applied to
aggregated items (single-paper clusters are kept so that nothing a paper said
disappears from the map).
