"""Turn a paper's per-page Markdown into one machine-readable JSON record.

Takes a folder produced by pdf_to_markdown.py (01.md, 02.md, ...), stitches
the pages into a single document:

    <PAPER>
    <PAGE number="1">
    ...
    </PAGE>
    ...
    </PAPER>

and asks an LLM for a consistent, detailed JSON representation of the paper
(fixed schema, suitable for indexing / RAG). The result is written to
paper.json inside the same folder by default.

Env configuration (falls back to the main PROVIDER block when unset):
    RESEARCH_PROVIDER   provider (e.g. deepseek)
    RESEARCH_MODEL      model (e.g. deepseek-v4-pro; reasoning helps here)

Usage:
    uv run python -m extraction.research_extract "markdown/<paper_folder>"
    uv run python -m extraction.research_extract "markdown/<paper_folder>" --out paper.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from llm_client import LLMClient

from .extract import _clean

from typing import Literal
from pydantic import BaseModel, ConfigDict, ValidationError


# ============================================================
# Base
# ============================================================

class StrictModel(BaseModel):
    """
    All models reject unexpected fields.
    This is important for keeping extraction consistent
    across thousands of papers.
    """
    model_config = ConfigDict(extra="forbid")


# ============================================================
# Common / reusable objects
# ============================================================

class SourceLocation(StrictModel):
    """
    Page MUST correspond to the explicit <PAGE number="...">
    identifier supplied to the LLM.
    """
    page: int | None
    section: str | None
    table: str | None
    figure: str | None
    equation: str | None
    appendix: str | None


class NamedValue(StrictModel):
    """
    Used instead of arbitrary dictionaries.

    Examples:
        {"name": "learning_rate", "value": "3e-4"}
        {"name": "batch_size", "value": "256"}
        {"name": "sequence_length", "value": "8192"}
    """
    name: str
    value: str | None


# ============================================================
# Metadata
# ============================================================

class PaperMetadata(StrictModel):
    title: str
    authors: list[str]

    year: int | None
    venue: str | None
    paper_type: str | None

    identifiers: list[NamedValue]

    research_areas: list[str]
    keywords: list[str]


# ============================================================
# High-level summary
# ============================================================

class HighLevelSummary(StrictModel):
    one_sentence_summary: str

    general_problem: str
    specific_problem: str

    proposed_solution: str

    main_result: str
    why_it_matters: str


# ============================================================
# Research problem
# ============================================================

class ResearchProblem(StrictModel):
    general_problem: str
    specific_problem: str

    motivation: str

    why_difficult: list[str]

    research_questions: list[str]
    hypotheses: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Research gap
# ============================================================

class ResearchGap(StrictModel):
    gap_description: str

    what_existed_before: list[str]

    limitations_of_prior_work: list[str]

    why_existing_methods_are_insufficient: list[str]

    evidence_for_gap: list[str]

    gap_explicitly_claimed_by_authors: bool | None

    source_locations: list[SourceLocation]


# ============================================================
# Prior work
# ============================================================

class PriorWorkItem(StrictModel):
    method_or_family: str

    description: str

    strengths: list[str]
    limitations: list[str]

    relationship_to_current_paper: str

    citations_mentioned_in_paper: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Contributions
# ============================================================

ContributionType = Literal[
    "architecture",
    "algorithm",
    "training_method",
    "inference_method",
    "systems_optimization",
    "theory",
    "dataset",
    "benchmark",
    "evaluation_method",
    "empirical_finding",
    "analysis",
    "combination_of_existing_methods",
    "other",
]


class Contribution(StrictModel):
    contribution: str

    contribution_type: ContributionType

    novelty_claim: str

    what_existed_before: str

    what_this_paper_changes: str

    why_it_matters: str

    explicitly_claimed_by_authors: bool

    source_locations: list[SourceLocation]


# ============================================================
# Method - pipeline
# ============================================================

class PipelineStep(StrictModel):
    step: int | None

    name: str

    description: str

    inputs: list[str]
    outputs: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Method - components
# ============================================================

class MethodComponent(StrictModel):
    name: str

    purpose: str

    inputs: list[str]

    operations: list[str]

    outputs: list[str]

    technical_details: str

    novel_component: bool | None

    source_locations: list[SourceLocation]


# ============================================================
# Method - architecture
# ============================================================

class Architecture(StrictModel):
    description: str

    model_components: list[str]

    information_flow: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Objectives and losses
# ============================================================

class ObjectiveOrLoss(StrictModel):
    name: str

    equation: str | None

    description: str

    purpose: str

    source_locations: list[SourceLocation]


# ============================================================
# Important equations
# ============================================================

class EquationVariable(StrictModel):
    symbol: str
    meaning: str


class ImportantEquation(StrictModel):
    equation_identifier: str | None

    equation: str

    variables: list[EquationVariable]

    meaning: str

    importance: str

    source_locations: list[SourceLocation]


# ============================================================
# Training
# ============================================================

class TrainingDetails(StrictModel):
    training_procedure: str

    training_data: list[str]

    preprocessing: list[str]

    optimization: str

    initialization: str

    fine_tuning_strategy: str | None

    regularization: list[str]

    important_hyperparameters: list[NamedValue]

    compute_requirements: str | None

    source_locations: list[SourceLocation]


# ============================================================
# Inference
# ============================================================

class InferenceDetails(StrictModel):
    procedure: str

    decoding_or_sampling: str | None

    complexity: str | None

    latency_considerations: str | None

    throughput_considerations: str | None

    memory_considerations: str | None

    source_locations: list[SourceLocation]


# ============================================================
# Systems characteristics
# ============================================================

class SystemsCharacteristics(StrictModel):
    time_complexity: str | None

    space_complexity: str | None

    flops: str | None

    memory: str | None

    memory_bandwidth: str | None

    communication_cost: str | None

    latency: str | None

    throughput: str | None

    other: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Complete method
# ============================================================

class Method(StrictModel):
    high_level_idea: str

    pipeline: list[PipelineStep]

    components: list[MethodComponent]

    architecture: Architecture

    objectives_and_losses: list[ObjectiveOrLoss]

    important_equations: list[ImportantEquation]

    training: TrainingDetails

    inference: InferenceDetails

    systems_characteristics: SystemsCharacteristics


# ============================================================
# Models
# ============================================================

class ModelRecord(StrictModel):
    name: str

    role: str

    architecture: str | None

    parameter_count: str | None

    configuration: list[NamedValue]

    pretrained: bool | None

    source_locations: list[SourceLocation]


# ============================================================
# Datasets
# ============================================================

class DatasetRecord(StrictModel):
    name: str

    purpose: str

    size: str | None

    task: str | None

    domain: str | None

    train_split: str | None

    validation_split: str | None

    test_split: str | None

    important_characteristics: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Baselines
# ============================================================

class BaselineRecord(StrictModel):
    name: str

    description: str

    why_selected: str | None

    relationship_to_proposed_method: str | None

    source_locations: list[SourceLocation]


# ============================================================
# Metrics
# ============================================================

MetricDirection = Literal[
    "higher",
    "lower",
    "depends",
]


class MetricRecord(StrictModel):
    name: str

    description: str

    what_it_measures: str

    better_direction: MetricDirection | None

    source_locations: list[SourceLocation]


# ============================================================
# Experimental setup
# ============================================================

class ExperimentalSetup(StrictModel):
    hardware: list[str]

    software: list[str]

    training_configuration: list[NamedValue]

    inference_configuration: list[NamedValue]

    number_of_runs: int | None

    random_seeds: list[str]

    statistical_testing: str | None

    other_details: list[str]

    source_locations: list[SourceLocation]


# ============================================================
# Experiment results
# ============================================================

class ExperimentResult(StrictModel):
    method: str

    metric: str

    # Parsed numeric value when cleanly available.
    value: float | None

    # Preserve the literal representation when useful:
    # "89.3 ± 0.2", "2.1x", "< 1 ms", etc.
    raw_value: str | None

    unit: str | None

    dataset_or_setting: str | None


# ============================================================
# Experiments
# ============================================================

EvidenceStrength = Literal[
    "strong",
    "moderate",
    "weak",
]


class ExperimentRecord(StrictModel):
    experiment_name: str

    research_question: str

    hypothesis_being_tested: str | None

    setup: str

    comparison: str

    datasets: list[str]

    models: list[str]

    baselines: list[str]

    metrics: list[str]

    results: list[ExperimentResult]

    key_result: str

    authors_conclusion: str

    evidence_supports_conclusion: EvidenceStrength

    source_locations: list[SourceLocation]


# ============================================================
# Ablations
# ============================================================

class AblationRecord(StrictModel):
    component_or_variable: str

    change: str

    motivation: str

    baseline_setting: str | None

    modified_setting: str | None

    result: str

    quantitative_change: str | None

    interpretation: str

    source_locations: list[SourceLocation]


# ============================================================
# Scaling / sensitivity
# ============================================================

class ScalingSensitivityRecord(StrictModel):
    variable: str

    values_tested: list[str]

    observed_trend: str

    interpretation: str

    source_locations: list[SourceLocation]


# ============================================================
# Key results
# ============================================================

class KeyResultRecord(StrictModel):
    finding: str

    quantitative_result: str | None

    comparison: str | None

    importance: str

    source_locations: list[SourceLocation]


# ============================================================
# Interesting / unexpected findings
# ============================================================

FindingExpectation = Literal[
    "expected",
    "unexpected",
    "unclear",
]


class InterestingFinding(StrictModel):
    finding: str

    expected_or_unexpected: FindingExpectation

    why_interesting: str

    possible_implication: str

    authors_discuss_implication: bool | None

    source_locations: list[SourceLocation]


# ============================================================
# Important figures / tables
# ============================================================

FigureTableType = Literal[
    "figure",
    "table",
]


class ImportantFigureOrTable(StrictModel):
    type: FigureTableType

    identifier: str

    what_it_shows: str

    key_observation: str

    importance: str

    source_locations: list[SourceLocation]


# ============================================================
# Failure cases
# ============================================================

class FailureCase(StrictModel):
    failure: str

    conditions: str

    observed_behavior: str

    possible_cause: str | None

    implication: str

    source_locations: list[SourceLocation]


# ============================================================
# Limitations
# ============================================================

class AuthorStatedLimitation(StrictModel):
    limitation: str

    impact: str | None

    source_locations: list[SourceLocation]


LimitationSeverity = Literal[
    "low",
    "medium",
    "high",
]


class InferredLimitation(StrictModel):
    limitation: str

    reasoning: str

    severity: LimitationSeverity

    evidence: str

    source_locations: list[SourceLocation]


class Limitations(StrictModel):
    author_stated: list[AuthorStatedLimitation]

    inferred: list[InferredLimitation]


# ============================================================
# Future work
# ============================================================

class AuthorProposedFutureWork(StrictModel):
    direction: str

    motivation: str

    source_locations: list[SourceLocation]


Confidence = Literal[
    "low",
    "medium",
    "high",
]


class InferredResearchOpportunity(StrictModel):
    observation_or_limitation: str

    research_question: str

    why_it_matters: str

    potential_approach: str | None

    confidence: Confidence

    supporting_source_locations: list[SourceLocation]


class FutureWork(StrictModel):
    author_proposed: list[AuthorProposedFutureWork]

    inferred_research_opportunities: list[InferredResearchOpportunity]


# ============================================================
# Claims and evidence
# ============================================================

ClaimType = Literal[
    "empirical",
    "theoretical",
    "methodological",
    "efficiency",
    "generalization",
    "other",
]


class ClaimEvidenceRecord(StrictModel):
    claim: str

    claim_type: ClaimType

    evidence: list[str]

    source_locations: list[SourceLocation]

    evidence_strength: EvidenceStrength

    reasoning: str


# ============================================================
# Reproducibility
# ============================================================

class Reproducibility(StrictModel):
    code_available: bool | None

    code_url: str | None

    data_available: bool | None

    data_url: str | None

    model_checkpoints_available: bool | None

    required_resources: list[str]

    reproduction_steps: list[str]

    missing_information: list[str]

    reproducibility_assessment: str


# ============================================================
# Paper assessment
# ============================================================

class PaperAssessment(StrictModel):
    main_strengths: list[str]

    main_weaknesses: list[str]

    most_important_contribution: str

    most_important_result: str

    most_important_limitation: str

    most_interesting_open_question: str

    what_the_paper_demonstrates: list[str]

    what_the_paper_does_not_demonstrate: list[str]


# ============================================================
# Retrieval tags
# ============================================================

class RetrievalTags(StrictModel):
    problems: list[str]

    methods: list[str]

    architectures: list[str]

    model_families: list[str]

    training_techniques: list[str]

    inference_techniques: list[str]

    optimization_techniques: list[str]

    datasets: list[str]

    benchmarks: list[str]

    metrics: list[str]

    applications: list[str]

    hardware_or_system_topics: list[str]

    theoretical_topics: list[str]


# ============================================================
# ROOT SCHEMA
# ============================================================

class PaperAnalysis(StrictModel):
    schema_version: Literal["paper_analysis_v1"]

    paper_metadata: PaperMetadata

    high_level_summary: HighLevelSummary

    research_problem: ResearchProblem

    research_gap: ResearchGap

    prior_work: list[PriorWorkItem]

    contributions: list[Contribution]

    method: Method

    models: list[ModelRecord]

    datasets: list[DatasetRecord]

    baselines: list[BaselineRecord]

    metrics: list[MetricRecord]

    experimental_setup: ExperimentalSetup

    experiments: list[ExperimentRecord]

    ablations: list[AblationRecord]

    scaling_and_sensitivity: list[ScalingSensitivityRecord]

    key_results: list[KeyResultRecord]

    interesting_findings: list[InterestingFinding]

    important_figures_and_tables: list[ImportantFigureOrTable]

    failure_cases: list[FailureCase]

    limitations: Limitations

    future_work: FutureWork

    claims_and_evidence: list[ClaimEvidenceRecord]

    reproducibility: Reproducibility

    paper_assessment: PaperAssessment

    retrieval_tags: RetrievalTags


PROMPT = f"""
You are an expert research scientist analyzing machine learning research papers for a large-scale scientific literature retrieval, comparison, and research-gap discovery system.

