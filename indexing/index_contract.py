"""Index contract: where every PaperAnalysis leaf field goes.

The retrieval index exists to *find* papers; the full paper.json (on S3,
keyed by paper_id) exists to *reason* about them. Every leaf field in the
extraction schema is therefore assigned to exactly one primary bucket:

    TEXT      goes into a record's text -> embedded + BM25. The record type
              says which kind of statement it becomes (one vector per
              statement; see flatten.py).
    META      structured field on the record or the paper doc -> filters,
              facets, boosts (year, venue, severity, dataset names, ...).
              Never embedded, but still query-addressable.
    ONDEMAND  not in the index at all. Read from paper.json by paper_id
              after retrieval (hyperparameter tables, equations, result
              rows, reproduction steps). Nobody phrases a search against
              these, but every downstream stage can reach them in one hop.

A TEXT field may additionally be listed in ALSO_META when the same value is
useful as a facet (e.g. dataset names are embedded inside a `dataset` record
*and* kept as a keyword array for filtering).

Run `uv run python -m indexing.index_contract` to verify that the contract
covers the schema exactly (no missing, no stale paths) and print the bucket
counts and per-type table. flatten.py is expected to implement this file;
the check is the coverage test.
"""

from __future__ import annotations

import sys
from typing import get_args

TEXT, META, ONDEMAND = "text", "meta", "ondemand"

# Every record carries this envelope regardless of type. `title` is
# prepended to the text of every record so that a statement retrieved on its
# own still identifies its paper to the embedding model and to the reader.
RECORD_ENVELOPE = {
    "record_id": "<paper_id>#<type>/<n>[#p<k>]",
    "paper_id": "canonical id, joins to papers.jsonl and to S3 paper.json",
    "type": "record type (see RECORD_TYPES)",
    "text": "'<title>. ' + labelled statement (what is embedded / BM25'd)",
    "source_locations": "evidence pointers into the markdown (not indexed)",
    "meta": "denormalised filters: year, venue, research_areas, + per-type "
            "record META fields listed below",
}

