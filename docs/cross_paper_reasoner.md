# Cross-Paper Reasoner (`reasoning/`)

Component 8 of the query-time pipeline
(`research_path_generator_components_refined.md`). It sits between the Landscape
Builder (7) and the Opportunity Miner (9) and answers one question:

> What do these papers collectively imply?

Status: **implemented and tested offline** (synthetic corpus, fake model). Not yet
run against a live provider or the real S3 corpus.

Design principles carried into the code: the landscape tells the reasoner where
to look, the papers decide what it may conclude; the model only ever emits
statements, kinds, stances and evidence ids, while code owns provenance, copied
values, page identities and acceptance; a citation that exists is not the same
as a claim that is supported, so every accepted item passes a review against
the cited pages; nothing unverified is presented as a finding.

## What it produces

For a Landscape (topic, groups, items, contradictions, relationships) and the
`paper.json` + page markdown behind it, one JSON file (`CrossPaperReasoning`):

| collection | contents | guarantee |
|---|---|---|
| `findings` | cross-paper findings (`confirms / qualifies / overturns / extends`) | ≥2 distinct papers, every citation resolved by code, passed the page review |
| `observations` | single-paper items worth keeping | same checks; no confidence field |
| `tensions` | disagreements the evidence could not resolve, two sides + explanations | both sides non-empty, ≥2 papers, passed the page review |
| `diagnostics` | every candidate or thread that did not make it, with a typed `reason` | nothing is silently dropped |
| `coverage` | papers selected/loaded/missing/invalid/inspected/cited; threads by status; citations; candidates by reason; review pages requested/fetched/supplied/omitted; calls by kind | run is auditable |
| `run`, `usage`, `landscape_ref` | provider, models, prompt/schema versions, budgets, token counter, landscape hash; token usage delta | reproducible |

`verification: "verified"` means *passed the automated support review against the
cited pages*, not scientific correctness. Manual review remains the V1 quality gate.

## Pipeline per thread

```
Landscape ──validate_landscape──► threads  (one per relationship / item / contradiction)
   │  supporting papers first (paper_id asc), then same-group expansion
   │  ordered by (-shared groups, paper_id); one shared cap per thread;
   │  contradiction sides get quotas floor(cap/2) / cap-floor(cap/2)
   ▼
PaperStore.get(paper_id)            local markdown/<id>/paper.json, lazy S3 fallback; sha256; loaded|missing|invalid
   ▼
candidates(paper) → bundle()        complete records with JSON Pointer value_path + provenance_path
   │                                (result rows inherit their experiment's), attached context
   │                                (setup, metric definition, hardware); per-paper allocation,
   │                                30% reserved for boundary items; omissions recorded
   ▼
draft call  (REASON_MODEL)          → ThreadReasoningDraft: statements, kinds, stances, evidence IDS ONLY
   │                                schema + referential validation; one repair round
   ▼
structural resolution (code)        ids → Evidence (paths, locations, values, hash). Unknown id ⇒ that
   │                                candidate → invalid_citation. 0 papers ⇒ no_valid_evidence,
   │                                1 ⇒ observation candidate, ≥2 ⇒ finding candidate. Tensions keep
   │                                two non-empty sides, ≥2 papers. page=None ⇒ unresolved_location.
   ▼
support review  (REASON_REVIEW_MODEL)  pages cited by the candidates, code-owned ids "<paper_id>#p<n>",
   │                                capped by --max-review-pages / --max-review-input-tokens;
   │                                exactly one accept|reject|insufficient per candidate, else the
   │                                call is invalid (one repair). Accept only if every required page
   │                                was supplied.
   ▼
accept                              agreement (per-paper stance, named denominator) + ordered
                                    confidence for findings; review_sources with page hashes
```

Budgets are enforced by an **attempt counter** that reserves a slot before every
request (`reasoning/budget.py`); failed or truncated requests still count. The
client is built with `max_retries=0`. `llm_client.usage` is reported, never used
for enforcement.

## Running