The complete research paper will be provided as page-delimited Markdown.

Your task is to read the ENTIRE paper and populate the provided structured-output schema accurately and comprehensively.

The structured-output schema is supplied separately and is the authoritative output format. Follow it exactly.

# INPUT FORMAT

The paper is provided in original page order:

```text
<PAPER>

<PAGE number="1">
...
</PAGE>

<PAGE number="2">
...
</PAGE>

...

</PAPER>
```

Treat all pages together as ONE complete research paper.

Do NOT analyze or summarize pages independently.

An idea introduced on one page may be explained, qualified, evaluated, contradicted, or limited on later pages. Form conclusions only after considering the entire paper.

The Markdown may contain extraction artifacts, including:

* repeated headers or footers,
* page numbers,
* broken line wrapping,
* formatting noise,
* duplicated captions,
* malformed tables,
* Markdown conversion errors.

Ignore artifacts when they are clearly not part of the scientific content.

# FUNDAMENTAL EXTRACTION RULES

## 1. Ground everything in this paper

Extract information from this specific paper.

Do not insert information merely because you know it from general domain knowledge.

Do not augment the paper with facts from outside sources.

If the paper does not provide something, represent it as missing according to the output schema.

Never fabricate missing information.

---

## 2. Read the complete paper

Do not rely only on:

