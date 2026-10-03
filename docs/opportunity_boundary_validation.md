# Opportunity Miner boundary and full-context validation

Run on 2026-10-03 using the configured DeepSeek `deepseek-flash` endpoint.
The focused checks passed: 12 synthetic reviews and three real-corpus miner
runs, across 34 API attempts. No provider errors or truncated responses occurred.
Peak concurrent API requests: four. The full offline suite passed 159 tests and
13 subtests (one existing FAISS/NumPy deprecation warning).

## Scope and setup

This validates architecture section 6: whether motivating evidence supports an
unresolved question, with accurate conditions, attribution and scope. It does
not judge scientific value, predict future discoveries, or certify novelty.
Direction generation, novelty retrieval and the research critic remain separate
stages with their own responsibilities.

Synthetic cases use clearly marked fictional pages and hand-authored upstream
reviewed observations. Each case received two independent live reviews. The real
runs reuse saved landscapes and reviewed reasoning from the earlier MoE,
KV-cache and RAG validations; retrieval, extraction, landscape construction and
cross-paper reasoning were not rerun here.

The miner used current defaults: one proposal context containing all reviewed
sources, 500,000-token input/output allowances, and the configured provider output
cap of 393,216. This was a functional run, not a 500k-token load test. The existing
five-candidate proposal cap remains independent of the token allowances.

## Evidence-boundary results

| Control | Expected behavior | Observed |
| --- | --- | --- |
| Compression changes entropy; separate caching work relates entropy to latency | Accept the inferred joint-effect question without claiming a demonstrated joint benefit | Accepted 2/2 |
| Same evidence, but candidate claims a proven combined benefit | Withhold unsupported benefit | Requires correction 2/2 |
| Opposite latency effects under nominally matched settings, unknown seed/warmup | Accept a scoped discrepancy question | Accepted 2/2 |
| Different GPUs, workloads and batch sizes; ask which differences explain the effects | Accept a question that preserves those differences | Accepted 2/2 |
| Same differing settings falsely described as identical experiments | Withhold false contradiction | Requires correction 2/2 |
| Reviewed pages explicitly say tail measurements exist elsewhere; candidate says never measured | Withhold missing-evaluation claim | Requires correction 2/2 |

The last control does not require appendix ingestion. Missing extracted material
is not proof of missing experiments. Reviewer suggestions for narrower wording
were retained as diagnostics; the rejected candidate was not silently rewritten
or promoted. A page-availability gap alone does not establish a scientific gap.

## Real-paper runs

| Corpus | Reviewed source objects in the single proposal context | Proposed | Accepted | Withheld | API attempts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| MoE inference, 3 papers | 1 finding containing multiple evidence records | 5 | 4 | 1 | 8 |
| KV-cache compression, 3 papers | 12 | 5 | 4 | 1 | 6 |
| RAG grounding, 3 papers | 8 | 5 | 4 | 1 | 8 |

All three saved results use `opportunities_v2`. Each has one accepted opportunity
with reviewed support from multiple papers; the other three are single-paper
opportunities. Paper roles describe support for the specific candidate, not a
count of all cited or reviewed papers.

Concrete withheld candidates:

- **MoE:** the candidate described a separate four-H100 machine; the page says
  TP-4 uses four GPUs of the eight-H100 machine. Review also required tighter
  wording about untested regimes and the additional hardware results mentioned
  on the page.
- **KV-cache:** an apparent disagreement about which paper reported a fused
  kernel was already resolved by the original AttentionPack pages. A landscape
  attribution error did not become a research contradiction.
- **RAG:** a candidate attributed a “weakest instrument” statement to the paper,
  but the supplied pages did not support that attribution. Even though its core
  question was plausible, the candidate was withheld as written.

Four bounded format/reference repairs succeeded: two proposals included an extra
`title` field, and two acceptance reviews initially omitted a required paper-role
assessment. These were not retries that erased substantive correction vetoes.
Actual simultaneous reviews peaked at three for MoE and four each for KV and RAG.

## Saved-output audit

All 18 accepted objects (six synthetic positive reviews plus 12 real opportunities)
passed the audit: 63 evidence references and 70 reviewed-page references, counted
per output rather than as unique documents. Checks covered:

- JSON round-trip through the v2 schema and exact landscape/reasoning hashes.
- Every upstream reviewed source reaching each real proposal call.
- Candidate/source/evidence ownership and landscape refinement IDs.
- Original artifact hashes, value/provenance pointers, source values and locations.
- Original page hashes and paper ownership of assessment citations.
- Supporting/context-only role partitions and derived support labels.
- Preservation of `literature_novelty: not_assessed`.

Offline audit tests also demonstrated rejection of changed original pages and
incorrect paper-role partitions. Hash/reference correctness does not establish
that every model judgment is scientifically correct. The sample is small and
model review remains variable. Related accepted MoE questions still overlap in
scope; full-context input does not guarantee semantic deduplication or exhaustive
opportunity coverage. The five-candidate cap also means these counts are not a
measure of all possible unresolved questions.

## Reproduction and artifacts

```bash
uv run python -m tools.validate_opportunity_boundaries --out /path/to/new/run
uv run pytest -q
```

The harness limits concurrent requests to four and total attempts to 48 by
default; it exits nonzero on failed expectations or audit errors. Its default
input paths refer to the private saved runs used here. Shared paper data remain
read-only.

Artifacts: `/home/hema/research_runs/opportunity_boundaries_live/` contains
`summary.json`, call timing and raw request/response files, synthetic inputs and
verdicts, and each real corpus's input and `opportunities.json` files. Per-miner
usage deltas overlap during concurrent execution; the process aggregate in
`summary.json` is authoritative: 331,897 prompt tokens, 81,408 cached prompt
tokens, 167,812 completion tokens, including 145,498 reasoning tokens.

No production miner logic was changed in this validation pass. Added files are
the reusable boundary harness, audit tests and this report; the synthetic fixture
helper now accepts a custom case. The associated token-budget and
full-context implementation changes are documented module by module in
[Implementation updates](implementation_updates.md).