# path -> (bucket, target)
#   TEXT     target = record type the field's text lands in
#   META     target = "paper" (papers doc) or "record" (on the record)
#   ONDEMAND target = None
CONTRACT: dict[str, tuple[str, str | None]] = {
    # ---- paper_metadata --------------------------------------------------
    "paper_metadata.title": (TEXT, "*"),           # prefixed to every record
    "paper_metadata.authors": (META, "paper"),
    "paper_metadata.year": (META, "paper"),
    "paper_metadata.venue": (META, "paper"),
    "paper_metadata.paper_type": (META, "paper"),
    "paper_metadata.identifiers[].name": (META, "paper"),    # arxiv/doi lookup
    "paper_metadata.identifiers[].value": (META, "paper"),
    "paper_metadata.research_areas": (META, "paper"),
    "paper_metadata.keywords": (TEXT, "tags"),

    # ---- high_level_summary -> one `summary` record ------------------------
    "high_level_summary.one_sentence_summary": (TEXT, "summary"),
    "high_level_summary.general_problem": (TEXT, "summary"),
    "high_level_summary.specific_problem": (TEXT, "summary"),
    "high_level_summary.proposed_solution": (TEXT, "summary"),
    "high_level_summary.main_result": (TEXT, "summary"),
    "high_level_summary.why_it_matters": (TEXT, "summary"),

    # ---- research_problem --------------------------------------------------
    "research_problem.general_problem": (TEXT, "problem"),
    "research_problem.specific_problem": (TEXT, "problem"),
    "research_problem.motivation": (TEXT, "problem"),
    "research_problem.why_difficult": (TEXT, "problem"),
    "research_problem.research_questions": (TEXT, "research_question"),  # 1 rec / item
    "research_problem.hypotheses": (TEXT, "hypothesis"),                 # 1 rec / item

    # ---- research_gap ------------------------------------------------------
    "research_gap.gap_description": (TEXT, "research_gap"),
    "research_gap.what_existed_before": (TEXT, "research_gap"),
    "research_gap.limitations_of_prior_work": (TEXT, "research_gap"),
    "research_gap.why_existing_methods_are_insufficient": (TEXT, "research_gap"),
    "research_gap.evidence_for_gap": (TEXT, "research_gap"),
    "research_gap.gap_explicitly_claimed_by_authors": (META, "record"),

    # ---- prior_work[] ------------------------------------------------------
    "prior_work[].method_or_family": (TEXT, "prior_work"),
    "prior_work[].description": (TEXT, "prior_work"),
    "prior_work[].strengths": (TEXT, "prior_work"),
    "prior_work[].limitations": (TEXT, "prior_work"),
    "prior_work[].relationship_to_current_paper": (TEXT, "prior_work"),
    "prior_work[].citations_mentioned_in_paper": (ONDEMAND, None),

    # ---- contributions[] ---------------------------------------------------
    "contributions[].contribution": (TEXT, "contribution"),
    "contributions[].contribution_type": (META, "record"),
    "contributions[].novelty_claim": (TEXT, "contribution"),
    "contributions[].what_existed_before": (TEXT, "contribution"),
    "contributions[].what_this_paper_changes": (TEXT, "contribution"),
    "contributions[].why_it_matters": (TEXT, "contribution"),
    "contributions[].explicitly_claimed_by_authors": (META, "record"),

    # ---- method ------------------------------------------------------------
    "method.high_level_idea": (TEXT, "method"),
    "method.architecture.description": (TEXT, "method"),
    "method.architecture.model_components": (TEXT, "method"),
    "method.architecture.information_flow": (TEXT, "method"),

    "method.pipeline[].step": (TEXT, "pipeline_step"),          # "Step 2:" label
    "method.pipeline[].name": (TEXT, "pipeline_step"),
    "method.pipeline[].description": (TEXT, "pipeline_step"),
    "method.pipeline[].inputs": (TEXT, "pipeline_step"),
    "method.pipeline[].outputs": (TEXT, "pipeline_step"),

    "method.components[].name": (TEXT, "method_component"),
    "method.components[].purpose": (TEXT, "method_component"),
    "method.components[].inputs": (TEXT, "method_component"),
    "method.components[].operations": (TEXT, "method_component"),
    "method.components[].outputs": (TEXT, "method_component"),
    "method.components[].technical_details": (TEXT, "method_component"),
    "method.components[].novel_component": (META, "record"),

    "method.objectives_and_losses[].name": (TEXT, "objective"),
    "method.objectives_and_losses[].equation": (ONDEMAND, None),   # LaTeX
    "method.objectives_and_losses[].description": (TEXT, "objective"),
    "method.objectives_and_losses[].purpose": (TEXT, "objective"),

    "method.important_equations[].equation_identifier": (ONDEMAND, None),
    "method.important_equations[].equation": (ONDEMAND, None),
    "method.important_equations[].variables[].symbol": (ONDEMAND, None),
    "method.important_equations[].variables[].meaning": (ONDEMAND, None),
    "method.important_equations[].meaning": (TEXT, "equation"),    # prose
    "method.important_equations[].importance": (TEXT, "equation"),

    "method.training.training_procedure": (TEXT, "training"),
    "method.training.training_data": (TEXT, "training"),
    "method.training.preprocessing": (TEXT, "training"),
    "method.training.optimization": (TEXT, "training"),
    "method.training.initialization": (TEXT, "training"),
    "method.training.fine_tuning_strategy": (TEXT, "training"),
    "method.training.regularization": (TEXT, "training"),
    "method.training.important_hyperparameters[].name": (ONDEMAND, None),
    "method.training.important_hyperparameters[].value": (ONDEMAND, None),
    "method.training.compute_requirements": (TEXT, "training"),

    "method.inference.procedure": (TEXT, "inference"),
    "method.inference.decoding_or_sampling": (TEXT, "inference"),
    "method.inference.complexity": (TEXT, "inference"),
    "method.inference.latency_considerations": (TEXT, "inference"),
    "method.inference.throughput_considerations": (TEXT, "inference"),
    "method.inference.memory_considerations": (TEXT, "inference"),

    "method.systems_characteristics.time_complexity": (TEXT, "systems"),
    "method.systems_characteristics.space_complexity": (TEXT, "systems"),
    "method.systems_characteristics.flops": (TEXT, "systems"),
    "method.systems_characteristics.memory": (TEXT, "systems"),
    "method.systems_characteristics.memory_bandwidth": (TEXT, "systems"),
    "method.systems_characteristics.communication_cost": (TEXT, "systems"),
    "method.systems_characteristics.latency": (TEXT, "systems"),
    "method.systems_characteristics.throughput": (TEXT, "systems"),
    "method.systems_characteristics.other": (TEXT, "systems"),

    # ---- models[] / datasets[] / baselines[] / metrics[] --------------------
    "models[].name": (TEXT, "model"),
    "models[].role": (TEXT, "model"),
    "models[].architecture": (TEXT, "model"),
    "models[].parameter_count": (TEXT, "model"),
    "models[].configuration[].name": (ONDEMAND, None),
    "models[].configuration[].value": (ONDEMAND, None),
    "models[].pretrained": (ONDEMAND, None),

    "datasets[].name": (TEXT, "dataset"),
    "datasets[].purpose": (TEXT, "dataset"),
    "datasets[].size": (TEXT, "dataset"),
    "datasets[].task": (TEXT, "dataset"),
    "datasets[].domain": (TEXT, "dataset"),
    "datasets[].train_split": (ONDEMAND, None),
    "datasets[].validation_split": (ONDEMAND, None),
    "datasets[].test_split": (ONDEMAND, None),
    "datasets[].important_characteristics": (TEXT, "dataset"),

    "baselines[].name": (TEXT, "baseline"),
    "baselines[].description": (TEXT, "baseline"),
    "baselines[].why_selected": (TEXT, "baseline"),
    "baselines[].relationship_to_proposed_method": (TEXT, "baseline"),

    "metrics[].name": (TEXT, "metric"),
    "metrics[].description": (TEXT, "metric"),
    "metrics[].what_it_measures": (TEXT, "metric"),
    "metrics[].better_direction": (ONDEMAND, None),

    # ---- experimental_setup -> one `evaluation_setup` record ----------------
    "experimental_setup.hardware": (TEXT, "evaluation_setup"),
    "experimental_setup.software": (TEXT, "evaluation_setup"),
    "experimental_setup.training_configuration[].name": (ONDEMAND, None),
    "experimental_setup.training_configuration[].value": (ONDEMAND, None),
    "experimental_setup.inference_configuration[].name": (ONDEMAND, None),
    "experimental_setup.inference_configuration[].value": (ONDEMAND, None),
    "experimental_setup.number_of_runs": (META, "paper"),
    "experimental_setup.random_seeds": (ONDEMAND, None),
    "experimental_setup.statistical_testing": (TEXT, "evaluation_setup"),
    "experimental_setup.other_details": (TEXT, "evaluation_setup"),

    # ---- experiments[] -----------------------------------------------------
    "experiments[].experiment_name": (TEXT, "experiment"),
    "experiments[].research_question": (TEXT, "experiment"),
    "experiments[].hypothesis_being_tested": (TEXT, "experiment"),
    "experiments[].setup": (TEXT, "experiment"),
    "experiments[].comparison": (TEXT, "experiment"),
    "experiments[].datasets": (TEXT, "experiment"),
    "experiments[].models": (TEXT, "experiment"),
    "experiments[].baselines": (TEXT, "experiment"),
    "experiments[].metrics": (TEXT, "experiment"),
    "experiments[].results[].method": (ONDEMAND, None),
    "experiments[].results[].metric": (ONDEMAND, None),
    "experiments[].results[].value": (ONDEMAND, None),
    "experiments[].results[].raw_value": (ONDEMAND, None),
    "experiments[].results[].unit": (ONDEMAND, None),
    "experiments[].results[].dataset_or_setting": (ONDEMAND, None),
    "experiments[].key_result": (TEXT, "experiment"),
    "experiments[].authors_conclusion": (TEXT, "experiment"),
    "experiments[].evidence_supports_conclusion": (META, "record"),

    # ---- ablations[] / scaling[] / key_results[] ---------------------------
    "ablations[].component_or_variable": (TEXT, "ablation"),
    "ablations[].change": (TEXT, "ablation"),
    "ablations[].motivation": (TEXT, "ablation"),
    "ablations[].baseline_setting": (TEXT, "ablation"),
    "ablations[].modified_setting": (TEXT, "ablation"),
    "ablations[].result": (TEXT, "ablation"),
    "ablations[].quantitative_change": (TEXT, "ablation"),
    "ablations[].interpretation": (TEXT, "ablation"),

    "scaling_and_sensitivity[].variable": (TEXT, "scaling"),
    "scaling_and_sensitivity[].values_tested": (TEXT, "scaling"),
    "scaling_and_sensitivity[].observed_trend": (TEXT, "scaling"),
    "scaling_and_sensitivity[].interpretation": (TEXT, "scaling"),

    "key_results[].finding": (TEXT, "key_result"),
    "key_results[].quantitative_result": (TEXT, "key_result"),
    "key_results[].comparison": (TEXT, "key_result"),
    "key_results[].importance": (TEXT, "key_result"),

    # ---- interesting_findings[] / figures / failure_cases[] -----------------
    "interesting_findings[].finding": (TEXT, "interesting_finding"),
    "interesting_findings[].expected_or_unexpected": (META, "record"),
    "interesting_findings[].why_interesting": (TEXT, "interesting_finding"),
    "interesting_findings[].possible_implication": (TEXT, "interesting_finding"),
    "interesting_findings[].authors_discuss_implication": (META, "record"),

    "important_figures_and_tables[].type": (META, "record"),
    "important_figures_and_tables[].identifier": (TEXT, "figure_table"),
    "important_figures_and_tables[].what_it_shows": (TEXT, "figure_table"),
    "important_figures_and_tables[].key_observation": (TEXT, "figure_table"),
    "important_figures_and_tables[].importance": (TEXT, "figure_table"),

    "failure_cases[].failure": (TEXT, "failure_case"),
    "failure_cases[].conditions": (TEXT, "failure_case"),
    "failure_cases[].observed_behavior": (TEXT, "failure_case"),
    "failure_cases[].possible_cause": (TEXT, "failure_case"),
    "failure_cases[].implication": (TEXT, "failure_case"),

    # ---- limitations / future_work -----------------------------------------
    "limitations.author_stated[].limitation": (TEXT, "limitation_author"),
    "limitations.author_stated[].impact": (TEXT, "limitation_author"),
    "limitations.inferred[].limitation": (TEXT, "limitation_inferred"),
    "limitations.inferred[].reasoning": (TEXT, "limitation_inferred"),
    "limitations.inferred[].severity": (META, "record"),
    "limitations.inferred[].evidence": (TEXT, "limitation_inferred"),

    "future_work.author_proposed[].direction": (TEXT, "future_work_author"),
    "future_work.author_proposed[].motivation": (TEXT, "future_work_author"),
    "future_work.inferred_research_opportunities[].observation_or_limitation":
        (TEXT, "research_opportunity"),
    "future_work.inferred_research_opportunities[].research_question":
        (TEXT, "research_opportunity"),
    "future_work.inferred_research_opportunities[].why_it_matters":
        (TEXT, "research_opportunity"),
    "future_work.inferred_research_opportunities[].potential_approach":
        (TEXT, "research_opportunity"),
    "future_work.inferred_research_opportunities[].confidence": (META, "record"),

    # ---- claims_and_evidence[] ---------------------------------------------
    "claims_and_evidence[].claim": (TEXT, "claim"),
    "claims_and_evidence[].claim_type": (META, "record"),
    "claims_and_evidence[].evidence": (TEXT, "claim"),
    "claims_and_evidence[].evidence_strength": (META, "record"),
    "claims_and_evidence[].reasoning": (TEXT, "claim"),

    # ---- reproducibility ---------------------------------------------------
    "reproducibility.code_available": (META, "paper"),
    "reproducibility.code_url": (META, "paper"),
    "reproducibility.data_available": (META, "paper"),
    "reproducibility.data_url": (META, "paper"),
    "reproducibility.model_checkpoints_available": (META, "paper"),
    "reproducibility.required_resources": (TEXT, "reproducibility"),
    "reproducibility.reproduction_steps": (ONDEMAND, None),
    "reproducibility.missing_information": (TEXT, "reproducibility"),
    "reproducibility.reproducibility_assessment": (TEXT, "reproducibility"),

    # ---- paper_assessment -> `paper_card` + `open_question` -----------------
    "paper_assessment.main_strengths": (TEXT, "paper_card"),
    "paper_assessment.main_weaknesses": (TEXT, "paper_card"),
    "paper_assessment.most_important_contribution": (TEXT, "paper_card"),
    "paper_assessment.most_important_result": (TEXT, "paper_card"),
    "paper_assessment.most_important_limitation": (TEXT, "paper_card"),
    "paper_assessment.most_interesting_open_question": (TEXT, "open_question"),
    "paper_assessment.what_the_paper_demonstrates": (TEXT, "paper_card"),
    "paper_assessment.what_the_paper_does_not_demonstrate": (TEXT, "paper_card"),

    # ---- retrieval_tags -> one `tags` record + paper facets ----------------
    "retrieval_tags.problems": (TEXT, "tags"),
    "retrieval_tags.methods": (TEXT, "tags"),
    "retrieval_tags.architectures": (TEXT, "tags"),
    "retrieval_tags.model_families": (TEXT, "tags"),
    "retrieval_tags.training_techniques": (TEXT, "tags"),
    "retrieval_tags.inference_techniques": (TEXT, "tags"),
    "retrieval_tags.optimization_techniques": (TEXT, "tags"),
    "retrieval_tags.datasets": (TEXT, "tags"),
    "retrieval_tags.benchmarks": (TEXT, "tags"),
    "retrieval_tags.metrics": (TEXT, "tags"),
    "retrieval_tags.applications": (TEXT, "tags"),
    "retrieval_tags.hardware_or_system_topics": (TEXT, "tags"),
    "retrieval_tags.theoretical_topics": (TEXT, "tags"),
}