* the abstract,
* introduction,
* related work,
* or conclusion.

Important information may appear only in:

* the method,
* experimental sections,
* tables,
* figures,
* appendices,
* ablations,
* limitations section.

Use the entire provided paper.

---

## 3. Distinguish evidence from interpretation

Maintain a strict distinction between:

### Author-stated information

Statements, contributions, limitations, conclusions, or future work explicitly presented by the authors.

### Evidence-supported findings

Conclusions directly supported by experiments, analyses, or theoretical results in the paper.

### Inferred analysis

Your own conservative analysis of limitations, evidence strength, or potential research opportunities.

Never present inferred analysis as an author claim.

---

## 4. Missing information

Do not guess.

If information required by the schema is not available:

* use `null` for nullable scalar fields,
* use `[]` when no legitimate list entries exist,
* use the appropriate empty representation required by the schema.

An empty list is preferable to invented content.

In particular, there is NO requirement that a paper have:

* a certain number of contributions,
* inferred limitations,
* research opportunities,
* failure cases,
* ablations,
* scaling experiments.

If none legitimately exist, return an empty list.

---

# SOURCE GROUNDING

The supplied `<PAGE number="...">` identifiers are the canonical page identifiers for this task.

Whenever the schema requests `source_locations`, use these supplied page numbers.

