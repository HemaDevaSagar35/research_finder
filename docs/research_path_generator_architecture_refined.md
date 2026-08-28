# Research Path Generator — Refined V1 Design

## Goal

Use a **2026-only research corpus** to turn a user-selected topic into a ranked set of evidence-grounded future research directions.

The final output is not one hypothesis. It is a portfolio:

```text
Research Direction A
  ├─ H1
  ├─ H2
  └─ H3

Research Direction B
  ├─ H1
  └─ H2
```

Each direction is broader than a hypothesis. Hypotheses are testable claims/questions inside a direction, and one experiment may test several hypotheses.

---

# End-to-End Architecture

```text
                         OFFLINE / INGESTION

                 ┌─────────────────────────┐
                 │      2026 PAPERS        │
                 │ ICML / ICLR / CVPR /    │
                 │ NeurIPS / ACL / ...     │
                 └────────────┬────────────┘
                              │
                              ▼
                      PDF → Markdown
                   Parser / OCR fallback
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
            PaperCard                  Summary
                 │
                 ▼
          Section-aware Chunks
                 │
                 ▼
      Vector + BM25 + Metadata Index


================================================================
                           QUERY TIME
================================================================

                         User Query
                             │
                             ▼
                     Research Planner
                             │
                             ▼
                     INITIAL RAG
              hybrid retrieval + reranking
                             │
                             ▼
              Relevant Papers + PaperCards
                     + source chunks
                             │
                             ▼
                   Landscape Builder
                             │
                             ▼
                   Research Landscape
                             │
                             ▼
                  Cross-Paper Reasoning
         landscape + actual relevant paper evidence
                             │
                             ▼
                    Opportunity Miner
         cross-paper findings + supporting evidence
                             │
                             ▼
                 Candidate Opportunities
                             │
                             ▼
              Future Direction Generator
     opportunity + landscape + actual relevant papers
                             │
                             ▼
       Candidate Directions + Hypotheses + Experiments
                             │
                             ▼
                Novelty Signature Extraction
                             │
                             ▼
                      SECOND RAG
                 over ALL 2026 papers
                             │
                             ▼
                  ~50–100 possible overlaps
                             │
                             ▼
                         Rerank
                             │
                             ▼
                   ~10–20 closest papers
                             │
                             ▼
               Candidate-vs-Paper Comparison
                             │
                             ▼
                Keep / Refine / Reject
                             │
                             ▼
                    Research Critic
                             │
                             ▼
                      Rank / Filter
                             │
                             ▼
                 3–5 Final Directions
```

A core design rule:

> **The landscape tells the system where to look. The actual papers determine what the system is allowed to conclude.**

---

# 1. Paper Representation

Each paper should keep both:

1. **PaperCard** — structured representation for comparison.
2. **Original chunks** — evidence used when making or verifying claims.

Example PaperCard:

```yaml
paper_id:
title:
venue:

problem:
  research_problem:
  bottlenecks:
  task:

method:
  method_family:
  intervention:
  mechanism:

evaluation:
  datasets:
  baselines:
  metrics:
  hardware:
  model_scale:
  deployment_regime:

findings:
  main_results:
  ablations:
  observations:

boundaries:
  assumptions:
  limitations:
  failure_modes:
  tradeoffs:
  authors_future_work:

claims:
  - claim:
    evidence_location:
```

---

# 2. Initial RAG

The initial RAG answers:

> **Which 2026 papers define the research space for this user query?**

Example query:

```text
What are promising directions for efficient MoE inference?
```

The Research Planner expands it into related concepts:

```text
expert routing
expert loading
expert caching
expert offloading
quantization
communication
expert parallelism
scheduling
serving
heterogeneous hardware
```

Then:

```text
Vector Search
    +
BM25 / lexical search
    +
metadata
    ↓
merge
    ↓
rerank
    ↓
relevant papers
```

The downstream unit should be the **paper**, not a disconnected chunk.

---

# 3. How the Research Landscape Is Built

The Landscape Builder is concrete:

```text
Retrieved PaperCards
       ↓
Concept Normalization
       ↓
Faceted Grouping
       ↓
Aggregate Findings
       ↓
Build Relationships
       ↓
Research Landscape
```

## 3.1 Concept Normalization

Different papers may use different phrases for the same concept:

```text
host-to-GPU expert loading
expert swapping
CPU-GPU expert movement
expert parameter transfer
```

These can be normalized to:

```text
expert_transfer
```

Use an LLM to merge only genuinely equivalent concepts. Do not merge concepts merely because they are related.

---

## 3.2 Faceted Grouping

Grouping is **not exclusive clustering**.

A paper can belong to many groups simultaneously:

```text
Problem:      inference latency
Bottleneck:   expert transfer
Method:       expert caching
Mechanism:    temporal expert reuse
Regime:       GPU/CPU offload
```

