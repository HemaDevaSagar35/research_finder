# Stage 1 — `plan`

Design reference: `research_path_generator_architecture_refined.md` §2.1 Query Planning.

## Aim

Expand the user's topic into a small set of complementary literature-search
queries. The stage produces search strings only; the original query is always
kept first. No ranking, retrieval or judgment happens here.

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

1. Validate arguments; `ValueError` on empty query, bad `max_queries`, or both `client` and `provider` (`query_planner.py:73`).
2. One structured call: system prompt `SYSTEM_PROMPT` (`query_planner.py:27`), user content `{"query", "max_queries"}` as JSON, response schema `QueryPlan` via `AsyncLLMClient.structured` (`llm_client/client.py:383`). No repair call, no fallback plan (docstring `query_planner.py:62`).
3. `_parse_plan` (`query_planner.py:39`): parse JSON into `QueryPlan`; reject blank queries (`QueryPlanningError`); rebuild deterministically — original query first, then expansions deduplicated on whitespace-normalised casefold, truncated to `max_queries`.
4. Close the owned client.

Failure mode: invalid JSON or an empty query → `QueryPlanningError`
(subclass of `ValueError`), which fails the run. There is no retry beyond the
SDK's transient retries.

## Outputs

`QueryPlan` — `research/query_planner.py:16` (`extra="forbid"`, strict).

| Field | Contract |
|---|---|
| `queries: list[str]` (min 1) | `queries[0]` is the stripped original query; the rest are unique expansions; length ≤ `max_queries`. |

Persisted as `out/plan.json` in the standard checkpoint wrapper.

## What later stages rely on

- `retrieval` re-validates the list (1–20 unique, non-blank) in `MultiQueryRetriever.retrieve`.
- Nothing downstream reads the plan after retrieval; it is kept for provenance.

## Implementation vs design

Matches §2.1. The design allows "query reformulation"; the implementation
restricts this to additive expansions and never rewrites the original.
