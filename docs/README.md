# Research Path Generator — Refined V1

This version incorporates the design refinements around:

- concrete landscape construction;
- faceted grouping;
- finding aggregation;
- relation extraction;
- cross-paper reasoning using the actual source papers;
- opportunity mining;
- multiple hypotheses per direction;
- novelty signatures;
- a second RAG over the full 2026 corpus;
- candidate-vs-prior-work comparison;
- direction-level and hypothesis-level novelty;
- candidate refinement and ranking.

Files:

- `research_path_generator_architecture_refined.md`
- `research_path_generator_components_refined.md`
- `offline_ingestion_design.md` — concrete decisions and status for the
  offline/ingestion stage (paper_id scheme, paper.json flattening, index
  layout, portability)

## Module documentation

One Markdown document per module contains its approach, contracts, usage,
implementation history and validation results. Existing descriptions are
preserved; dated change sections identify what supersedes them. Shared
architecture and the project status/index remain separate cross-module documents.

- [Landscape Builder](landscape_builder.md) — contributor's existing module;
  our contract, provenance, attribution and concurrency modifications.
- [Cross-Paper Reasoner](cross_paper_reasoner.md) — existing reasoner; shared
  input integration, attribution, budget changes and historical live validation.
- [Opportunity Miner](opportunity_miner.md) — new module first implemented in
  `847b910`, subsequent review fixes, full-context proposals and all test reports.
- [Shared LLM client](llm_client.md) — existing provider client and the new
  configurable output cap.

- [Direction Generator](direction_generator.md) — new architecture sections 7–8
  module; coupled directions, hypotheses and cheapest-first experiment proposals,
  reference validation, implementation history and tests.

The previously separate integration notes, miner validation/fix reports and
cross-module implementation log have been consolidated into these module
files. Source reports are retained as dated history within their owning module;
links now point to those sections. Future updates go into the same module file.

- [Novelty Search](novelty_search.md) — evidence-backed direction/hypothesis signatures, independent fidelity review, full-corpus retrieval and semantic reranking; novelty comparison remains downstream.

- [Research Critic](research_critic.md) — independent scientific critique, refinement, and revalidation handoffs.
- [Final portfolio](final_portfolio.md) — architecture sections 16–17: source-preserving candidate reports, explicit ranking and portfolio selection.