Useful facets:

```text
problem
bottleneck
method
mechanism
evaluation regime
hardware regime
model scale
optimization target
failure mode
```

---

## 3.3 Aggregate Findings

Aggregation means:

> Merge semantically equivalent paper-level observations into group-level findings while preserving which papers support them.

Example:

```text
P1: caching lowers expert-transfer latency.
P5: caching reduces PCIe transfers.
P12: cache hit rate strongly predicts lower latency.
```

Aggregate:

```yaml
finding:
  Expert caching generally reduces expert-transfer overhead.

supporting_papers:
  - P1
  - P5
  - P12
```

Possible implementation:

```text
collect findings / limitations / assumptions
      ↓
embed statements
      ↓
cluster near-equivalent observations
      ↓
LLM summarizes each cluster
      ↓
retain source evidence underneath
```

---

## 3.4 Build Relationships

Extract evidence-backed relations such as:

```text
SOLVES
REDUCES
INTRODUCES
CAUSES
DEPENDS_ON
REQUIRES
FAILS_UNDER
TRADES_OFF_WITH
HURTS
CONTRADICTS
```

Example:

```text
expert_offloading --REDUCES--> GPU_memory_usage
expert_offloading --INTRODUCES--> expert_transfer_latency
expert_caching --REDUCES--> expert_transfer_latency
expert_caching --DEPENDS_ON--> expert_reuse
```

Every edge retains paper/section provenance.

A graph database is not required for V1; JSON edges are enough.

---

# 4. Landscape Output

```yaml
topic:

groups:
  problems:
  bottlenecks:
  methods:
  mechanisms:
  regimes:

aggregated_findings:

recurring_limitations:

common_assumptions:

contradictions:

underexplored_regimes:

relationships:
  - source:
    relation:
    target:
    supporting_papers:
```

The Landscape Builder answers:

> **What does this research area look like?**

It does not yet decide what future research should be done.

---

# 5. Cross-Paper Reasoning

This stage answers:

> **What do these papers collectively imply?**

It uses:

```text
Research Landscape
       +
Relevant PaperCards
       +
Actual method / result / ablation / limitation sections
```

Example:

```text
Landscape:
Caching depends on expert reuse.

Actual evidence:
P4 → strong dependence on locality
P9 → benefit drops at high routing entropy
P17 → effect strongest at small cache capacity
P21 → weaker effect for another architecture
```

Cross-paper result:

```text
Caching effectiveness appears jointly controlled by
routing entropy, cache capacity, and architecture,
rather than by locality alone.
```

---

# 6. Opportunity Miner

The Opportunity Miner answers:

> **What appears unresolved given those cross-paper findings?**

It looks for:

```text
recurring limitation
restrictive assumption
missing regime
contradiction
failure mode
missing evaluation
unresolved tradeoff
mechanistic interaction
```

Example:

```text
Cross-paper findings:
- Offloading reduces memory but introduces transfer latency.
- Caching reduces transfer cost.
- Caching degrades when routing becomes unpredictable.
- Routing entropy varies across workloads.

Potential opportunity:
Adaptive expert residency under changing routing behavior.
```

The system should verify the opportunity against the underlying papers before promoting it.

---

# 7. Future Direction Generator

This stage answers:

> **What research could attack the unresolved opportunity?**

Inputs:

```text
Validated opportunity
+
Landscape context
+
Cross-paper findings
+
Actual supporting papers
```

The gap is **not rediscovered from scratch** here.

Example direction:

```text
Entropy-aware expert caching for non-stationary MoE serving.
```

A direction may contain multiple hypotheses:

```text
H1:
Router entropy predicts degradation in expert cache hit rate.

H2:
Entropy-conditioned cache admission outperforms LRU
under routing-distribution shift.

H3:
The advantage grows as cache capacity decreases.

H4:
At sufficiently high routing entropy, predictive prefetching
becomes more useful than caching.
```

---

# 8. Candidate × Hypothesis Interpretation

If the system generates:

```text
Direction A → 3 hypotheses
Direction B → 2 hypotheses
Direction C → 4 hypotheses
```

then there are roughly:

```text
3 + 2 + 4 = 9
```

hypothesis-level things that can be explored.

But that does **not** mean 9 papers.

One experiment can test several hypotheses, and one eventual paper may contain several hypotheses from the same candidate direction.

---

# 9. Novelty Search = Second RAG

The novelty stage is a second RAG with a different purpose.

Initial RAG:

> **Which papers define the research landscape?**

Novelty RAG:

> **Which papers could threaten the novelty of this candidate direction or hypothesis?**

It searches the entire 2026 index, but does not send every paper to an LLM.

---

# 10. Novelty Signature

Do not simply reuse the words in the generated future direction.

Create a semantic decomposition.

Example:

