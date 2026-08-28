"""Turn a paper's per-page Markdown into one detailed summary.md.

Takes a folder produced by pdf_to_markdown.py (01.md, 02.md, ...), stitches
the pages into a single document:

    <PAPER>
    <PAGE number="1">
    ...
    </PAGE>
    ...
    </PAPER>

and asks an LLM for a detailed Markdown summary of the paper. The result is
written to summary.md inside the same folder by default.

Env configuration (falls back to the main PROVIDER block when unset):
    SUMMARY_PROVIDER   provider (e.g. deepseek)
    SUMMARY_MODEL      model (e.g. deepseek-v4-pro)

Usage:
    uv run python -m extraction.extract_summary "markdown/<paper_folder>"
    uv run python -m extraction.extract_summary "markdown/<paper_folder>" --out summary.md
"""

import argparse
import os
import sys
from pathlib import Path

from llm_client import LLMClient

from .extract import _clean
from .research_extract import stitch_pages

PROMPT = """You are an expert research scientist specializing in machine learning, deep learning, large language models, NLP, computer vision, reinforcement learning, and ML systems.

Your task is to read the COMPLETE research paper provided below and produce a **detailed, technically faithful, human-readable technical summary**.

This summary will serve two purposes:

1. allow a technically sophisticated reader to understand the paper without immediately reading the entire original;
2. later be chunked and indexed in a semantic retrieval / RAG system.

Therefore, prioritize:

* technical completeness,
* clear section structure,
* precise terminology,
* preservation of important numerical results,
* explicit discussion of research gaps and limitations,
* minimal unnecessary repetition.

Do NOT optimize for brevity.

---

# INPUT FORMAT

The paper is provided as Markdown pages in original page order:

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

Treat ALL pages together as ONE complete paper.

Do NOT summarize individual pages independently.

An idea introduced on one page may be:

* explained,
* qualified,
* evaluated,
* contradicted,
* or limited

on later pages.

Read the entire paper before forming conclusions.

The Markdown may contain extraction artifacts such as:

* repeated headers,
* repeated footers,
* page numbers,
* broken line wrapping,
* malformed tables,
* duplicated captions,
* formatting noise.

Ignore obvious extraction artifacts.

---

# CRITICAL RULES

## 1. Be faithful to the paper

Do not invent:

* claims,
* motivations,
* contributions,
* methods,
* datasets,
* results,
* experiments,
* hyperparameters,
* assumptions,
* limitations,
* or conclusions.

If information is unavailable or unclear, explicitly say so.

---

## 2. Distinguish three types of statements

Clearly distinguish between:

### Author-stated information

Claims, limitations, motivations, conclusions, or future work explicitly stated by the authors.

### Evidence-supported interpretation

Conclusions that follow directly from experiments, theoretical results, or analyses in the paper.

### Your own analysis

Reasonable interpretation of limitations, evidence strength, or unanswered questions.

Never present your own inference as an author claim.

---

## 3. Preserve technical details

Do not over-compress important technical information.

Preserve important:

* architecture details,
* algorithms,
* equations,
* losses,
* objectives,
* training procedures,
* inference procedures,
* datasets,
* baselines,
* metrics,
* ablations,
* hyperparameters,
* complexity,
* memory characteristics,
* latency / throughput results,
* compute requirements,
* quantitative comparisons.

Explain them rather than merely listing them.

---

## 4. Explain WHY, not only WHAT

For important methodological decisions, explain:

* What problem is this solving?
* Why is this component needed?
* How does it work?
* What was done before?
* What does this paper change?
* Why might that change help?
* What experimental evidence supports it?

---

# SOURCE GROUNDING

The supplied `<PAGE number="...">` identifiers are the canonical page numbers.

Whenever practical, ground important claims using:

* Page,
* Section,
* Table,
* Figure,
* Equation,
* Appendix.

Example:

> The method reduces KV-cache requirements while retaining performance close to multi-head attention (Page 7, Table 2).

Page numbers MUST correspond to the supplied `<PAGE number>` identifiers.

Do NOT infer page numbers from numbers printed inside the extracted Markdown.

Never fabricate:

* section identifiers,
* table identifiers,
* figure identifiers,
* equation identifiers,
* appendix identifiers.

---

# OUTPUT STRUCTURE

# 1. Paper at a Glance

Provide:

* **Title**
* **Authors**
* **Venue / year**, if available
* **Research area(s)**
* **Main research problem**
* **Specific problem addressed**
* **Main contribution in 1–2 sentences**
* **Most important result**
* **3–7 major contributions**

Then provide a concise 2–4 paragraph overview of the complete paper.

This section should allow someone to quickly understand what the paper is about.

---

# 2. Research Problem and Motivation

Explain:

* What broader problem is being studied?
* What specific problem does this paper address?
* Why is the problem important?
* Why is the problem technically difficult?
* What limitations exist in current approaches?
* What research question is the paper trying to answer?
* What hypotheses, if any, are being tested?

Explicitly distinguish:

### General Research Problem

from

### Specific Problem Addressed by This Paper

---

# 3. Research Gap

Identify the precise gap in prior research that motivates this work.

Explain:

* What approaches existed before?
* What could they not do adequately?
* What technical limitation remained unresolved?
* Why does that limitation matter?
* What capability was missing?
* How does this paper intend to address the gap?

Avoid vague descriptions.

BAD:

> Existing methods are inefficient.

BETTER:

> Existing approaches require maintaining independent key/value states for every attention head, increasing KV-cache memory and memory-bandwidth requirements during autoregressive decoding.

Clearly indicate whether the gap is:

* explicitly stated by the authors,
* or inferred from their discussion.

---

# 4. Background and Prerequisites

Explain only the concepts necessary to understand this paper.

Potentially include:

* architectures,
* prior algorithms,
* mathematical concepts,
* training approaches,
* inference concepts,
* benchmarks,
* systems terminology.

For each prerequisite:

* explain what it is,
* explain why it matters for this paper.

Do NOT create a generic textbook chapter.

Focus specifically on knowledge necessary to understand this work.

---

# 5. Prior Work and Positioning

Describe the major prior approaches relevant to the paper.

For each important method or family:

* What does it do?
* What are its strengths?
* What limitations are relevant here?
* How does this paper differ?
* Does the current paper extend, combine, replace, or challenge it?

Do not reproduce the entire related-work section or bibliography.

Focus on work necessary to understand the paper's novelty and research gap.

---

# 6. Core Technical Insight

Explain the central idea of the paper intuitively.

Answer:

* What did the authors realize?
* What is the key conceptual insight?
* Why should the proposed idea work?
* What tradeoff is being exploited?
* How is the idea different from the obvious or conventional solution?

A technically knowledgeable reader should understand the paper's main idea after reading this section.

---

# 7. Proposed Method

This should be one of the MOST DETAILED sections.

## 7.1 High-Level Pipeline

Describe the complete method from input to output.

Where appropriate:

`Input → Stage 1 → Stage 2 → Stage 3 → Output`

Explain each stage.

---

## 7.2 Components

For every important component describe:

* its purpose,
* its input,
* what operation it performs,
* its output,
* why it is necessary,
* how it interacts with other components.

Clearly distinguish between:

* components inherited from existing methods,
* components introduced by this paper.

---

## 7.3 Architecture

If applicable, explain the architecture in detail.

Potentially include:

* model modules,
* layers,
* encoders,
* decoders,
* attention mechanisms,
* routing,
* retrieval,
* memory,
* parallelism,
* system components,
* information flow.

Explain how the pieces connect.

---

## 7.4 Important Equations

Extract only equations that are important for understanding:

* the proposed method,
* novelty,
* theoretical argument,
* loss/objective,
* complexity,
* or important analysis.

For each important equation:

1. provide the equation or mathematical relationship;
2. define each important variable;
3. explain what the equation computes;
4. explain the intuition;
5. explain why it matters;
6. explain how it differs from a conventional formulation, when relevant.

Do not spend large amounts of space on routine equations unrelated to the paper's contribution.

---

## 7.5 Training Procedure

If applicable, explain:

* training data,
* preprocessing,
* objective,
* loss functions,
* initialization,
* optimizer,
* learning-rate strategy,
* fine-tuning,
* uptraining,
* distillation,
* sampling,
* regularization,
* important hyperparameters,
* compute requirements.

Do not infer details that the paper does not report.

---

## 7.6 Inference Procedure

Explain what happens during inference.

Include when relevant:

* decoding,
* retrieval,
* caching,
* routing,
* batching,
* sampling,
* parallelism,
* memory behavior,
* latency,
* throughput.

---

## 7.7 Computational and Systems Characteristics

If relevant, explain:

* time complexity,
* space complexity,
* FLOPs,
* memory usage,
* memory bandwidth,
* communication cost,
* latency,
* throughput,
* scaling characteristics.

Explain the bottleneck the paper is trying to solve and how the proposed approach changes it.

---

# 8. What Is Actually Novel?

Separate genuine novelty from existing building blocks.

For every major contribution explain:

### What existed before

### What this paper changes

### Why that change matters

Classify the contribution, when useful, as:

* architecture,
* algorithm,
* training method,
* inference method,
* systems optimization,
* theory,
* dataset,
* benchmark,
* evaluation methodology,
* empirical finding,
* analysis,
* combination of existing techniques.

Do not treat every component used by the system as novel.

---

# 9. Experimental Setup

Explain the experimental methodology thoroughly.

## Models

Include:

* names,
* architectures,
* parameter counts,
* variants,
* important configurations.

## Datasets / Benchmarks

For each important dataset:

* purpose,
* task,
* size if given,
* domain,
* train/validation/test setup,
* important characteristics.

## Baselines

For each important baseline:

* what it is,
* why it is an appropriate comparison,
* how it differs from the proposed method.

## Metrics

Explain:

* metric,
* what it measures,
* whether higher/lower is better,
* why it is relevant.

## Implementation Details

Include important:

* hardware,
* software,
* batch sizes,
* learning rates,
* sequence lengths,
* context lengths,
* number of runs,
* random seeds,
* training configurations,
* inference configurations.

Report only what is actually stated.

---

# 10. Main Experiments and Results

Organize experiments according to the scientific question they answer.

For each major experiment provide:

### Research Question

What is being tested?

### Setup

What methods, models, datasets, or conditions are compared?

### Result

What happened?

### Quantitative Evidence

Preserve the most important numerical results.

### Authors' Interpretation

What conclusion do the authors draw?

### Evidence Assessment

Does the experiment convincingly support that conclusion?

Explain why.

Reference important:

* pages,
* tables,
* figures,
* sections.

Do not merely reproduce table contents.

Explain what the results mean.

---

# 11. Ablation Studies

For each meaningful ablation explain:

* What component or variable is modified?
* What is the baseline configuration?
* What is changed?
* Why was the ablation performed?
* What happens?
* What is the quantitative effect?
* What does this reveal about the method?

Identify which components appear most responsible for the gains.

Do not confuse ordinary baseline comparisons with ablations.

---

# 12. Scaling and Sensitivity

If present, explain experiments varying:

* model size,
* dataset size,
* context length,
* compute,
* batch size,
* number of heads,
* number of groups,
* hyperparameters,
* retrieval size,
* hardware configuration,
* or other important variables.

For each, explain the observed TREND.

For example:

> Performance improves from 1B to 7B parameters but shows little additional improvement after 7B.

Do not merely list values.

If the paper does not meaningfully study scaling or sensitivity, state that.

---

# 13. Important Figures and Tables

Identify only the figures/tables most important for understanding the paper.

For each:

* What is shown?
* What comparison matters?
* What pattern is visible?
* What conclusion follows?
* Why is this important?

Include page and identifier when available.

---

# 14. Key Findings

Separate findings into:

## Main Findings

Results directly supporting the central contribution.

## Secondary Findings

Useful observations not central to the main claim.

## Interesting or Unexpected Findings

Observations that are:

* surprising,
* counterintuitive,
* unexplained,
* inconsistent with an assumption,
* revealing about scaling,
* revealing about failure behavior,
* or potentially useful for future research.

Explain WHY each interesting finding matters.

This section is especially important for downstream research discovery.

---

# 15. Failure Cases

If the paper discusses meaningful failures, explain:

* what fails,
* under what conditions,
* what behavior is observed,
* possible causes if supported,
* implications.

If the paper does not investigate failure cases, explicitly state that.

Do not invent failure cases.

---

# 16. Limitations

Carefully separate two categories.

## 16.1 Author-Stated Limitations

Include ONLY limitations explicitly acknowledged by the authors.

For each:

* describe the limitation,
* explain its consequence,
* provide source location.

## 16.2 Additional Potential Limitations

Identify reasonable limitations based on evidence in the paper.

Possible examples:

* limited datasets,
* limited domains,
* limited model scale,
* narrow evaluation,
* missing baselines,
* missing ablations,
* unrealistic assumptions,
* compute cost,
* memory cost,
* weak statistical evidence,
* sensitivity,
* reproducibility,
* missing generalization tests.

For each inferred limitation use:

**Observation → Why this may be a limitation → Supporting evidence**

Clearly label these as your analysis.

Do not invent criticism merely to fill this section.

An empty list of additional limitations is acceptable.

---

# 17. Open Questions and Future Research

This section is especially important.

Separate:

## 17.1 Future Work Explicitly Proposed by the Authors

Include only future directions the authors actually mention.

For each:

* direction,
* motivation,
* source location.

## 17.2 Inferred Research Opportunities

Identify potentially important unanswered questions arising from concrete evidence in the paper.

Possible origins include:

* author-stated limitation,
* inferred limitation,
* failure case,
* unexplained result,
* surprising result,
* contradictory observation,
* missing experiment,
* narrow evaluation,
* scaling behavior,
* strong assumption,
* observed tradeoff.

For every research opportunity provide:

### Observation / Limitation

What specific result or limitation motivates the question?

### Research Question

Phrase it as a precise scientific question.

### Why It Matters

Why would answering it improve scientific understanding or practical capability?

### Possible Investigation

When reasonable, describe how the question might be experimentally studied.

### Confidence

State whether the opportunity appears:

* high confidence,
* medium confidence,
* low confidence.

Avoid generic ideas.

BAD:

> Use a bigger model.

BAD:

> Improve efficiency.

BAD:

> Test on more datasets.

BETTER:

> The method uses a fixed KV-head grouping configuration across all transformer layers. Does the optimal grouping vary by layer depth because attention-head specialization differs across layers?

Do not force research opportunities when the evidence does not support them.

---

# 18. Claims vs Evidence

Identify the paper's major claims.

For each:

### Claim

State the claim precisely.

### Evidence

What experiment, theorem, analysis, table, or figure supports it?

### Source

Page / Section / Table / Figure / Equation.

### Evidence Strength

Choose:

* **Strong**
* **Moderate**
* **Weak**

### Reasoning

Explain why the evidence deserves that rating.

Evaluate evidence relative to the breadth of the claim.

A strong experiment can still provide weak support for an overly broad claim.

---

# 19. Reproducibility Notes

Imagine you want to reproduce the paper.

Extract:

* datasets,
* preprocessing,
* architecture,
* training procedure,
* objectives/losses,
* optimization,
* hyperparameters,
* inference settings,
* evaluation methodology,
* hardware,
* software,
* checkpoints,
* external dependencies.

Then separately provide:

## Missing or Underspecified Information

List reproduction-critical information that appears absent or insufficiently specified.

---

# 20. Final Technical Assessment

Conclude with:

## What the Paper Demonstrates

State conclusions actually supported by the evidence.

## What the Paper Does NOT Demonstrate

Define important boundaries of the evidence.

For example:

* not tested above a certain model scale,
* not evaluated on long-context workloads,
* not evaluated outside English,
* no real production latency measurements,
* no evidence for a broader generalization claim.

Do not invent arbitrary criticisms.

## Why the Paper Matters

Explain its significance within the research area.

## Single Most Important Technical Insight

State the one idea worth remembering.

## Most Important Experimental Result

Identify the result providing the strongest support for the paper.

## Most Important Limitation

Identify the limitation most constraining the conclusions.

## Most Interesting Unanswered Question

Identify the research question most naturally suggested by the paper.

---

# STYLE AND CHUNKABILITY REQUIREMENTS

Because this summary will later be chunked for retrieval:

1. Use the exact major section headings specified above.

2. Make each section reasonably self-contained.

3. When referring to another section, briefly restate enough context that the current section remains understandable after chunking.

4. Avoid vague references such as:

   * "as mentioned above,"
   * "the previous result,"
   * "this technique,"
     when the referent would become unclear if the section were retrieved independently.

Instead prefer explicit references such as:

> The grouped-query attention mechanism described in Section 7...

5. Use the paper's canonical technical terminology consistently.

6. Preserve names of:

   * methods,
   * models,
   * datasets,
   * benchmarks,
   * metrics,
   * algorithms.

7. Avoid unnecessary rhetorical language.

8. Avoid repeating the same explanation across multiple sections unless the repetition is necessary for a section to remain independently understandable.

9. Preserve important numerical results.

10. Do not intentionally shorten the summary to save tokens.

The final document should function as:

* an exhaustive technical summary,
* a study guide,
* and a high-quality retrieval corpus for later semantic search and RAG.


"""


