# Broader Opportunity Miner validation

Completed 2026-10-03 against implementation commit `847b910`. Production miner
code, prompts, provider settings, and shared corpus artifacts were unchanged.
This report records failures as well as successful checks.

## Offline checks

`uv run pytest -q`: **135 tests passed, 13 subtests passed**. One existing
FAISS/NumPy deprecation warning remains. Nine new tests in
`tests/test_opportunity_quality_controls.py` cover evidence handling for all eight
categories and retention of contrary original-page context even when a candidate
selects evidence from only one source paper.

The offline tests use stub model decisions. They establish evidence handling,
not scientific validity or LLM judgment quality.

## Live setup

The model returned `deepseek-flash`. Two independently bounded suites ran
concurrently using the configured DeepSeek API. Total: **124 calls** out of the
180-call allowance, zero truncated responses, and no recorded provider errors.

| Suite | Calls | Peak concurrent calls | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| Controlled cases | 42 | 4 | 117517 | 72068 |
| Two fresh corpus pipelines | 82 | 6 | 547736 | 520340 |
| Total | 124 | — | 665253 | 592408 |

Output totals include 521172 reported reasoning tokens. Usage is summed from
isolated process totals, not overlapping component snapshots. Fresh retrieval
used the local lexical index; normalization/aggregation used local embeddings.
Only the configured chat endpoint received model requests.

## Controlled real-model checks

The synthetic documents are explicitly fictional. Their upstream reviewed
observations are hand-authored fixtures, not live Cross-Paper Reasoner results.
This isolates Opportunity Miner proposal and original-page review behavior.

| Category | Hand-authored positive review | Unconstrained generated categories | Accepted generated candidates |
|---|---|---|---:|
| Recurring limitation | Accepted | recurring limitation | 1 |
| Restrictive assumption | Accepted | restrictive assumption | 1 |
| Missing regime | Accepted | missing regime | 2 |
| Contradiction | Accepted | contradiction, missing evaluation | 2 |
| Failure mode | Accepted | recurring limitation, missing evaluation | 2 |
| Missing evaluation | Accepted | missing evaluation | 1 |
| Unresolved tradeoff | Accepted | unresolved tradeoff, missing evaluation | 2 |
| Mechanistic interaction | Accepted | mechanistic interaction, missing evaluation | 2 |

All eight positive review controls passed. The eight unconstrained runs proposed
13 candidates and accepted all 13; 12 cited both papers. They exercised seven
of the eight category labels in generation: the rare-class failure fixture was
classified as recurring limitation/missing evaluation rather than failure mode.
These overlapping categories are not a reliable mutually exclusive taxonomy.
Two proposal repairs removed extra `title` fields; neither involved truncation.

Six synthetic negative controls were all correctly rejected:

- An already-answered median-latency comparison.
- A p99 regression claimed without any tail-latency measurements.
- A false assertion that TP-4 used a separate four-GPU machine.
- A literature-wide absence claim based on two papers.
- A claimed demonstrated joint compression/caching benefit despite no joint test.
- An assertion of identical implementations despite undocumented seed/warmup.

Two additional real-paper probes were correctly rejected: attributing an inferred
SemMoE limitation to an explicit author admission, and claiming its measured
performance improvement remained unanswered.

### Failed real-paper hardware regression: 3 of 3 repetitions

The saved TokenWeave opportunity's scope says “8xH100 and 4xH100 DGX systems.”
The original page 9 says TP-4 uses four GPUs on the **same eight-GPU DGX**.
All three repetitions still accepted the candidate. One review explicitly called
the misstatement a narrowing note “not grounds for rejection.” Thus correct
rejection of a blatant synthetic hardware error did not transfer reliably to
an incidental factual error embedded in an otherwise plausible real question.

The current prompt tells the reviewer to judge the candidate as written, but
this is insufficient. Before downstream generation, review should require a
correction/review cycle or rejection when factual scope needs correction. There
is currently no structured correction requirement or corrected-candidate re-review.

## Fresh real-corpus pipelines

Both runs freshly retrieved three papers, built a full landscape, selected
representative bounded subsets without changing paper support or item IDs,
ran the actual Cross-Paper Reasoner, and mined opportunities. They were not
reruns of the earlier MoE landscape. Full landscapes and selected subsets are
both saved so the sampling boundary is inspectable.

| Result | KV-cache compression | RAG grounding |
|---|---:|---:|
| Papers | 3 | 3 |
| Full landscape groups / relationships | 147 / 50 | 211 / 81 |
| Reasoning tasks | 8 | 8 |
| Tasks involving multiple papers | 4 | 1 |
| Accepted reasoning findings / observations / tensions | 2 / 10 / 0 | 1 / 7 / 0 |
| Reasoning calls | 16 | 17 |
| Opportunity batches | 4 | 3 |
| Proposed opportunities | 12 | 9 |
| Accepted / rejected opportunities | 9 / 3 | 8 / 1 |
| Miner calls (including repairs) | 18 | 12 |
| Outputs labelled multiple-paper support | 2 | 0 |