Do NOT infer page numbers from page numbers printed inside the Markdown.

For example, if information comes from:

```text
<PAGE number="8">
```

then:

`page = 8`

When available, also record:

* section,
* table,
* figure,
* equation,
* appendix.

Only record identifiers explicitly visible in the paper.

Never fabricate a section number, figure number, table number, equation number, or appendix identifier.

If an identifier is unavailable, use `null`.

Use the most specific source locations reasonably supporting the extracted claim.

---

# RESEARCH PROBLEM

Distinguish carefully between:

### General problem

The broader scientific or engineering problem.

### Specific problem

The narrower problem this paper actually attempts to solve.

For example, "efficient LLM inference" may be the general problem, while "reducing KV-cache memory-bandwidth cost without substantially degrading attention quality" may be the specific problem.

Do not collapse these into the same generic description.

---

# RESEARCH GAP

The `research_gap` field is especially important.

Identify the precise technical deficiency in existing work that motivates this paper.

Avoid vague statements.

BAD:

> Existing methods are inefficient.

BAD:

> Previous work has limitations.

GOOD:

> Existing multi-head attention implementations maintain independent key and value states for each KV head, increasing KV-cache capacity and memory-bandwidth requirements during autoregressive decoding.

A useful research gap should make clear:

1. what existed previously,
2. what limitation remained,
3. why that limitation matters,
4. what capability was missing.