```bash
# offline smoke run on the synthetic corpus (no keys, no S3)
uv run python -m reasoning.fixtures --out /tmp/demo
uv run python -m reasoning.cross_paper --landscape /tmp/demo/landscape.json \
    --root /tmp/demo --chat fake --no-s3 --out /tmp/out.json

# real run (REASON_* in .env; artifacts under markdown/ or S3_ARTIFACTS_URL)
uv run python -m reasoning.cross_paper --landscape landscape.json --out reasoning_out/topic.json \
    [--root markdown] [--index-dir index] [--verify pages|none] [--concurrency 4] \
    [--max-calls 300] [--max-threads 100] [--max-papers-per-thread 12] \
    [--max-draft-input-tokens 24000] [--max-draft-output-tokens 6000] \
    [--max-review-pages 16] [--max-review-input-tokens 40000] [--max-review-output-tokens 3000] \
    [--repair-rounds 1]

uv run python -m reasoning.schemas --check                 # JSON Schemas for adjacent components
uv run python -m reasoning.schemas --landscape L.json      # validate a Landscape Builder output
uv run pytest tests/reasoning                              # 53 tests, ~1 s, no network
```

Exit code 2 = provider account error (401/402/403); partial output is still
written with the remaining threads marked `failed`.

## Contracts (source of truth: `reasoning/schemas.py`)

**Landscape (input)** — `schema_version: "landscape_v1"`, `topic`, `paper_ids`
(complete inventory), `groups[{group_id, facet, concept, aliases, paper_ids}]`,
`items[{item_id, kind, statement, group_ids, supporting[PaperRef]}]`,
`contradictions[{item_id, side_a, side_b}]` (sides are items),
`relationships[{item_id, source_group_id, relation, target_group_id, supporting}]`.
`PaperRef = {paper_id, source_locations[SourceLocation]}` reuses the extraction
schema's `SourceLocation`. `validate_landscape()` rejects unknown ids, papers
outside the inventory, empty `group_ids`. **No adapter reconstructs a missing
inventory** — the Landscape Builder must emit it.

**ThreadReasoningDraft (model → code)** — `outcome ∈ candidates |
insufficient_evidence | comparability_issue`; findings cite `evidence_ids` with a
`stance_by_evidence` map whose keys equal the ids; conditions list evidence ids or
are hypotheses; tensions have exactly two sides. Errors are categorised
`structural` (repair, then fail the thread) vs `citation` (repair once; then only
the affected candidate is rejected).

**SupportReviewResponse (model → code)** — one decision per submitted
`candidate_id`; `page_ids` must be supplied ids. Anything else invalidates the call.

**Evidence (output)** — `evidence_id, paper_id, source_kind: paper_json,
value_path, provenance_path, source_locations, source_value, summary
("<label> — <source_value>", deterministic), stance, artifact_sha256`.

**Confidence** (findings only, ordered, first match): `low` if unknown papers >
half the denominator, or comparability notes on the thread, or any hypothesis
condition, or any contradicting/mixed paper; `medium` if two supporting/qualifying
papers or any qualifying paper; `high` otherwise.

## Deferred items and known gaps

| Item | Status |
|---|---|
| Narrowing redraft after a `reject` with reviewer notes | **Not implemented.** Reject → diagnostic. The review prompt asks the reviewer to decide on the candidate as written. Bounded follow-up. |
| Heading/caption lookup for `page=None` locations (R2) | Deferred as agreed; `unresolved_location` diagnostic. |
| Per-thread cache (R3) | Deferred as agreed. |
| `tiktoken` dependency | **Optional**, not added to `pyproject.toml`: used when importable, else chars/4. Which ran is in `run.token_counter`. Add it if exact estimates matter. |
| Retrieval hints (`--index-dir`) | Implemented as an optional adapter; exercised only for guards (no index in tests). |
| Live provider / real corpus run | Not done: no `.env` on this machine, and a read-only probe of `s3://research-finder-2026/artifacts/` returned AccessDenied for the local AWS identity, so the `paper.json` inventory is still unknown. |

## Tests (`tests/reasoning/`)

