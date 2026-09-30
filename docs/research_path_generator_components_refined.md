# Research Path Generator — Refined Component Contracts

## Service Boundary

All online components below execute within a server-deployed query-to-hypothesis
service, from query planning to the final candidate portfolio. They are logical
modules, not individually deployed services. Keep their contracts independent
of the API transport and isolate intermediate state per request. Production
retrieval uses OpenSearch; paper and evidence loading uses S3. Local backends
support development. See the architecture's
[deployment requirement](research_path_generator_architecture_refined.md#deployment-requirement-online-generation-service).

## 1. PaperCard Extractor
**Input:** existing `paper.json` (`PaperAnalysis`).
**Output:** problem, bottleneck, method, mechanism, evaluation, findings, assumptions, limitations, failures, tradeoffs, and evidence locations.

For the online stage, derive this compact view by field projection, without
another extraction LLM call or a separately stored PaperCard. Query-specific
concept normalization and derived facets belong to the landscape stages.

## 2. Initial Retriever
**Purpose:** find the papers that define the queried research landscape.  
**Implementation:** BM25 + vector + metadata retrieval + reranking.

### 2.1 Research Planner
**Input:** user query and explicit constraints.
**Output:** `QueryPlan` with only `queries: list[str]`, original query first.

Implemented as async `research.query_planner.plan_queries`: one LLM planning
call, strict output validation, whitespace/case deduplication, and a configurable
query-count cap (default five including the original). It accepts a shared
`AsyncLLMClient` for service use. Invalid output raises `QueryPlanningError`;
provider errors propagate. No automatic repair call or silent fallback.

Expand terminology and relevant subtopics while preserving user scope.
For efficient MoE inference, searches might cover caching/offloading,
routing behavior, communication, and memory–latency tradeoffs. This stage
plans retrieval; opportunities and hypotheses are produced downstream.

### 2.2 Paper-Level Retrieval and Reranking
**Input:** research plan and typed index.
**Output:** deduplicated, ranked papers with matching records, originating
searches, scores, and evidence references.

Fuse lexical and vector results, then roll record matches up to papers.
Rerank against the query and constraints and preserve coverage across relevant
approaches. Keep retrieval scores distinct from reranking assessments.
Local and OpenSearch backends should expose the same downstream contract.

Implemented retrieval: `research.retrieval.MultiQueryRetriever.retrieve(plan)`
with local and OpenSearch adapters. Each query first rolls unique matching
records into papers using decaying hybrid scores (1, 1/2, 1/4, ...). Paper ranks
are then fused with equal-weight RRF, `sum(1 / (60 + rank))`; the constant is
configurable. Each paper contributes once per unique query, with deterministic
paper-ID tie-breaking. Matching records retain per-query scores and source
locations. Reranking is still pending. Shared structured filters currently cover
record types and exact year, applied before search; no arbitrary SQL is exposed.

Reuse one retriever across service requests for bounded synchronous search
workers. Its async API owns no backend clients and keeps results request-local.
Deadlines/cancellation do not forcibly stop already-running synchronous calls;
configure backend timeouts and drain the retriever before closing its clients.
Failures do not produce silent partial rankings. Production retrieval uses
OpenSearch without requiring local artifacts.

### 2.3 Paper/Evidence Loader
**Input:** paper IDs and requested evidence references.
**Output:** paper contexts containing projected PaperCards, selected structured
details, and source excerpts with provenance.

Load `paper.json` and resolve `source_locations` to Markdown pages on demand.
Provide a common interface over local storage and S3. Surface missing artifacts
and unresolved source locations explicitly; an extracted statement alone is
not evidence that the source page was checked.

### 2.4 Shared Online Contracts
QueryPlan and the retrieved paper/record contracts are implemented; resolved
evidence and paper-context schemas remain proposed:

| Object | Required information |
|---|---|
| Query plan | `queries`: search strings, original query first |
| Retrieved paper | Paper ID, matching records, originating searches, retrieval scores and optional reranking scores |
| Evidence reference | Paper ID, record ID when applicable, source location |
| Paper context | Projected PaperCard, selected structured details, source excerpts and evidence references |

Preserve references through landscape findings, opportunities, and generated
directions so final claims can be traced back to their supporting papers.
These contracts and modules constitute the first online milestone; see
[architecture §2](research_path_generator_architecture_refined.md#2-query-to-evidence-foundation)
for the foundation design and [architecture §19](research_path_generator_architecture_refined.md#19-online-implementation-milestones)
for the build order and acceptance criteria.

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
Research Planner
   ↓
Initial RAG + paper-level reranking
   ↓
Paper/Evidence Loader + projected PaperCards
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