Determine whether the gap is explicitly claimed by the authors or inferred from their discussion.

Do not manufacture a research gap beyond what the paper supports.

---

# PRIOR WORK

Represent important prior-work families or methods only when they materially help explain:

* the research gap,
* the proposed method,
* comparisons,
* or novelty.

Do not attempt to reproduce the entire bibliography.

For each important prior approach, distinguish:

* what it does,
* its relevant strengths,
* its relevant limitations,
* how the current paper relates to it.

Only report strengths or limitations supported by the paper's discussion.

---

# CONTRIBUTIONS

Create one contribution record for each genuinely distinct contribution.

Do not merge unrelated contributions merely to reduce the number of entries.

Do not split a single contribution into multiple artificial entries merely to increase the count.

For each contribution, determine:

**What existed before → What changed → Why the change matters**

Distinguish novel components from existing techniques used as building blocks.

Do not treat every component appearing in the proposed system as novel.

Use the contribution type that best describes the contribution.

---

# METHOD

Reconstruct the proposed method as faithfully as possible.

## High-level idea

Capture the central conceptual insight.

## Pipeline

Break the method into meaningful sequential stages when such a pipeline exists.

Pipeline stages should represent actual computational or conceptual stages, not arbitrary paragraphs from the paper.

## Components

Create component records for distinct functional pieces of the method.

For each component extract:

* purpose,
* inputs,
* operations,
* outputs,
* important technical details.

Indicate whether the component appears novel only when this can reasonably be determined from the paper.

## Architecture

Describe how major components fit together and how information flows.

## Objectives and losses

Capture objectives or losses that are important to the proposed method.

Do not populate this merely with standard losses mentioned incidentally.

## Equations

Capture equations necessary to understand:

* the proposed method,
* a novel formulation,
* theoretical claims,
* important complexity relationships.

Do not extract every routine mathematical expression.

Preserve the mathematical relationship faithfully.

## Training

Capture important:

* data,
* preprocessing,
* optimization,
* initialization,
* fine-tuning,
* regularization,
* hyperparameters,
* compute.

Do not infer missing hyperparameters.

## Inference

Describe what actually happens at inference time.

Capture relevant:

* decoding,
* caching,
* retrieval,
* routing,
* batching,
* sampling,
* parallelism,
* latency,
* throughput,
* memory effects.

## Systems characteristics

Only populate system-performance characteristics when meaningfully discussed.

Do not infer FLOPs, memory, complexity, or latency numbers unless the paper provides enough information to support them.

---

# MODELS

Create records for models that materially participate in:

* the proposed method,
* training,
* evaluation,
* or baselines.

Do not create records for models mentioned only incidentally in related work.

Preserve parameter counts exactly as reported.

Do not convert approximate values into falsely precise numbers.

For example, preserve `~7B` rather than inventing `7,000,000,000`.

---

# DATASETS

Create one record per important dataset or benchmark dataset.

Capture:

* purpose,
* task,
* size,
* domain,
* split details,
* characteristics relevant to the experiment.

Do not guess dataset sizes or splits from general knowledge.

---

# BASELINES