# TEXT fields that are *also* kept as structured keyword facets:
# path -> (scope, field name on the paper row / record meta).
ALSO_META: dict[str, tuple[str, str]] = {
    "paper_metadata.title": ("paper", "title"),
    "paper_metadata.keywords": ("paper", "keywords"),
    "models[].name": ("paper", "model_names"),
    "datasets[].name": ("paper", "dataset_names"),
    "baselines[].name": ("paper", "baseline_names"),
    "metrics[].name": ("paper", "metric_names"),
    "experimental_setup.hardware": ("paper", "hardware"),
    "experiments[].datasets": ("record", "datasets"),   # keyword arrays on
    "experiments[].models": ("record", "models"),       # experiment records
    "experiments[].baselines": ("record", "baselines"),
    "experiments[].metrics": ("record", "metrics"),
    **{f"retrieval_tags.{k}": ("paper", "retrieval_tags") for k in (
        "problems", "methods", "architectures", "model_families",
        "training_techniques", "inference_techniques",
        "optimization_techniques", "datasets", "benchmarks", "metrics",
        "applications", "hardware_or_system_topics", "theoretical_topics")},
}

# Paper-level META leaves -> field name on the paper row (leaf name unless
# listed here).
PAPER_META_FIELD: dict[str, str] = {
    "paper_metadata.identifiers[].name": "identifiers",
    "paper_metadata.identifiers[].value": "identifiers",
}

