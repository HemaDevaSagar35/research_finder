# Cross-Paper Reasoner (`reasoning/`)

Component 8 of the query-time pipeline
(`research_path_generator_components_refined.md`). It sits between the Landscape
Builder (7) and the Opportunity Miner (9) and answers one question:

> What do these papers collectively imply?

Status: **implemented, tested offline, and smoke-tested with a live provider**
on three local corpus papers. See [live results](cross_paper_reasoner.md#integration-live-validation)
for review truncation, the focused follow-up, and manual quality limitations.
The S3 path was not exercised by that run.

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

## Contracts (shared input: `landscape/schemas.py`; reasoning: `reasoning/schemas.py`)

**Landscape (input)** — the shared `landscape.schemas.Landscape`, emitted
by the builder as `landscape_builder_v1`. It retains `groups` (concept IDs and
labels), separate findings/limitations/assumptions, relationships, contradictions
(`a`/`b`), and underexplored counts. Each scheduled object has an `item_id`;
relationship evidence is attributed per paper and record. Statements without
concept associations use only their explicit supporting papers.

The historical reasoner-only `landscape_v1` is accepted as `LegacyLandscape`
for existing fixtures. New integrations use the builder contract. The input
inventory and all paper/concept/item references are validated before model calls.
See [integration design](landscape_builder.md) for provenance,
compatibility, and the offline builder-to-reasoner test.

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
| Live provider / real corpus run | Small local-corpus smoke test completed; see [results](cross_paper_reasoner.md#integration-live-validation). S3 integration and broader quality evaluation remain unverified. |

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
| Landscape missing inventory or referencing unknown ids (legacy items also require `group_ids`) | Rejected by validation before any call |

## First real run (what to do next)

1. Put `REASON_PROVIDER` / `REASON_MODEL` (reasoning model) and optionally
   `REASON_REVIEW_MODEL` in `.env`; set `S3_ARTIFACTS_URL` or sync `markdown/`.
2. Obtain a Landscape from the Landscape Builder that validates with
   `reasoning.schemas --landscape`. Builder output now passes directly to the
   reasoner; the fixture generator remains an offline legacy-format demo.
3. Start small: `--max-threads 3 --max-calls 12`. Read every accepted item against
   its `review_sources` pages. Record citation-validity rate, review acceptance rate,
   tokens/calls per thread from `coverage` and `usage`.
4. Only then consider a cheaper `REASON_REVIEW_MODEL` (needs a quality comparison,
   not just token accounting).

### Shared local artifacts

Given a saved builder output, validate and run it without writing shared data:

```bash
uv run python -m reasoning.schemas --landscape landscape.json
uv run python -m reasoning.cross_paper --landscape landscape.json \
  --root /srv/research_finder/markdown --no-s3 \
  --max-threads 3 --max-calls 12 --out reasoning_out/topic.json
```

`--index-dir /srv/research_finder/index` optionally adds retrieval hints. This
command uses real model calls; the offline integration test uses FakeChat.

### Review output allowance

Set `REASON_MAX_REVIEW_OUTPUT_TOKENS` in `.env` to configure the review response
allowance (default 3,000 when unset). `--max-review-output-tokens` overrides it.
This explicit reasoner budget takes precedence over provider-wide
`DEEPSEEK_MAX_TOKENS`. It is an output allowance, not an input/context limit;
the provider still enforces its model's supported maximum. Draft output and
input/page budgets remain separate.

### Author attribution versus extraction inference

Limitation evidence now carries `origin` derived from its extraction path:
`author_stated` or `model_inferred`. Other evidence has `origin: null`.
The landscape also preserves original limitation statements and per-paper
source pointers through aggregation. Mixed sources remain distinct. Drafts
must not present inferred missing evaluations as explicit author admissions,
and reviewers must check both the claim and its attribution against the pages.
See [attribution design](landscape_builder.md#limitation-attribution).


## 2026-10-03 implementation update — supersedes the allowances above

In `reasoning/cross_paper.py`, `Budgets` now reads four input/output allowances
from `REASON_MAX_DRAFT_INPUT_TOKENS`, `REASON_MAX_DRAFT_OUTPUT_TOKENS`,
`REASON_MAX_REVIEW_INPUT_TOKENS`, and `REASON_MAX_REVIEW_OUTPUT_TOKENS`, each with
a 500000-token fallback. This supersedes the 3000-token review-output fallback
in the earlier section. Explicit CLI/library values still take precedence.

`REASON_MAX_BUNDLE_CHARS` now configures the aggregate bundle cap, with a
2000000-character fallback instead of 12000. Characters are not an exact token
measure; the complete prompt token check still applies. Per-record, paper,
page and call selection controls remain separate. The reasoner's algorithm and
schemas did not change in this update.

`llm_client/client.py` now caps the outgoing output allowance using the optional
provider-specific `*_OUTPUT_TOKEN_LIMIT`. The active DeepSeek configuration uses
393216 while the application allowance is 500000. Historical validation reports
retain their original settings. See [module implementation changes](cross_paper_reasoner.md#implementation-history)
for before/after values, precedence and validation coverage.


<a id="implementation-history"></a>

## Implementation history — changes to the existing Cross-Paper Reasoner

The reasoner already existed in `feat/cross-paper-reasoner`, merged into main
through PR #3 at `8edaeaa`, before the contributor's builder was merged at
`1e3a634`. Its evidence store/bundles, draft → citation resolution → original-page
review, findings/observations/tensions, diagnostics, concurrent tasks and attempt
budgets were existing work. The Opportunity Miner work modified that module;
it did not create it for the first time.

### `365f351` — consume the contributor's builder contract

| Existing file / symbol edited | Previous behavior → change |
| --- | --- |
| `reasoning/schemas.py` — `Landscape`, `LegacyLandscape`, `validate_landscape()`, `load_landscape()` | A separate reasoner-only `landscape_v1` model → import canonical `landscape.schemas.Landscape`; retain the earlier schema explicitly as `LegacyLandscape` for fixtures/files. Revalidate canonical nested references at the boundary. |
| `reasoning/cross_paper.py` — `schedule_threads()` | Schedule from the old input shape → directly handle builder relationships, aggregated findings, recurring limitations, assumptions, sparse concepts and a/b contradictions. |
| `reasoning/cross_paper.py` — `_schedule_legacy_threads()` | Preserve the prior scheduler for historical inputs instead of coercing builder output into a lossy copy. |
| `reasoning/cross_paper.py` — `run()`, `_assemble()` | Accept the shared contract and preserve exact input hashing; result `refines` points to builder-assigned item IDs. |
| `reasoning/cross_paper.py` — `Budgets` | Make review-output allowance configurable through `REASON_MAX_REVIEW_OUTPUT_TOKENS` after live truncation exposed the fixed budget's limits. |
| `reasoning/fixtures.py` and reasoner/integration tests | Use explicit legacy fixtures and test both formats, stable references, task scheduling and override behavior. |

Relationships expand through their endpoint groups; aggregated statements use
only explicitly supporting papers, without invented concept associations.
Contradiction paper allocation remains balanced between sides and the draft
receives a potential conflict to investigate. Sparse concept counts describe
only the selected corpus, not a literature-wide gap. Existing evidence resolution
and page-review acceptance remain mandatory.

### `708f47a` — carry attribution through drafting, review and accepted evidence

| Existing file / symbol edited | Change and purpose |
| --- | --- |
| `reasoning/cross_paper.py` — `Thread`, `schedule_threads()`, `draft_messages()` | Carry the landscape's per-source `limitation_sources` for selected papers into draft context. These are context, not additional citable evidence IDs. |
| `reasoning/cross_paper.py` — draft/review prompts and evidence assembly | Distinguish inference from author attribution, require page verification even for author-stated labels, include origin in review payloads, and derive accepted evidence origins from original record paths. Prompt version becomes `cross_paper_prompt_v2_attribution`. |
| `reasoning/evidence.py` — `bundle()` | Derive limitation origin in code from the original `paper.json` value path when constructing bundle entries. |
| `reasoning/schemas.py` — `BundleItem`, `Evidence` | Add optional `origin`; non-limitation evidence keeps `null`. |

This corrects attribution loss between the builder and the reasoner. It does
not turn an extractor's category into verified testimony or remove the need
for the original-page review. Mixed-source limitations remain mixed.

### `62de9cd` — increase configurable allowances

In `reasoning/cross_paper.py`, `Budgets` changes draft input 24000, draft output
6000, review input 40000 and review-output fallback 3000 to environment-backed
fallbacks of 500000 each. The preceding allowance update lists the exact keys.
`max_bundle_chars` changes from 12000 to `REASON_MAX_BUNDLE_CHARS`, fallback
2000000. Explicit constructor/CLI values retain precedence. Per-record, paper,
page and call limits remain separate; the complete prompt token guard still
runs after evidence selection. The shared [LLM client](llm_client.md) enforces
the configured provider output cap on outgoing requests.

`tests/reasoning/test_scheduling_and_budget.py` covers the changed review fallback
and overrides; `tests/test_token_allowances.py` covers the new environment/default
and explicit-setting behavior. There was no new scheduling algorithm in this
budget update. Full-context proposal batching belongs to the downstream miner.

### Validation record

The original integration, its failed/truncated runs, subsequent async run and
attribution rerun are preserved below. The tests cover traceability and review
behavior; they do not establish universal scientific correctness. The latest
whole-repository suite passes 159 tests plus 13 subtests. Later miner runs reused
saved reasoner outputs, so their success is not a fresh reasoner evaluation.


<a id="integration-live-validation"></a>

## Live landscape → reasoning smoke test

Run on 2026-10-03 on `feature/opportunity_miner`, using the shared-contract
integration described in [the design note](landscape_builder.md).
No shared artifacts, credentials, or provider defaults were changed.

### Setup

- Topic: `efficient MoE inference`.
- Two planned queries; three retrieved papers; local hybrid index at
  `/srv/research_finder/index`.
- Local `paper.json` and Markdown at `/srv/research_finder/markdown`.
- Chat provider returned model name `deepseek-flash`; corpus query embeddings
  use the index's DeepInfra Qwen configuration. Landscape normalization and
  aggregation use their existing local embedding defaults.
- The run harness calls `build_landscape(topic, contexts)` with PaperCards
  projected from local artifacts. The builder CLI's S3 loader was not tested
  or changed. This validates library composition using real providers.

Papers: TokenWeave (`b02deed4ad77`), Semantic Parallelism / Sem-MoE
(`432956a1a205`), and Capacity-Aware Inference (`9515cc87b003`). All three
artifacts validated and their PaperCards had no missing fields. All 17 distinct
paper/page pairs cited by retrieved records were present locally.

### Landscape

The actual builder output passed directly into the shared schema and reasoner:

| Collection | Count |
|---|---:|
| Groups | 154 |
| Relationships | 55 |
| Aggregated findings | 27 |
| Recurring limitations | 27 |
| Common assumptions | 14 |
| Contradictions | 0 |
| Underexplored regimes | 151 |

Twelve of 55 relationships have resolved record-level evidence. The resolver
only searches retrieved matching records, so absence of a match is not absence
of support in the full paper. Paper support remains explicit; locations were
not fabricated. Sparse-concept counts are relative to these three papers.

### Initial bounded run

Limits: 3 threads, 12 calls, existing 6,000 draft / 3,000 review output tokens.

- 11 calls: 3 draft, 3 review, 5 repairs; 6 truncated responses.
- 49 emitted evidence citations resolved; all 28 requested page instances
  were fetched and supplied (the same page may occur in several threads).
- Zero accepted results; six candidates received `review_invalid` because
  support-review output was truncated, including after repair.
- The scheduler lists relationships first, in builder order. The first three
  were all TokenWeave tasks, so only one paper was actually inspected.
- The remaining 271 of 274 tasks were correctly budget-skipped. A completed
  thread status alone does not imply accepted output or successful review.

This exposed runtime sizing and sampling issues, not a schema handoff failure.

### Focused cross-paper follow-up

Preserved the first run. Selected the one aggregated limitation supported by
all three papers, keeping its original item ID, full paper inventory and groups.
Saved this selection as a separate landscape; output hashes that actual input.
Other collections were empty for this targeted diagnostic run.

Limits: 1 thread, 4 calls, 12,000 draft and review output tokens, 16 review pages.

- Two calls (draft + review), zero truncation, all three papers inspected.
- 24/24 emitted evidence citations resolved.
- 17 requested pages fetched, 16 supplied; one omitted by the page cap.
- One accepted cross-paper finding, with low confidence: the selected papers
  evaluate a limited set of models and benchmarks, including few MoE models.
- One candidate withheld because it required the omitted page.
- One rejected as overbroad: claiming larger-scale MoE models remained untested
  overlooked TokenWeave's Mixtral-8x22B evaluation.

#### Manual audit

All 11 accepted evidence references resolved both value and provenance JSON
pointers; their artifact hashes matched. All 11 accepted review-source page
hashes matched. Read evaluation material on Sem-MoE pages 8–9, Capacity-Aware
Inference pages 6/8/10, and TokenWeave pages 9/11.

Those pages establish the named evaluation settings and support a cautious
coverage observation. They do not establish that the scope is inadequate or
that a novel opportunity exists. The phrase "small set" lacks an explicit
comparison baseline. The automated review's counts are also incomplete:
Capacity-Aware Inference page 10 includes an additional Qwen3-MoE evaluation,
beyond the four principal language models listed in its review note. Do not
promote this finding directly into a validated opportunity.

### Usage and limits of this test

Chat accounting across both runs: 26 calls, 145,335 input tokens, 121,809 output
tokens, of which 103,083 were reported as reasoning tokens. This includes query
planning and landscape construction; it excludes embedding API usage. No
currency estimate is asserted. The 12-call bound applied to the initial
reasoner stage, not the builder; the follow-up had its own four-call bound.

The larger review-output allowance worked for this one follow-up. Defaults
remain unchanged; a single run does not justify a global budget increase.
Before Opportunity Miner quality evaluation:

1. Use provider-appropriate reasoning/output budgets, checking truncation.
2. Select representative multi-paper tasks; a prefix cap can select one paper.
3. Prefer specific, scoped observations over generic "limited evaluation" claims.
4. Preserve rejection and omitted-page diagnostics, rather than treating a
   process exit code as a quality pass.

### Artifacts and reproduction

Private local artifacts are under `/home/hema/research_runs/live_integration/`:
`query_plan.json`, `retrieval.json`, `inventory.json`, `landscape.json`,
`reasoning.json`, usage snapshots, and `focused/{landscape,reasoning}.json` plus
`focused/provenance_audit.json`. Run harnesses are in `/home/hema/research_runs/`.
They contain no copied API credentials. These absolute paths are machine-local.

To rerun the focused saved landscape (makes paid provider calls):

```bash
uv run python -m reasoning.cross_paper \
  --landscape /home/hema/research_runs/live_integration/focused/landscape.json \
  --root /srv/research_finder/markdown --no-s3 \
  --max-threads 1 --max-calls 4 \
  --max-draft-output-tokens 12000 --max-review-output-tokens 12000 \
  --out /home/hema/research_runs/focused_rerun.json
```

### Broader asynchronous follow-up

A later run reused the saved landscape and selected eight real items: one
relationship and one aggregated finding per paper, the multi-paper limitation,
and one assumption. No supporting-paper links were fabricated to make an item
cross-paper. The saved subset retained all original groups and paper IDs.

- Four concurrent model calls observed at peak (instrumented around API calls).
- Eight tasks completed; 18 calls: eight drafts, eight reviews, two repairs.
- Review output used `.env`'s `REASON_MAX_REVIEW_OUTPUT_TOKENS=128000`;
  draft output was 12,000 for this test. No response was truncated.
- 13 accepted single-paper observations; no accepted cross-paper findings or
  tensions. Four candidates were rejected on review, and one invalid one-paper
  tension was rejected structurally.
- 126/126 emitted citations resolved; all 72 requested page instances supplied.
- Audit verified 80 accepted evidence references (JSON pointers and artifact
  hashes) and 25 unique review-page hashes. Representative source passages were
  also inspected; this was not exhaustive human review of all claims.
- Chat usage: 159,849 input tokens, 92,897 output tokens (79,283 reasoning).

The genuine multi-paper limitation was rejected because it attributed an
inferred limitation to an explicit statement in the original pages. The
reviewer also rejected overbroad model-generalization and unsupported tuning
claims. This run supports functioning validation and useful scoped observations;
it does not establish reliable cross-paper synthesis or validated opportunities.
For example, an accepted routing observation mentions F1 "up to 1.000", while
the source table includes 0.667–1.000 across layers. Downstream consumers must
retain conditions and source scope rather than treat the maximum as universal.

Artifacts: `/home/hema/research_runs/broader_async_validation/` contains the
selected `landscape.json`, `selection.json`, `reasoning.json`, `concurrency.json`,
and `provenance_audit.json`. The harness closes its async provider client.

### Concurrent implementation

Independent landscape work now overlaps: graph extraction/normalization and
statement aggregation run together, and independent cluster summaries run
concurrently through the async client's semaphore. Synchronous embedding and
similarity work is offloaded from the event loop. Results retain input order;
concurrent calls are drained before owned clients close on failure. A thread's
support review still depends on its draft. This dependency is intentionally
sequential. These builder changes were regression-tested offline rather than
spending calls regenerating the saved live landscape.

Validation after these changes: 90 tests and 13 subtests passed, including
concurrency overlap, deterministic result ordering, and error cleanup.


<a id="attribution-validation"></a>

## Limitation attribution: implementation and live validation

### Problem and change

The landscape combined author-stated and extractor-inferred limitations as
plain strings. Paper IDs survived, but attribution and original record paths
did not. A later draft could incorrectly describe an inference as something
an author explicitly acknowledged.

The fix preserves each source's paper ID, extraction origin, exact JSON record
pointer, original statement, and source locations through PaperCard projection
and aggregation. Mixed sources remain separate. The reasoner includes the
source labels in drafting and review; its evidence origin is derived in code
from the actual record path. Author-stated remains an extraction classification,
not independently verified testimony. The review must still inspect the pages.

This change covers limitations. Other evidence kinds, including inferred future
work, retain their existing labels and paths but have `origin: null` in the new
limitation-origin field. Empty legacy source lists are supported; old aggregate
files must be rebuilt to recover exact original source statements and pointers.

### Regression validation

97 tests and 13 subtests passed. New tests cover:

- Original array indices preserved when empty strings are filtered.
- Missing locations remain empty; legacy cards do not invent record paths.
- Origin/path mismatch rejected.
- Mixed author/inferred sources survive aggregation and JSON round-trip.
- Source paper membership validated against supporting papers.
- Attribution reaches the draft, review, and accepted output evidence.
- A reviewer rejection of an explicit-author overclaim remains rejected.

The fake-model regression checks data flow and review enforcement. It cannot
prove that a live model will always preserve attribution correctly.

### Live rerun: 2026-10-03

Reused the same three papers and rebuilt only statement aggregation using the
real provider, with up to four concurrent calls. Extraction, normalization,
retrieval, and the shared index were not rebuilt. The same all-three-paper
limitation cluster was recovered. Its three source records were:

| Paper | Record path | Origin |
|---|---|---|
| Sem-MoE (`432956a1a205`) | `/limitations/inferred/1` | model_inferred |
| Capacity-Aware Inference (`9515cc87b003`) | `/limitations/inferred/0` | model_inferred |
| TokenWeave (`b02deed4ad77`) | `/limitations/inferred/1` | model_inferred |

All source statements and locations matched the original records exactly.
The aggregate was phrased as an apparent evaluation limitation, and retained
these three distinct source references. It was not labeled author-stated.

Reasoning was bounded to one task and four calls, with 12,000 draft output
tokens and the configured 128,000 review output allowance:

- Two calls (draft and review); no truncation.
- All three papers inspected; 12/12 emitted citations resolved.
- Nine requested pages fetched and supplied; none omitted.
- One accepted low-confidence cross-paper finding and one rejected candidate.

The accepted result separated evaluation coverage by paper: model/routing
coverage for Sem-MoE, intra-node tensor-parallel scope for TokenWeave, and
capacity/multimodal coverage for Capacity-Aware Inference. It did not assert
that the authors explicitly admitted these limitations. It retained three
`model_inferred` limitation references alongside other structured evidence.
All six accepted evidence artifact hashes and five accepted review-page hashes
matched the local files.

The other candidate was rejected because its model list was not established
by the particular pages supplied for its citations. This does not establish
that those models are absent from the whole paper: an earlier run's page 8
showed additional models. A rejection is about the candidate's supplied support.

### Interpretation

This rerun shows that attribution survives the pipeline and that the earlier
explicit-author overclaim did not recur in this sample. It does not prove
causality from a single stochastic rerun or establish scientific novelty. The
accepted wording still generalizes from selected evaluation pages; omissions
should remain scoped to reviewed evidence and verified more broadly before
Opportunity Miner promotes them. The low-confidence finding is not a validated
research opportunity.

Artifacts: `/home/hema/research_runs/limitation_attribution_validation/` contains
`aggregation.json`, `landscape.json`, `reasoning.json`, and token usage. The live
harness is `/home/hema/research_runs/run_attribution_validation.py`. No API
credentials are copied into those files.
