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
- `opportunity_miner.md` — implemented section 6 contract, concurrent proposal
  and original-page review, usage, and live validation limits
- `opportunity_validation.md` — broader live quality checks and observed failures
- `opportunity_review_fixes.md` — correction vetoes, support/context roles, and live regression results