# Record types, in the order flatten should emit them, with cardinality.
#   "1"      one record per paper
#   "N"      one record per list item
#   "N/item" one record per *string* in a list[str] (each item is a statement)
RECORD_TYPES: dict[str, str] = {
    "summary": "1", "problem": "1", "research_question": "N/item",
    "hypothesis": "N/item", "research_gap": "1", "prior_work": "N",
    "contribution": "N", "method": "1", "pipeline_step": "N",
    "method_component": "N", "objective": "N", "equation": "N",
    "training": "1", "inference": "1", "systems": "1", "model": "N",
    "dataset": "N", "baseline": "N", "metric": "N", "evaluation_setup": "1",
    "experiment": "N", "ablation": "N", "scaling": "N", "key_result": "N",
    "interesting_finding": "N", "figure_table": "N", "failure_case": "N",
    "limitation_author": "N", "limitation_inferred": "N",
    "future_work_author": "N", "research_opportunity": "N", "claim": "N",
    "reproducibility": "1", "paper_card": "1", "open_question": "1",
    "tags": "1",
}


def schema_leaves() -> list[str]:
    """Every leaf path of PaperAnalysis, minus source_locations and
    schema_version, using the same path grammar as CONTRACT."""
    from pydantic import BaseModel

    from extraction.research_extract import PaperAnalysis

    def walk(model, prefix=""):
        out = []
        for name, f in model.model_fields.items():
            ann = f.annotation
            inner = None
            is_list = "list" in str(ann)
            for a in (ann, *get_args(ann)):
                for b in (a, *get_args(a)):
                    if isinstance(b, type) and issubclass(b, BaseModel):
                        inner = b
            path = f"{prefix}{name}" + ("[]" if is_list and inner else "")
            if inner is not None and inner.__name__ != "SourceLocation":
                out += walk(inner, path + ".")
            else:
                out.append(path)
        return out

    return [p for p in walk(PaperAnalysis)
            if "source_locations" not in p and p != "schema_version"]


