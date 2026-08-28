# Research Path Generator — Refined Component Contracts

## 1. PaperCard Extractor
**Input:** parsed paper.  
**Output:** problem, bottleneck, method, mechanism, evaluation, findings, assumptions, limitations, failures, tradeoffs, and evidence locations.

## 2. Initial Retriever
**Purpose:** find the papers that define the queried research landscape.  
**Implementation:** BM25 + vector + metadata retrieval + reranking.

## 3. Concept Normalizer
Maps equivalent terminology to canonical query-specific concepts.

```text
expert swapping
CPU-GPU expert transfer
host-side expert loading
    ↓
expert_transfer
```

## 4. Faceted Grouper
Groups papers along overlapping facets:

```text
problem
bottleneck
method
mechanism
regime
hardware
model scale
optimization target
failure mode
```

A paper may belong to many groups.

## 5. Finding Aggregator
Merges semantically equivalent paper-level observations into evidence-backed group-level findings.

```text
paper findings
  ↓
embeddings
  ↓
cluster near-equivalent statements
  ↓
LLM cluster summary
  ↓
retain supporting papers
```

## 6. Relation Extractor
Extracts evidence-backed triples such as:

```text
expert_offloading --INTRODUCES--> transfer_latency
expert_caching --DEPENDS_ON--> expert_reuse
```

Every edge keeps provenance.

## 7. Landscape Builder
Answers:

> What does this research area look like?

Input: normalized PaperCards + groups + aggregated findings + relations.

## 8. Cross-Paper Reasoner
Answers:

> What do these papers collectively imply?

Input: landscape + actual PaperCards + relevant paper sections.

The landscape guides comparison; actual papers determine the conclusion.

## 9. Opportunity Miner
Answers:

> What appears unresolved?

Searches for recurring limitations, assumptions, missing regimes, contradictions, failure modes, missing evaluations, tradeoffs, and mechanistic interactions.

## 10. Future Direction Generator
Turns a validated opportunity into a broader research direction.

Input:

```text
Opportunity
+
Landscape
+
Cross-paper findings
+
Actual supporting papers
```

Output: direction + rationale + hypotheses + experiments + risks.

## 11. Hypothesis Generator
Creates testable claims inside a direction.

```text
Under condition C,
doing X should affect Y
because mechanism M.
```

A direction may contain several hypotheses.

## 12. Experiment Generator
Proposes the cheapest informative test first, then follow-up experiments.

## 13. Novelty Signature Extractor
Semantically decomposes a candidate into:

```text
problem
intervention
mechanism
signal
regime
comparison
expected effect
```

Do not simply copy keywords.

## 14. Novelty Retriever — Second RAG
Searches the entire 2026 corpus for possible novelty threats.

```text
signature
  ↓
multi-query retrieval
  ↓
~50–100 possible overlaps
  ↓
rerank
  ↓
~10–20 closest papers
```

## 15. Candidate-vs-Paper Comparator
Compares candidate and prior paper across:

```text
problem
method
mechanism
signal
regime
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

## 16. Direction-Level Novelty Checker
Asks whether the overall direction has already been pursued.

## 17. Hypothesis-Level Novelty Checker
Asks whether a specific scientific relationship has already been tested.

A direction can survive even if one hypothesis is already known.

## 18. Candidate Refiner
Possible actions:

```text
KEEP
REFRAME
NARROW
MERGE
REJECT
```

## 19. Research Critic
Checks whether the gap is real, the distinction is meaningful, the mechanism is plausible, hypotheses are falsifiable, and the work is research rather than only implementation.

## 20. Direction Ranker
Potential criteria:

```text
evidence strength
novelty within corpus
importance
technical depth
feasibility
experimental clarity
potential impact
risk
```

## 21. Final Candidate Renderer
Each candidate contains:

```text
research direction
why it emerged
supporting 2026 evidence
unresolved gap
1–N hypotheses
initial experiments
closest prior work
difference from prior work
novelty status per hypothesis
risks
falsification criteria
recommended next experiment
```

---

# Full Responsibility Flow

```text
Initial RAG
   ↓
Concept Normalizer
   ↓
Faceted Grouper
   ↓
Finding Aggregator
   ↓
Relation Extractor
   ↓
Landscape Builder
   ↓
Cross-Paper Reasoner
   ↓
Opportunity Miner
   ↓
Future Direction Generator
   ↓
Hypotheses + Experiments
   ↓
Novelty Signature
   ↓
Second RAG
   ↓
Closest Prior Work
   ↓
Candidate-vs-Paper Comparison
   ↓
Direction/Hypothesis Novelty
   ↓
Candidate Refiner
   ↓
Research Critic
   ↓
Ranker
   ↓
Final Candidate Portfolio
```

These are reasoning responsibilities, not necessarily separate agents or microservices.