Record baselines that are important for interpreting the main experimental claims.

Explain their relationship to the proposed method when the paper makes this clear.

Do not treat every cited model as an experimental baseline.

---

# METRICS

Record metrics actually used to evaluate the paper.

State what the metric measures and whether higher or lower is preferable when that is well-defined.

If direction depends on context, use the schema's `depends` option.

---

# EXPERIMENTS

This section is extremely important.

Organize experiments according to the SCIENTIFIC QUESTION being tested, not merely by table number.

BAD:

> Table 4 experiment.

GOOD:

> Does grouped-query attention preserve multi-head-attention quality while reducing inference memory requirements?

For every major experiment identify:

* research question,
* setup,
* models,
* datasets,
* baselines,
* metrics,
* important results,
* author conclusion.

Do not copy every number from every table.

Capture quantitative results that are important for:

* supporting major claims,
* comparing methods,
* understanding tradeoffs,
* discovering limitations,
* understanding scaling behavior.

When a value can be cleanly represented numerically, populate the numeric value.

When the original result contains additional semantics such as:

* `89.3 ± 0.2`,
* `2.1x`,
* `<1 ms`,
* `~95%`,

preserve the original representation in `raw_value`.

---

# EVIDENCE STRENGTH

When evaluating whether an experiment supports a conclusion, use:

## strong

The claim is directly supported by appropriate experiments, rigorous analysis, or theoretical proof, with sufficiently broad or convincing evidence for the stated claim.

## moderate

Evidence supports the claim, but evaluation scope, uncertainty, baselines, statistical evidence, scale, or generality is meaningfully limited.

## weak

Evidence is indirect, narrow, incomplete, or substantially weaker than the breadth of the claim.

Judge evidence relative to the CLAIM being made.

A good experiment may still provide weak evidence for an overly broad claim.

---

# ABLATIONS

Record meaningful ablations separately.

For each ablation identify:

* component or variable modified,
* original condition,
* modified condition,
* motivation,
* result,
* quantitative effect when available,
* interpretation.

The purpose is to expose which parts of the method actually cause the reported improvement.

Do not call an ordinary baseline comparison an ablation unless it actually modifies/removes a component or design choice.

---

# SCALING AND SENSITIVITY

Record experiments studying changes in variables such as:

* model size,
* data size,
* compute,
* context length,
* number of layers,
* attention heads,
* groups,
* retrieval size,
* batch size,
* hyperparameters.

Capture the observed trend rather than merely listing tested values.

If no meaningful scaling or sensitivity analysis exists, return an empty list.

---

# KEY RESULTS

Capture the results most important to understanding the paper's scientific contribution.

A key result should answer:

> What evidence would I cite if I had to explain why this paper matters?

Preserve important quantitative comparisons when available.

---

# INTERESTING FINDINGS

This section should NOT simply duplicate `key_results`.

Capture observations that may be scientifically revealing, surprising, unexplained, or especially useful for cross-paper comparison.

Examples include:

* performance saturates after a particular scale,
* gains disappear under one setting,
* a simpler method unexpectedly performs similarly,
* an efficiency/quality tradeoff behaves differently than expected,
* different layers or model sizes show qualitatively different behavior,
* a presumed bottleneck becomes less important at larger scales.

Explain why the observation is interesting.

Distinguish whether the authors themselves discuss its implications.

---

# FIGURES AND TABLES

Include figures/tables only when they are important to understanding:

* the method,
* central results,
* ablations,
* scaling,
* or an important finding.

Do not enumerate every figure and table.

Explain what the important artifact demonstrates rather than merely describing its appearance.

---

# FAILURE CASES

Only create failure cases when the paper actually presents or clearly demonstrates them.

A weak benchmark score alone is not necessarily a failure case unless it exposes a meaningful condition under which the method fails.

Capture:

* failure,
* conditions,
* observed behavior,
* possible cause if supported,
* implication.

Do not invent causes.

---

# LIMITATIONS

This distinction is mandatory.

## author_stated

Only include limitations explicitly acknowledged by the authors.

Do not place your own criticism here.

## inferred

You may infer additional limitations, but be conservative.

Every inferred limitation must be connected to concrete evidence such as:

* narrow evaluation,
* missing relevant comparison,
* limited scale,
* failure case,
* strong assumption,
* absence of an important experiment,
* unexplained result,
* sensitivity,
* compute requirements.