def check() -> int:
    leaves = set(schema_leaves())
    contract = set(CONTRACT)
    missing = sorted(leaves - contract)
    stale = sorted(contract - leaves)
    bad_types = sorted({t for b, t in CONTRACT.values()
                        if b == TEXT and t != "*" and t not in RECORD_TYPES})
    bad_also = sorted(p for p in ALSO_META if CONTRACT.get(p, (None,))[0] != TEXT)
    ok = not (missing or stale or bad_types or bad_also)

    by_bucket = {TEXT: 0, META: 0, ONDEMAND: 0}
    for b, _ in CONTRACT.values():
        by_bucket[b] += 1
    text_fields = by_bucket[TEXT]
    meta_only = by_bucket[META]
    print(f"schema leaves: {len(leaves)}   contract entries: {len(contract)}")
    print(f"  TEXT      (embedded + BM25):        {text_fields:3d}"
          f"   (+{len(ALSO_META)} of these also kept as META facets)")
    print(f"  META      (filters/facets only):    {meta_only:3d}")
    print(f"  ONDEMAND  (paper.json by paper_id): {by_bucket[ONDEMAND]:3d}")
    print(f"  query-addressable (TEXT + META):    {text_fields + meta_only:3d}")
    print(f"  record types: {len(RECORD_TYPES)}")
    if missing:
        print("\nMISSING from contract:\n  " + "\n  ".join(missing))
    if stale:
        print("\nSTALE in contract (not in schema):\n  " + "\n  ".join(stale))
    if bad_types:
        print("\nTEXT targets not in RECORD_TYPES:\n  " + "\n  ".join(bad_types))
    if bad_also:
        print("\nALSO_META entries that are not TEXT:\n  " + "\n  ".join(bad_also))
    print("\nOK: contract covers the schema exactly" if ok else "\nFAIL")
    return 0 if ok else 1


