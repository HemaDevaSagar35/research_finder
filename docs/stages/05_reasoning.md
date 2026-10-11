# Stage 5 — `reasoning`

Design reference: architecture §5 Cross-Paper Reasoning.

## Aim

The landscape says "six papers connect X and Y". This stage asks the harder
question for each such item: what do those papers, read together, actually
establish? The answers come in three kinds — a **finding** (two or more
papers support the same statement), an **observation** (only one paper
supports it, so it is not yet a cross-paper fact) and a **tension** (papers
disagree and the disagreement has not been resolved). Every accepted
statement must be traceable to specific evidence inside `paper.json` and must
survive a second, independent model call that checks it against the original
page text. Anything that fails is kept as a typed diagnostic rather than
dropped, so the next stage can see what was *not* established and why.

## Inputs

Entry point: `CrossPaperReasoner(store, chat, *, budgets, provider, draft_model, review_model, verify='pages', concurrency=4, index_dir=None, bundle_budget=None).run(land) -> CrossPaperReasoning`
— constructor `reasoning/cross_paper.py:370`, `run` at `:416`. Pipeline wiring
(`research/pipeline.py:160`): a dedicated `AsyncLLMClient(c.provider or
REASON_PROVIDER, concurrency=c.concurrency, max_retries=0)` whose
`chat_result` is injected; `budgets=c.reasoning`; `draft_model=c.model`;
`review_model=c.review_model`.

| Input | Contract |
|---|---|
| `land: Landscape` | Stage 4 output, validated by `validate_landscape` (`reasoning/schemas.py:165`). |
| `store: PaperStore` | Loads `paper.json` and page markdown for evidence and review. |
| `budgets: Budgets` (`cross_paper.py:70`) | `max_calls` (None), `max_threads` 100, `max_papers_per_thread` 12, `max_bundle_chars` (`REASON_MAX_BUNDLE_CHARS`, 2 000 000), `max_draft_input_tokens` / `max_draft_output_tokens` (`REASON_MAX_DRAFT_*`, 500 000), `max_review_pages` 16, `max_review_input_tokens` / `max_review_output_tokens` (`REASON_MAX_REVIEW_*`, 500 000), `repair_rounds` 1. |
| provider / models | `REASON_PROVIDER`, `REASON_MODEL`, `REASON_REVIEW_MODEL` as fallbacks. |

`PROMPT_VERSION = 'cross_paper_prompt_v2_attribution'` (`cross_paper.py:61`).

## Logic

### Framing: threads, and why the code picks the evidence