For each inferred limitation provide:

**Limitation → Reasoning → Evidence**

Do not manufacture criticisms merely to produce entries.

An empty inferred-limitations list is valid.

---

# FUTURE WORK

## author_proposed

Only include future directions explicitly suggested by the authors.

Do not transform your interpretation into author-proposed future work.

## inferred_research_opportunities

This field is particularly important for downstream research-gap discovery.

Every inferred opportunity MUST originate from a concrete observation in this paper.

Valid origins include:

* author-stated limitation,
* inferred limitation,
* failure case,
* unexplained finding,
* contradictory result,
* missing experiment,
* narrow evaluation,
* scaling behavior,
* assumption,
* observed tradeoff.

Represent each opportunity as:

**Observation or limitation → Unanswered research question → Why it matters**

A potential approach may be included when there is a reasonable concrete direction, but it is optional.

Avoid generic brainstorming.

BAD:

> Use larger models.

BAD:

> Improve efficiency further.

BAD:

> Test on more datasets.

BETTER:

> Performance is evaluated only at a fixed number of KV groups. Does the optimal number of KV groups vary by transformer layer or model scale due to differences in attention-head specialization?

The research question should be specific enough that it could plausibly motivate a research project or experiment.

Use confidence conservatively.

---

# CLAIMS AND EVIDENCE

Capture the paper's major claims.

For every claim:

1. state the claim precisely,
2. identify the evidence supporting it,
3. provide source locations,
4. assess evidence strength,
5. explain the assessment.

Do not populate this section with trivial descriptive statements.

Prioritize claims central to:

* novelty,
* effectiveness,
* efficiency,
* theoretical guarantees,
* generalization,
* scalability.

---

# REPRODUCIBILITY

Extract information needed to reproduce the work.

Capture:

* code availability,
* datasets,
* model checkpoints,
* hardware,
* preprocessing,
* training steps,
* important hyperparameters,
* evaluation methodology,
* required external resources.

Do not assume code or data availability based on outside knowledge.

`missing_information` should identify reproduction-critical details that appear absent or underspecified.

---

# PAPER ASSESSMENT

The paper assessment should reflect the evidence in the complete paper.

## main_strengths

Identify substantive methodological or empirical strengths.

## main_weaknesses

Be evidence-based and conservative.

## what_the_paper_demonstrates

State conclusions reasonably supported by the provided evidence.

## what_the_paper_does_not_demonstrate

Define the boundary of the evidence.

Examples:

* evaluation is limited to specific model sizes,
* no evidence is provided for multilingual generalization,
* production latency was not evaluated,
* scaling beyond the tested regime remains unknown.

Do not criticize the paper for every possible experiment it could theoretically have performed.

Focus on boundaries relevant to its claims.

---

# RETRIEVAL TAGS

Retrieval tags will be used for:

* filtering,
* semantic retrieval,
* lexical retrieval,
* clustering,
* cross-paper analysis.

Use concise canonical technical concepts.

GOOD:

* KV cache
* grouped-query attention
* autoregressive decoding
* memory bandwidth
* transformer inference

BAD:

* This paper tries to make transformer inference faster.
* A method for reducing memory during inference.

Do not intentionally generate many synonyms for the same concept.

Use terminology commonly associated with the research area and terminology used by the paper.

---

# FINAL INTERNAL VALIDATION

Before producing the structured output, internally verify:

1. You considered the complete provided paper.

2. No information was introduced solely from outside knowledge.

3. Major contributions are represented.

4. Important experiments are represented.

5. Important numerical results supporting central claims are retained.

6. Research gaps are technically specific.

7. Author-stated and inferred limitations are separated.

8. Author-proposed and inferred future work are separated.

9. Inferred research opportunities originate from concrete paper evidence.

10. Major claims are connected to supporting evidence.

11. Source page numbers correspond ONLY to supplied `<PAGE number>` identifiers.

12. No section/table/figure/equation identifiers were fabricated.

13. Missing information was left missing rather than guessed.

14. Retrieval tags are concepts rather than descriptive sentences.

15. The output conforms exactly to the supplied structured-output schema.