```yaml
problem:
  - expert transfer latency
  - memory-constrained MoE inference

intervention:
  - adaptive expert caching
  - dynamic expert residency

decision_signal:
  - router entropy
  - routing uncertainty

mechanism:
  - adapt cache allocation based on routing predictability

regime:
  - non-stationary workloads

comparison:
  - LRU
  - static caching
  - frequency-based caching

expected_effect:
  - higher cache hit rate
  - lower transfer latency
```

This allows retrieval to find semantically close papers even when the wording differs.

---

# 11. Second RAG Retrieval

Generate multiple queries from the novelty signature:

```text
router entropy expert caching MoE
routing uncertainty dynamic expert residency
adaptive expert cache workload shift
expert offloading cache routing entropy
```

Then:

```text
BM25 top 50
+
Vector top 50
+
PaperCard semantic matches
      ↓
union / deduplicate
      ↓
~50–100 possible overlaps
      ↓
rerank
      ↓
~10–20 closest papers
```

The papers are intentionally **close to the direction**.

The question is:

> Did any of them actually explore the same method, mechanism, regime, or scientific hypothesis?

---

# 12. Candidate-vs-Prior-Work Comparison

For each close paper, compare:

```text
problem
method
mechanism
signal
regime
evaluation
scientific question
hypothesis
```

Labels:

```text
SAME
VERY_CLOSE
PARTIAL_OVERLAP
ADJACENT
DIFFERENT
```

Example:

| Dimension | Candidate | Paper P44 |
|---|---|---|
| Problem | expert-loading latency | same |
| Method | adaptive caching | same |
| Signal | router entropy | historical frequency |
| Regime | non-stationary | stationary |
| Question | adaptation under shift | cache optimization |
| Hypothesis | entropy helps during shift | not tested |

Possible conclusion:

```text
PARTIAL_OVERLAP

The method family is close, but P44 does not test
entropy-conditioned adaptation under changing routing distributions.
```

---

# 13. Direction-Level vs Hypothesis-Level Novelty

There are two checks.

## Direction-Level

```text
Has this overall approach already been pursued?
```

## Hypothesis-Level

```text
Has this specific scientific relationship already been tested?
```

A direction can survive even if one hypothesis is already known:

```text
H1 → already studied
H2 → partial overlap
H3 → little direct overlap
H4 → little direct overlap
```

The candidate can be refined around the remaining hypotheses.

---

# 14. Novelty Outcomes

```text
1. SAME
   → reject or heavily reframe

2. SAME METHOD, DIFFERENT QUESTION
   → keep only if the scientific question is meaningful

3. COMPONENTS EXIST SEPARATELY
   → potentially interesting if the interaction is justified

4. ADJACENT WORK
   → not necessarily a novelty threat
```

Novelty search should refine candidates, not merely say `novel / not novel`.

---

# 15. Research Critic

After novelty refinement:

```text
Is the gap actually supported?
Is the distinction from prior work meaningful?
Is the idea merely A + B?
Is there a plausible mechanism?
Are the hypotheses testable?
Can they be falsified?
Would a negative result still teach us something?
Is this research or only implementation work?
```

Actions:

```text
KEEP
REFINE
MERGE
DOWNRANK
DISCARD
```

---

# 16. Final Candidate Output

Each final candidate should look like:

```yaml
candidate_id: D3

title:
  Entropy-aware expert caching for non-stationary MoE serving

research_direction:
  Develop cache policies that adapt expert residency
  based on online routing uncertainty.

why_this_direction_exists:
  - ...

supporting_2026_evidence:
  - paper: P12
    finding: ...

unresolved_gap:
  ...

possible_hypotheses:
  - id: H1
    statement: ...
    novelty_status: PARTIAL_OVERLAP

  - id: H2
    statement: ...
    novelty_status: LOW_PRIOR_OVERLAP

suggested_initial_experiments:
  - ...

closest_prior_work:
  - paper: P44
    classification: PARTIAL_OVERLAP
    difference: ...

novelty_assessment:
  status: PROMISING

risks:
  - ...

what_would_falsify_it:
  ...

recommended_next_step:
  ...
```

---

# 17. Final System Output

Generate more candidates internally than are returned.

Example:

```text
8 opportunities
      ↓
8 candidate directions
      ↓
~20 hypotheses total
      ↓
Novelty RAG + comparison
      ↓
2 rejected
1 merged
2 downranked
      ↓
3–5 strong final directions
```

The user gets a **portfolio**, not a single bet.

---

# 18. V1 Scope

Do not add these unless needed:

```text
× multi-year trend analysis
× citation graph
× graph database
× universal research ontology
× multi-agent swarm
× automatic global novelty proof
× automatic paper writing
```

The V1 success criterion is:

> For a research topic, can the system produce a small set of directions grounded in actual 2026 evidence, explain the unresolved gap, propose testable hypotheses and experiments, and show exactly how each candidate differs from the closest prior work?
