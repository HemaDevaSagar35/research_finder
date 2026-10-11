# Stage 1 — `plan`

Design reference: `research_path_generator_architecture_refined.md` §2.1 Query Planning.

## Aim

A single user query rarely covers a research area: the same idea is described
with different vocabulary in different sub-communities, and a literal search
for the user's phrasing would miss half of the relevant papers. This stage
asks a model to write a handful of complementary search queries that approach
the topic from different angles, so that the retrieval stage sees the area
rather than one phrasing of it. The stage deliberately does nothing else: no
retrieval, no filtering, no judgment about the topic.

## Inputs

Entry point: `plan_queries(query, *, max_queries=5, client=None, provider=None, model=None) -> QueryPlan`
— `research/query_planner.py:58`. Dispatched from `StageRunner.execute`
(`research/pipeline.py:141`).

| Argument | From | Contract |
|---|---|---|
| `query: str` | `PipelineConfig.query` | Non-empty user topic (stripped before use). |
| `max_queries: int` | `PipelineConfig.max_queries` (default 5, 1–20) | Upper bound on queries *including* the original. |
| `provider`, `model` | `PipelineConfig.provider/model` | Overrides; else `QUERY_PLANNER_PROVIDER` / `QUERY_PLANNER_MODEL`, else `PROVIDER` and the provider's `<P>_MODEL`. |
| `client` | not used by the pipeline | Injected `AsyncLLMClient` for tests; mutually exclusive with `provider`. |

Env read (via `llm_client/client.py`): `<P>_API_KEY`, `<P>_BASE_URL`,
`<P>_MAX_TOKENS`, `<P>_CONTEXT_TOKEN_LIMIT`, `<P>_MIN_OUTPUT_TOKENS`.

## Logic

**What the model is asked.** The system prompt tells the model it is a
literature-search planner: given the topic and a budget, return a JSON list of
search queries that are *complementary* — different vocabulary, adjacent
sub-problems, the method names a practitioner would use — rather than
paraphrases of the same sentence. The model returns a `QueryPlan` object and
nothing else. (`SYSTEM_PROMPT`, `query_planner.py:27`; the call is
`AsyncLLMClient.structured`, which sends the pydantic schema as the response
format.)

**Why the original query is kept first.** Expansions are a bet; the user's
wording is the one query we know is on-topic. The code therefore does not
trust the model to include it — it rebuilds the list itself with the original
first, then appends the model's expansions. This guarantees that even a poor
expansion set cannot make retrieval *worse* than a plain search.
(`_parse_plan`, `query_planner.py:39`.)

**Why duplicates are removed by the code, not the model.** Two queries that
differ only in case or spacing would hit the index twice and double-count the
same papers in the rank fusion of stage 2. The parser normalises whitespace
and case and drops repeats, then cuts the list to `max_queries`.

**Why there is no retry or fallback.** If the model returns invalid JSON or an
empty query, the stage raises `QueryPlanningError` and the run fails. A silent
fallback (for example, "just use the original query") would hide a broken
provider configuration behind a run that looks normal but retrieves less; the
authors chose to fail loudly instead. The only retries are the SDK's transient
network retries.

## Outputs

`QueryPlan` — `research/query_planner.py:16` (`extra="forbid"`, strict).

| Field | Contract |
|---|---|
| `queries: list[str]` (min 1) | `queries[0]` is the stripped original query; the rest are unique expansions; length ≤ `max_queries`. |

Persisted as `out/plan.json` in the standard checkpoint wrapper.

## What later stages rely on

- `retrieval` re-validates the list (1–20 unique, non-blank) and gives every query an equal vote in rank fusion — so the *number* of expansions directly shapes which papers surface.
- Nothing downstream reads the plan after retrieval; it is kept for provenance.

## Implementation vs design

Matches §2.1. The design allows "query reformulation"; the implementation
restricts this to additive expansions and never rewrites the original.