"""


# DeepSeek's API has no server-side schema enforcement (response_format only
# supports "json_object", not "json_schema" — the latter is rejected with 400).
# So we supply the JSON Schema in the request, force JSON mode, and enforce
# the schema client-side with pydantic, feeding validation errors back to the
# model for repair rounds.
SCHEMA_JSON = json.dumps(PaperAnalysis.model_json_schema(), indent=2)

MAX_REPAIR_ROUNDS = 2


def _schema_message(document: str) -> str:
    return (
        "# STRUCTURED-OUTPUT SCHEMA\n\n"
        "Your entire output must be a single JSON object that validates "
        "against this JSON Schema. Output ONLY the JSON object — no prose, "
        "no code fences. Every property is required; use null / [] exactly "
        "where the schema allows them. Do not add properties that are not "
        "in the schema.\n\n"
        f"```json\n{SCHEMA_JSON}\n```\n\n"
        "# PAPER\n\n"
        f"{document}")


def _format_validation_errors(exc: ValidationError, limit: int = 40) -> str:
    lines = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"- {loc}: {err['msg']}")
    if exc.error_count() > limit:
        lines.append(f"- ... and {exc.error_count() - limit} more errors")
    return "\n".join(lines)


def stitch_pages(folder: Path) -> str:
    """Join NN.md files into one <PAPER> document with <PAGE> tags."""
    page_files = sorted(
        (f for f in folder.glob("*.md") if f.stem.isdigit()),
        key=lambda f: int(f.stem))
    if not page_files:
        raise FileNotFoundError(f"No page files (NN.md) found in {folder}")
    parts = ["<PAPER>"]
    for f in page_files:
        parts.append(f'\n<PAGE number="{int(f.stem)}">')
        parts.append(f.read_text().strip())
        parts.append("</PAGE>")
    parts.append("\n</PAPER>")
    return "\n".join(parts)


def _parse_json(raw: str) -> dict:
    text = _clean(raw)
    # tolerate stray text around the JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in model output: {text[:200]!r}")
    return json.loads(match.group(0))


def extract_research(folder: Path, *,
                     provider: str | None = None,
                     model: str | None = None) -> dict:
    provider = provider or os.environ.get("RESEARCH_PROVIDER")
    model = model or os.environ.get("RESEARCH_MODEL")
    client = LLMClient(provider)
    print(f"Provider: {client.provider} | model: {model or client.default_model}")

    document = stitch_pages(folder)
    print(f"Stitched {document.count('<PAGE')} pages "
          f"({len(document)} chars); querying LLM...")

    messages = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": _schema_message(document)},
    ]

    last_error = None
    for attempt in range(1 + MAX_REPAIR_ROUNDS):
        raw = client.chat(messages=messages, model=model,
                          response_format={"type": "json_object"})
        if not raw or not raw.strip():
            # Known DeepSeek JSON-mode quirk: occasional empty responses.
            print(f"Attempt {attempt + 1}: empty response, retrying...")
            last_error = "empty response"
            continue
        try:
            data = _parse_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"Attempt {attempt + 1}: unparseable JSON ({exc}), retrying...")
            last_error = str(exc)
            continue
        try:
            return PaperAnalysis.model_validate(data).model_dump()
        except ValidationError as exc:
            errors = _format_validation_errors(exc)
            print(f"Attempt {attempt + 1}: {exc.error_count()} schema "
                  f"violations, asking model to repair...")
            last_error = errors
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    "Your JSON output failed validation against the supplied "
                    "schema. Fix these errors and output the complete, "
                    "corrected JSON object (nothing else):\n\n" + errors},
            ]

    raise RuntimeError(
        f"Schema-conformant output not obtained after "
        f"{1 + MAX_REPAIR_ROUNDS} attempts. Last error:\n{last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder",
                        help="Paper folder with per-page markdown (01.md, ...)")
    parser.add_argument("--out", default=None,
                        help="Output JSON path (default: <folder>/paper.json)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider (default: RESEARCH_PROVIDER or PROVIDER)")
    parser.add_argument("--model", default=None,
                        help="Model (default: RESEARCH_MODEL or provider's model)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    record = extract_research(folder, provider=args.provider, model=args.model)

    out_path = Path(args.out) if args.out else folder / "paper.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    print(f"Wrote {out_path}")
    print(f"Title: {record['paper_metadata']['title']}")
    print(f"TLDR: {record['high_level_summary']['one_sentence_summary']}")


if __name__ == "__main__":
    main()