KV-cache papers: MOMENTKV (`436775470aa1`), STAR-KV (`fc330d65cf4f`), and
AttentionPack (`712e704db09d`). RAG papers: translative/multi-agent Amharic RAG
(`f8f2ce0be4be`), CF-RAG (`4cb7db179a09`), and TRACE (`95525342129f`).

Examples of useful accepted questions include matched-compression comparisons
for STAR-KV; behavior of MOMENTKV outside measured low-logit-spread settings;
CF-RAG component rankings on benchmarks outside its two-dataset ablation; and
whether TRACE stage-separability findings hold with model-inferred rather than
injected gold trace information.
These are automated acceptances within reviewed evidence, not human-certified
research opportunities.

The four rejected opportunities concerned claims already addressed by original
pages or unsupported absence/attribution: confused AttentionPack/STAR-KV speedup
attribution; supposedly missing STAR-KV context/hardware comparisons; a no-fine-
tuning evaluation supposedly absent based on related work alone; and a RAG
joint-metric evaluation falsely claimed absent despite TRACE's table providing it.

### Cross-paper synthesis: one shared opportunity, one contextual second paper

KV `op-000-000` concerns generalization beyond the reviewed 7B–13B evaluations
in both AttentionPack and STAR-KV. This is a shared, scoped two-paper opportunity.
A manual spot-check of AttentionPack page 5 and STAR-KV page 7 confirms the
reported evaluation model sets. It remains an inferred coverage question.

KV `op-000-001` is also labelled `multiple_papers`, but explicitly concerns only
AttentionPack's visual/video task coverage and says it does **not** apply to
STAR-KV. Its STAR-KV evidence has stance `contradicts`: it excludes that paper
from the motivating limitation. The code counts unique cited evidence papers,
which conflates a supporting paper with a contextual/contrary paper. Therefore
**two multiple-paper labels do not mean two shared cross-paper gaps**. RAG's
eight accepted opportunities were all single-paper scoped.

The contract should distinguish cited papers from papers supporting the actual
opportunity, with roles reviewed for the new candidate rather than blindly
inherited from the upstream finding. Merely filtering original evidence stances
would not establish their role relative to the newly proposed question.

## Integrity audit and remaining limitations

The deterministic audit passed for all 41 accepted output objects across both
suites (including the three erroneous negative-control acceptances): 117 evidence
references and 114 reviewed-page references. It checked original artifact/page
hashes, value/provenance pointers, source values/locations, limitation origins,
paper-count labels, and exact input lineage for complete mining results.
References are counted per output, not as unique source pages. Passing this audit
does not resolve the semantic review failures.

Several accepted opportunities concern absences in reviewed pages while those
pages explicitly refer to appendices (for example STAR-KV A.5.4/A.1.6). Scope
notes preserve that limitation, but a stronger verifier should follow relevant
appendix references before promoting a missing-evaluation claim. Similar
Amharic evaluation questions and STAR-KV bit-allocation questions also recur;
semantic deduplication across batches remains absent.

This validation supports functioning concurrent end-to-end execution, original-
page rejection, and at least one real shared opportunity. It does **not** justify
claiming reliable scientific review or exhaustive category/topic coverage.
Address factual corrections and support-role classification before treating
accepted opportunities as trusted input to the Direction Generator.

## Reproduction and artifacts

Paid live harnesses (use new output directories):

```bash
uv run python -m tools.validate_opportunity_controls --out /path/to/new/controls
uv run python -m tools.validate_opportunity_pipeline --out /path/to/new/pipelines
```

`tools/opportunity_validation_cases.py` contains the synthetic fixtures.
The harnesses cap attempts at 60 and 120 respectively, preserve failed results,
and keep the shared data read-only. Private local run artifacts:

- `/home/hema/research_runs/opportunity_controls_live/`: fixture inputs, raw
  model requests/responses, decisions, summary, and call timings.
- `/home/hema/research_runs/opportunity_broad_live/`: fresh retrieval inventories,
  full/selected landscapes, reasoning, opportunities, raw responses and timings.
- `/home/hema/research_runs/opportunity_live_audit.json`: integrity audit results.
- `/home/hema/research_runs/audit_opportunity_live.py`: local audit script.

The earlier three-opportunity smoke run is retained separately in
`opportunity_miner.md`; no saved live results were rewritten to hide failures.

## Fix follow-up

The baseline failures above are preserved. [Review and paper-role fixes](opportunity_review_fixes.md)
now document v2 correction vetoes, reviewed support/context roles, 149 passing
offline tests plus 13 subtests, and 32 focused live calls validating the fixes.
