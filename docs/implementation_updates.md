# Implementation updates

This is an append-only record of implementation changes. Each entry names the
modules, explains the approach and identifies earlier behavior it supersedes.
Existing design and validation documents remain historical records; dated
follow-ups in those documents point here when their defaults or guidance change.

## 2026-10-03 — Full-context Opportunity Miner, token allowances and validation

### Context and approach

Architecture section 6 asks what appears unresolved given reviewed cross-paper
findings. Fixed-size source batches could separate related evidence before the
model had a chance to consider it together. The miner now places every reviewed
source in one proposal context by default, with relevant landscape context and
namespaced evidence references. This improves the opportunity to compare related
findings; it does not guarantee synthesis, semantic deduplication or completeness.

The execution sequence is:

1. Validate exact landscape/reasoning lineage and collect page-reviewed sources.
2. Supply the complete reviewed-source set to one proposal call. Apply the input
   budget to the complete prompt, including schema and context. If it exceeds
   the allowance, record `input_budget`; do not silently omit or split sources.
3. Validate candidate reference ownership and resolve original artifacts/pages.
4. Review independent candidates concurrently. Required proposal-to-review
   dependencies remain sequential; file reads run through `asyncio.to_thread`.
5. Preserve the existing v2 promotion rules: substantive factual corrections
   block acceptance; each selected paper receives a reviewed support/context
   role; accepted outputs retain original evidence and page references.

Application allowances were raised to 500000 tokens as requested. Provider
output limits are applied separately when constructing API requests. Bigger
allowances do not remove candidate, page, paper, per-record or API-attempt caps,
and they do not enlarge a model's actual context window.

### Production modules changed

| Module / symbol | Earlier implementation | Change and reason |
| --- | --- | --- |
| `opportunities/miner.py` — `Settings.batch_size`, `OpportunityMiner.run()`, `main()` | Default batches of 12 sources; zero disallowed | Default 0 means all reviewed sources together. Positive sizes opt into batching. CLI help explains the meaning. Empty input still yields the existing diagnostic. |
| `opportunities/miner.py` — `Settings.max_input_tokens` | Fixed default 40000 | Read `OPPORTUNITY_MAX_INPUT_TOKENS`, fallback 500000. Complete-prompt guard is retained. |
| `opportunities/miner.py` — output settings | Proposal fallback 12000; review fallback 12000, potentially overridden by reasoner environment | Proposal and review fallbacks are 500000. Review precedence remains opportunity-specific environment, then reasoner review environment, then fallback. |
| `reasoning/cross_paper.py` — `Budgets` | Draft input 24000, draft output 6000, review input 40000, review-output fallback 3000 | Each field now reads its corresponding `REASON_MAX_*_TOKENS` environment variable with fallback 500000. Explicit constructor/CLI budgets still take precedence. |
| `reasoning/cross_paper.py` — `Budgets.max_bundle_chars` | Aggregate character cap 12000 | Read `REASON_MAX_BUNDLE_CHARS`, fallback 2000000. The reasoner passes this to its evidence bundle builder. This is a character allowance, not an exact token guarantee; per-record selection and the complete-prompt check still apply. |
| `llm_client/client.py` — `_provider_config()` | Output allowance but no configurable provider cap | Read optional `{PROVIDER}_OUTPUT_TOKEN_LIMIT`; reject nonpositive configured limits. The setting is configuration, not automatic discovery of provider capacity. |
| `llm_client/client.py` — new `_cap_output()`, `_chat_params()`, `_response_params()` | Outgoing output maximum taken from configuration/call arguments | Cap the outgoing `max_tokens` or `max_output_tokens` after constructing request parameters. Both sync and async callers use these builders. Smaller explicit allowances are preserved; providers without a configured cap get no implicit cap. |

`opportunities/schemas.py` and the landscape-building algorithm were not changed
in this update. The existing `opportunities_v2` contract and correction/role
semantics remain as described in [review fixes](opportunity_review_fixes.md).
The Cross-Paper Reasoner still selects and reviews its own tasks; this update
does not make all its tasks one call or change how the landscape is constructed.