53 tests, all offline, driven by `reasoning.fake.FakeChat` (a cooperative fake that
parses the prompt payload) and the synthetic corpus in `reasoning/fixtures.py`.
They cover the failure-mode table below: invalid/unknown citations,
condition ids outside evidence, one-paper observations, zero-paper tensions,
verify-none, missing pages, page caps, review reject/insufficient, unknown or
missing candidate decisions, unknown page ids, truncated output (per-response
finish reason), malformed JSON, failed calls consuming attempts, budget exhaustion
between draft and review, concurrent attempts never exceeding the cap, fatal
provider errors stopping the run, contradiction cap arithmetic (caps 1/3/12,
overlap, redistribution), expansion ordering, agreement/confidence rules, and
output round-trip.

### Behaviour under failure (each row is pinned by a test)

| Case | Behaviour |
|---|---|
| Valid evidence id attached to an invented condition | Structural check passes; the page review rejects → `rejected_on_review` |
| Unknown evidence id, or a condition citing an id outside its finding | Whole candidate → `invalid_citation`; other candidates in the draft proceed |
| Stance keys ≠ evidence ids, tension without two sides | Draft repaired once, then thread `failed` with `draft_invalid` |
| Result row without its own location | Inherits the parent experiment's provenance; result context kept |
| Exactly one contributing paper | Observation candidate, reviewed, no confidence field |
| Tension with one distinct paper or an empty side | `no_valid_evidence`; never reclassified as an observation |
| Explanation citing an id outside the tension's evidence | `invalid_citation` |
| `page=None` / all-null locations on any cited evidence | `unresolved_location` / `unverifiable_location` for that candidate |
| `--verify none` | Every structurally valid candidate → `verification_skipped`; nothing accepted |
| Cited page missing on disk / S3, or cut by `--max-review-pages` | `insufficient_pages`; no partial acceptance |
| Reviewer returns reject / insufficient | `rejected_on_review` / `insufficient_pages` |
| Reviewer omits a candidate, adds an unknown one, or cites an unsupplied page id | Call invalid, repaired once, then all its candidates `review_invalid` |
| Same page number in two papers | Distinct code-owned page ids `<paper_id>#p<n>` |
| Output hits `max_tokens` (per-response `finish_reason`) | Treated as malformed; one repair; only that thread fails |
| Request raises or times out | One attempt consumed; thread `failed`; no usage entry |
| Budget exhausted between draft and review | Candidates `budget_skipped`; nothing accepted |
| Concurrent threads near `--max-calls` | Reservation before each request; total attempts never exceed the cap |
| Provider 401 / 402 / 403 | Run stops (exit 2); partial output; remaining threads `failed` |
| Contradiction cap 1 / 3 / 12, disjoint or overlapping sides, excess supporting papers | Total ≤ cap; quotas sum to the cap; deterministic; both sides re-checked after loading; cap 1 → `insufficient_for_adjudication` |
| One paper with both supporting and contradicting evidence | Paper stance `mixed` → confidence `low`; counts sum to the denominator |
| Thread-level comparability notes | Every finding in the thread → confidence `low` |
| Landscape missing inventory or `group_ids`, or referencing unknown ids | Rejected by validation before any call |

## First real run (what to do next)

1. Put `REASON_PROVIDER` / `REASON_MODEL` (reasoning model) and optionally
   `REASON_REVIEW_MODEL` in `.env`; set `S3_ARTIFACTS_URL` or sync `markdown/`.
2. Obtain a Landscape from the Landscape Builder that validates with
   `reasoning.schemas --landscape`. Until then, adapt `reasoning/fixtures.py`'s
   `demo_landscape` to real `paper_id`s.
3. Start small: `--max-threads 3 --max-calls 12`. Read every accepted item against
   its `review_sources` pages. Record citation-validity rate, review acceptance rate,
   tokens/calls per thread from `coverage` and `usage`.
4. Only then consider a cheaper `REASON_REVIEW_MODEL` (needs a quality comparison,
   not just token accounting).