Each landscape item becomes a **thread**: a question ("what do these papers
say about *X improves Y*?") plus the list of papers that should be consulted.
The model never gets to choose its own evidence. The code walks each paper's
`paper.json`, turns every relevant field (a result, a limitation, a claim with
its evidence) into a candidate evidence item with a JSON pointer to where it
lives, and hands the model a numbered bundle. The model can only *cite
numbers from that bundle*. This is what makes the output auditable: every
claim the model makes points at a concrete field in a concrete paper, and
the code — not the model — later resolves what that field says and where on
the page it came from.

### Step 1 — schedule threads

One thread per relationship, aggregated finding / limitation / assumption,
under-explored concept and contradiction. Papers for a thread are the item's
supporting papers first, then other papers that share concept groups with
it, capped at `max_papers_per_thread`. Contradictions are scheduled with two
*sides* so the model is asked to adjudicate between them; if either side has
fewer than two loadable papers the thread is marked
`insufficient_for_adjudication` up front — the code will not ask a model to
settle a dispute with one paper per side. Threads beyond `max_threads` are
recorded as `budget_skipped` rather than silently omitted. (`schedule_threads`,
`cross_paper.py:151`; `_ordered`, `:107`; `allocate_sides`, `:123`.)

### Step 2 — build the evidence bundle

For each thread's papers the code extracts candidate evidence from
`paper.json` and selects a bundle under a character budget, giving each paper
a fair share so one long paper cannot crowd the others out. Items that did
not fit are listed explicitly as *omitted*. That list matters: the model is
told that an item not shown to it is not evidence of agreement, and the
prompt forbids treating silence as support. (`candidates`,
`reasoning/evidence.py:236`; `bundle`, `:403`.)

### Step 3 — draft

The model receives the thread question, the paper titles and the numbered
bundle and is asked to propose candidate statements: for each, the statement,
the conditions under which it holds, which bundle items support it, and (for
tensions) which items sit on each side. If the response is structurally
invalid or cites an item that is not in the bundle, the code sends it back
once with the errors (`repair_rounds`). If the bundle is too large for the
input budget, the lowest-ranked items are dropped and moved to the omitted
list. (`_draft`, `cross_paper.py:524`; prompt `DRAFT_SYSTEM`, `:269`;
schema `ThreadReasoningDraft`.)

### Step 4 — resolve, classify, and apply the paper-count rule

The code turns each citation back into an `Evidence` record (paper, pointer,
the actual source value, its page/section locations, the artifact hash at
read time). Then it counts *distinct papers*, not citations: a statement
supported by one paper is an **observation** no matter how many of that
paper's fields it cites; two or more papers make a **finding**; a tension
needs two sides with at least two distinct papers across them. Citations
that cannot be resolved, or evidence whose location is unknown, are recorded
as diagnostics (`invalid_citation`, `unresolved_location`,
`unverifiable_location`, `no_valid_evidence`). (`_resolve`, `cross_paper.py:596`.)

### Step 5 — independent page review

A second model call — with the review model, in a fresh context — receives
the candidate statements and the *actual page text* of the pages the cited
evidence points to, and is asked, per candidate, whether the pages support
the statement as written. This is the check against the extraction layer: a
`paper.json` field may have been mis-extracted, and the only way to know is to
read the page. Pages are ranked by how many citations point to them and
capped at `max_review_pages`; a candidate whose required pages did not fit is
marked `insufficient_pages` rather than approved on partial reading. A review
that returns a decision set different from the candidates it was given, or
cites a page it was not shown, is invalid as a whole. (`_review`,
`cross_paper.py:696`; prompt `REVIEW_SYSTEM`, `:297`; schema
`SupportReviewResponse`.)

### Step 6 — accept

Only candidates the review supports become findings / observations /
tensions. Their `review_sources` (page number and page-file hash) are
written by the code from the pages it actually loaded, never copied from
model text. `agreement` is expressed over the thread's scheduled papers
(e.g. 3 of 7 consulted papers support it), so a finding backed by three
papers out of twelve reads differently from three out of three.
(`_accept`, `cross_paper.py:809`; `_agreement`, `:834`; `_confidence`, `:854`.)

### What a diagnostic means

`DiagnosticReason` (`reasoning/schemas.py:478`) distinguishes: budget limits
(`budget_skipped`), not enough material (`insufficient`,
`insufficient_for_adjudication`, `no_valid_evidence`, `insufficient_pages`),
bad model output (`draft_invalid`, `review_invalid`, `invalid_citation`),
evidence that cannot be located on a page (`unresolved_location`,
`unverifiable_location`), and `rejected_on_review`. A fatal provider error
fails the remaining threads instead of retrying indefinitely.

## Outputs

`CrossPaperReasoning` — `reasoning/schemas.py:518` (`schema_version='cross_paper_v1'`).

| Field | Contract |
|---|---|
| `landscape_ref: {schema_version, sha256}` | Digest of the input landscape; checked by every later stage. |
| `run: RunInfo` | provider, draft/review models, prompt_version, budgets, verify_mode, token_counter, timestamps. |
| `findings: list[CrossPaperFinding]` | `finding_id`, `thread_id`, `refines` (landscape item ids), `finding_kind`, `statement`, `conditions`, `evidence: Evidence[]`, `agreement`, `confidence`, `verification='verified'`, `review_sources`, `review_notes`. |
| `observations: list[Observation]` | Same shape for single-paper statements (`observation_id`). |
| `tensions: list[Tension]` | `tension_id`, `sides`, `candidate_explanations`, `distinct_papers`. |
| `diagnostics: list[CandidateDiagnostic]` | Everything not accepted, with reason. |
| `coverage`, `usage` | Counters and token usage. |

`Evidence` (`schemas.py:385`): `evidence_id`, `paper_id`, `source_kind='paper_json'`,
`value_path`, `provenance_path`, `source_locations`, `source_value`,
`summary`, `stance`, `artifact_sha256`. `ReviewSource` (`:405`): `paper_id`,
`page`, `key_or_path`, `sha256`.

Persisted as `out/reasoning.json`.

## What later stages rely on

- Only items with `verification == 'verified'` and non-empty `review_sources` can be mined for opportunities; the diagnostics are context, never evidence.
- `Evidence.artifact_sha256` and `ReviewSource.sha256` are re-checked every time a later stage reloads the paper or page; a mismatch is `stale_evidence`.
- `refines` ids must resolve to landscape `item_id`s, which is how an opportunity can later say which part of the map it comes from.

## Implementation vs design

§5 is implemented as specified, with two additions not in the design: the
page-level support review as a mandatory second call, and explicit
`OmittedItem` accounting so that evidence not shown to the model is never
read as agreement.