def synthetic_paper() -> tuple[dict, dict[str, object]]:
    """A PaperAnalysis-shaped dict where every leaf holds a unique sentinel
    (strings: 'S<path>'; bools True; ints 7; floats 1.5; Literals: their
    first value). Returns (paper, {leaf path: value}) so a flatten run can be
    checked field-by-field against CONTRACT."""
    import types
    from typing import Literal, Union

    from pydantic import BaseModel

    from extraction.research_extract import PaperAnalysis

    values: dict[str, object] = {}

    def value(ann, path):
        origin, args = getattr(ann, "__origin__", None), get_args(ann)
        if origin is Union or isinstance(ann, types.UnionType):
            for a in args:
                if a is not type(None):
                    return value(a, path)
        if origin is Literal:
            values[path] = args[0]
            return args[0]
        if origin is list:
            inner = args[0]
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return [build(inner, path + "[].")]
            v = [f"S<{path}>"]
            values[path] = v
            return v
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return build(ann, path + ".")
        v = {str: f"S<{path}>", bool: True, int: 7, float: 1.5}[ann]
        values[path] = v
        return v

    def build(model, prefix=""):
        return {name: value(f.annotation, prefix + name)
                for name, f in model.model_fields.items()}

    paper = build(PaperAnalysis)
    return paper, values