### Configuration changes

`.env.example` documents the active settings; the private `.env` uses the same
allowances and is excluded from Git. Unrelated provider settings retain their
previous values.

| Setting | Active value |
| --- | ---: |
| `DEEPSEEK_MAX_TOKENS` | 500000 |
| `DEEPSEEK_OUTPUT_TOKEN_LIMIT` | 393216 |
| `REASON_MAX_DRAFT_INPUT_TOKENS` | 500000 |
| `REASON_MAX_DRAFT_OUTPUT_TOKENS` | 500000 |
| `REASON_MAX_REVIEW_INPUT_TOKENS` | 500000 |
| `REASON_MAX_REVIEW_OUTPUT_TOKENS` | 500000 |
| `REASON_MAX_BUNDLE_CHARS` | 2000000 |
| `OPPORTUNITY_MAX_INPUT_TOKENS` | 500000 |
| `OPPORTUNITY_MAX_OUTPUT_TOKENS` | 500000 |
| `OPPORTUNITY_REVIEW_MAX_OUTPUT_TOKENS` | 500000 |

The DeepSeek example model is `deepseek-flash`. The configured 393216 output cap
bounds the 500000 application output request. Input and generated output also
share the provider context window. No claim is made that this validation filled
the 500k input allowance.

### Validation modules added or changed

| File | Change / purpose |
| --- | --- |
| `tests/test_opportunity_miner.py` | Added a 25-source check showing the default sends every source in one proposal even with `max_batches=1`. |
| `tests/test_token_allowances.py` (new) | Tests chat/Responses caps, smaller explicit limits, absence of implicit caps, invalid caps, environment defaults and explicit overrides. |
| `tests/reasoning/test_scheduling_and_budget.py` | Updated the review-output fallback expectation to 500000; retains override checks. |
| `tools/opportunity_validation_cases.py` — `fixture()` | Added optional custom-case input; existing callers and category fixtures keep their behavior. |
| `tools/validate_opportunity_boundaries.py` (new) | Six paired-boundary cases, each reviewed twice; three real miner runs using saved upstream inputs; four simultaneous calls and a 48-attempt default cap. Saves inputs, raw calls, verdicts, outputs and summary; exits nonzero on failed expectations/audits. |
| `tools/validate_opportunity_boundaries.py` — `audit()` | Independently checks saved v2 evidence ownership, original hashes/pointers/locations, supporting/context roles and page citations. Full-run checks also verify lineage and complete source delivery. |
| `tests/test_opportunity_boundary_audit.py` (new) | Checks fixture lineage and demonstrates audit failure for changed original pages or broken support-role partitions. Stub responses test plumbing, not model judgment. |

The live controls distinguish valid inferred questions from invented benefits,
scoped comparisons from false same-condition contradictions, and unavailable
reported results from claims that experiments never happened. Appendix extraction
is not required; the earlier suggestion to follow appendix references is
superseded by the dated clarification in [validation history](opportunity_validation.md).

### Evidence and limits

- Offline: **159 tests and 13 subtests passed**; one existing FAISS/NumPy warning.
- Live: **34 calls**, peak concurrency four, no provider errors or truncations.
- Synthetic expectations: **12/12 passed**.
- Three real-paper miner runs: **15 proposed, 12 accepted, three withheld**.
- All **18 accepted objects** across synthetic and real runs passed the saved
  evidence audit: **63 evidence references and 70 page references**.

See [boundary validation](opportunity_boundary_validation.md) for exact outcomes,
repairs, reproduction commands and artifact paths. These runs reuse reviewed
upstream artifacts; they do not rerun extraction, landscape construction or
cross-paper reasoning. Model review remains fallible, accepted questions may
overlap, and scientific value and novelty belong to later architecture stages.
Service deployment/load behavior and a near-limit input load were not measured.