def summarize(folder: Path, *,
              provider: str | None = None,
              model: str | None = None) -> str:
    provider = provider or os.environ.get("SUMMARY_PROVIDER")
    model = model or os.environ.get("SUMMARY_MODEL")
    client = LLMClient(provider)
    print(f"Provider: {client.provider} | model: {model or client.default_model}")

    document = stitch_pages(folder)
    print(f"Stitched {document.count('<PAGE')} pages "
          f"({len(document)} chars); querying LLM...")

    raw = client.chat(messages=[{"role": "user", "content": PROMPT + document}],
                      model=model)
    if not raw or not raw.strip():
        raise RuntimeError("Empty response from the model.")
    return _clean(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder",
                        help="Paper folder with per-page markdown (01.md, ...)")
    parser.add_argument("--out", default=None,
                        help="Output path (default: <folder>/summary.md)")
    parser.add_argument("--provider", default=None,
                        help="LLM provider (default: SUMMARY_PROVIDER or PROVIDER)")
    parser.add_argument("--model", default=None,
                        help="Model (default: SUMMARY_MODEL or provider's model)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    summary = summarize(folder, provider=args.provider, model=args.model)

    out_path = Path(args.out) if args.out else folder / "summary.md"
    out_path.write_text(summary + "\n")
    print(f"Wrote {out_path} ({len(summary)} chars)")


if __name__ == "__main__":
    main()