def verify_flatten() -> int:
    """Run flatten.flatten_paper on the synthetic paper and check that every
    leaf lands where CONTRACT says: TEXT sentinels inside a record of the
    right type, META on the record meta / paper row, ONDEMAND nowhere."""
    import json

    from . import flatten

    paper, values = synthetic_paper()
    records = flatten.flatten_paper(paper, "PID")
    row = flatten._paper_row("PID", paper, None, "test")
    row_dump = json.dumps(row)
    by_type: dict[str, list[dict]] = {}
    for r in records:
        by_type.setdefault(r["type"], []).append(r)
    all_text = "\n".join(r["text"] for r in records)
    all_meta = json.dumps([r["meta"] for r in records])

    def sentinel(path):
        v = values[path]
        return v[0] if isinstance(v, list) else str(v)

    def section_types(path):
        parent = path.rsplit(".", 1)[0]
        return {t for p, (b, t) in CONTRACT.items()
                if b == TEXT and t != "*" and p.rsplit(".", 1)[0] == parent}

    problems = []
    for path, (bucket, target) in CONTRACT.items():
        s = sentinel(path)
        leaf = path.split(".")[-1]
        if bucket == TEXT:
            recs = records if target == "*" else by_type.get(target, [])
            if not any(s in r["text"] for r in recs):
                problems.append(f"TEXT  {path}: not in any `{target}` record text")
            if target == "*" and not all(r["text"].startswith(s) for r in records):
                problems.append(f"TEXT  {path}: not prefixed on every record")
        elif bucket == META and target == "record":
            recs = [r for t in section_types(path) for r in by_type.get(t, [])]
            if not any(r["meta"].get(leaf) == values[path] for r in recs):
                problems.append(f"META  {path}: `{leaf}` missing from record meta "
                                f"of {sorted(section_types(path))}")
        elif bucket == META:
            field = PAPER_META_FIELD.get(path, leaf)
            if field not in row:
                problems.append(f"META  {path}: `{field}` missing from paper row")
            elif isinstance(values[path], (str, list)) and s not in json.dumps(row[field]):
                problems.append(f"META  {path}: value not in paper row `{field}`")
        else:  # ONDEMAND: must not leak into the index
            if isinstance(values[path], (str, list)) and (
                    s in all_text or s in all_meta or s in row_dump):
                problems.append(f"ONDEMAND {path}: leaked into the index")

    for path, (scope, field) in ALSO_META.items():
        s = sentinel(path)
        if scope == "paper":
            if s not in json.dumps(row.get(field)):
                problems.append(f"FACET {path}: not in paper row `{field}`")
        else:
            recs = [r for t in section_types(path) for r in by_type.get(t, [])]
            if not any(s in json.dumps(r["meta"].get(field)) for r in recs):
                problems.append(f"FACET {path}: not in record meta `{field}`")

    emitted = set(by_type)
    for t in RECORD_TYPES:
        if t not in emitted:
            problems.append(f"TYPE  `{t}` declared but flatten emitted none")
    for t in emitted - set(RECORD_TYPES):
        problems.append(f"TYPE  `{t}` emitted but not declared in RECORD_TYPES")

    print(f"synthetic paper -> {len(records)} records, {len(emitted)} types")
    if problems:
        print("\n".join(problems))
        print(f"\nFAIL: {len(problems)} problems")
        return 1
    print("OK: flatten implements the contract")
    return 0


def markdown_table() -> str:
    """Per-record-type table for docs/index_contract.md."""
    rows = ["| record type | per paper | text fields | record META |", "|---|---|---|---|"]
    for rtype, card in RECORD_TYPES.items():
        fields = [p for p, (b, t) in CONTRACT.items() if b == TEXT and t == rtype]
        # META fields that live on this record type: same section prefix
        prefixes = {p.rsplit(".", 1)[0] for p in fields}
        rmeta = [p for p, (b, t) in CONTRACT.items()
                 if b == META and t == "record" and p.rsplit(".", 1)[0] in prefixes]
        short = lambda p: p.split(".")[-1]  # noqa: E731
        rows.append(f"| `{rtype}` | {card} | "
                    f"{', '.join(short(p) for p in fields)} | "
                    f"{', '.join(short(p) for p in rmeta) or '—'} |")
    return "\n".join(rows)


def bucket_listing(bucket: str) -> str:
    return "\n".join(f"- `{p}`" for p, (b, _) in CONTRACT.items() if b == bucket)


if __name__ == "__main__":
    if "--table" in sys.argv:
        print(markdown_table())
    elif "--verify-flatten" in sys.argv:
        sys.exit(check() or verify_flatten())
    elif "--ondemand" in sys.argv:
        print(bucket_listing(ONDEMAND))
    elif "--meta" in sys.argv:
        print(bucket_listing(META))
    else:
        sys.exit(check())
