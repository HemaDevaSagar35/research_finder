# Shared LLM client (`llm_client/`)

This is the module document for the existing shared sync/async provider client
and the output-limit change made during Opportunity Miner development.

## Existing behavior

`llm_client/client.py` loads project environment configuration and uses shared
parameter builders for chat-completions and Responses requests. Sync and async
clients use these builders; the async client supplies concurrency/retry controls.
Per-call `ChatResult` metadata permits truncation checks during concurrent work.
This client existed before the Opportunity Miner and was reused by the pipeline.

## 2026-10-03 — `62de9cd` output-limit implementation

The requested application generation allowance is 500000 tokens. That does not
override provider capacity. Previously the configured or explicitly supplied
output allowance was forwarded without a configurable provider output ceiling.

| Existing/new symbol in `llm_client/client.py` | Change |
| --- | --- |
| `_provider_config()` | Read optional `{PROVIDER}_OUTPUT_TOKEN_LIMIT`; reject nonpositive values. This is explicit configuration, not automatic model-capacity discovery. |
| New `_cap_output()` | Cap a provided output maximum to the configured limit, preserving smaller requests. No configured limit means no implicit cap. |
| `_chat_params()` | Apply the cap to `max_tokens` after merging request parameters. |
| `_response_params()` | Apply the cap to `max_output_tokens` after merging request parameters. |

Both sync and async clients inherit the behavior. Application settings retain
the requested allowance; the outgoing request carries the capped value.
`tests/test_token_allowances.py` verifies both builders, smaller explicit limits,
unconfigured providers, invalid caps and configuration/explicit overrides.

## Configuration recorded with the change

`.env.example` documents the settings; the private `.env` is excluded from Git.
The active DeepSeek example model is `deepseek-flash`. Other provider blocks
retain their previous values. The module-specific defaults/precedence are
explained in the [reasoner](cross_paper_reasoner.md#implementation-history) and
[miner](opportunity_miner.md#implementation-history) documents.

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


## Validation boundaries

The whole-repository suite passed 159 tests plus 13 subtests. The miner boundary
run completed 34 live DeepSeek calls without truncations or provider errors;
that tests the active chat path. Responses request construction has offline test
coverage, not a live DeepSeek Responses compatibility claim. These runs did not
fill the 500k input allowance or measure deployment-scale load.
