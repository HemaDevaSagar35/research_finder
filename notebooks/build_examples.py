"""Build offline teaching notebooks and fixtures from an existing comparison checkpoint.

Run explicitly with a checkpoint path; never calls a model or changes run artifacts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from extraction.research_extract import _parse_json
from novelty.comparison_records import EvidenceReport, EvidenceReview
from novelty.comparison_schemas import Passage
from novelty.comparison_workflow import validate_evidence_report, eligible


SETUP = '''from pathlib import Path
import sys
import json
from pprint import pprint

# Works when opened from the repository, notebooks/, or its parent directory.
root = next(p for base in [Path.cwd(), *Path.cwd().parents]
            for p in [base, base / "research_finder"]
            if (p / "novelty/comparison_workflow.py").exists())
sys.path.insert(0, str(root))
# Check the selected kernel before importing project modules.
import importlib.util
missing = [name for name in ("openai", "pydantic", "pymupdf")
           if importlib.util.find_spec(name) is None]
if missing:
    raise RuntimeError(
        f"Kernel {sys.executable} is missing: {', '.join(missing)}. "
        f"In VS Code choose Select Kernel > Python Environments > {root / '.venv/bin/python'}, "
        "then restart the kernel and Run All."
    )
from notebooks.example_helpers import load_example, expect_failure
from extraction.research_extract import _parse_json
from novelty.comparison_records import EvidenceReport, EvidenceRecord, EvidenceReview
from novelty.comparison_schemas import Passage
from novelty.comparison_workflow import validate_evidence_report, validate_record, eligible, scope_citation_gaps
from novelty.schemas import TargetSignature
'''


def notebook(title, intro, fixture, cells):
    content = [("markdown", f"# {title}\n\n{intro}\n\nRun cells from top to bottom. Everything runs offline; no API key or model request is used."),
               ("code", SETUP),
               ("code", f'example = load_example(root, "{fixture}")\npprint(example["provenance"])')]
    content += cells
    result = {"cells": [], "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.10"}}, "nbformat": 4, "nbformat_minor": 5}
    for i, (kind, text) in enumerate(content):
        cell = {"cell_type": kind, "id": f"cell-{i:02d}", "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == "code": cell.update(execution_count=None, outputs=[])
        result["cells"].append(cell)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint", type=Path)
    args = parser.parse_args()
    raw = args.checkpoint.read_bytes()
    candidates = json.loads(raw)["result"]["candidates"]
    out = ROOT / "notebooks"
    (out / "examples").mkdir(exist_ok=True)
    fixtures = {}
    for ci, candidate in enumerate(candidates):
        for pi, paper in enumerate(candidate["papers"]):
            base = {"run": args.checkpoint.parent.name, "checkpoint": "comparison.json", "checkpoint_sha256": hashlib.sha256(raw).hexdigest(), "direction_id": candidate["direction_id"], "paper_id": paper["paper_id"]}
            for ri, review in enumerate(paper["evidence_reviews"]):
                for fi, repair in enumerate(review["format_repairs"]):
                    value = repair["invalid_output"]
                    try:
                        parsed = _parse_json(value) if isinstance(value, str) else value
                    except (ValueError, TypeError):
                        category = "01_invalid_json" if "Extra data" in repair["validation_error"] else None
                    else:
                        try:
                            report = EvidenceReport.model_validate(parsed)
                        except ValueError:
                            category = "02_schema_violations"
                        else:
                            try:
                                audit = EvidenceReview.model_validate(review)
                                validate_evidence_report(report, audit.record, [Passage.model_validate(p) for p in paper["passages"]], require_scope=True)
                            except ValueError as exc:
                                category = "03_invalid_passage_references" if "unknown/wrong-paper" in str(exc) else None
                            else:
                                category = None
                    if category and category not in fixtures:
                        fixtures[category] = {"provenance": {**base, "authenticity": "actual saved failed review response; may be an intermediate attempt", "source_pointer": f"/result/candidates/{ci}/papers/{pi}/evidence_reviews/{ri}/format_repairs/{fi}"}, "invalid_output": value, "recorded_error": repair["validation_error"], "record": review["record"], "passages": paper["passages"]}
            if paper["paper_id"] == "4b9a4c446e2f":
                fixtures["04_unsupported_direct_evidence"] = {"provenance": {**base, "authenticity": "reconstructed teaching response: original rejected extraction response was not saved", "source_pointer": f"/result/candidates/{ci}/papers/{pi}"}, "recorded_error": paper["diagnostic"], "target": paper["targets"][0], "passage": paper["passages"][0]}
            if paper["paper_id"] == "dd5034eb29fd":
                review = paper["evidence_reviews"][-1]
                audit = EvidenceReview.model_validate(review)
                assert audit.report and len(eligible(audit)[0]) < len(audit.record.relationships)
                fixtures["05_unresolved_evidence_review"] = {"provenance": {**base, "authenticity": "actual final saved evidence review", "source_pointer": f"/result/candidates/{ci}/papers/{pi}/evidence_reviews/{len(paper['evidence_reviews'])-1}"}, "review": review, "passages": paper["passages"], "recorded_error": paper["diagnostic"]}
    assert len(fixtures) == 5, fixtures.keys()
    for name, value in fixtures.items():
        (out / "examples" / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")

    specs = [
        ("01_invalid_json", "1. Invalid JSON", "The response cannot be decoded as a JSON object. This example contains an extra closing brace after an otherwise decodable object.", [
            ("markdown", "## Inspect the saved output\nThe complete original response is in `example['invalid_output']`. We display its ending because the response is long."),
            ("code", 'raw = example["invalid_output"]\nprint("Recorded error:", example["recorded_error"])\nprint("Response ending:", repr(raw[-250:]))'),
            ("markdown", "## Reproduce the production parser failure"),
            ("code", 'error = expect_failure(lambda: _parse_json(raw), "Extra data")\nfirst_object, end = json.JSONDecoder().raw_decode(raw.strip())\nprint("Trailing text after first JSON object:", repr(raw.strip()[end:]))'),
            ("markdown", "The parser never reaches schema or scientific checks. Decoding the first object above is a diagnostic only: it does not establish that the object meets the schema or contains valid evidence. This saved attempt could have been followed by a repair; it is not a count of unique failed papers.")]),
        ("02_schema_violations", "2. Schema violations", "JSON can parse successfully while its fields or values violate the required response schema.", [
            ("code", 'raw = example["invalid_output"]\nparsed = _parse_json(raw) if isinstance(raw, str) else raw\nprint("JSON parsed. Top-level keys:", list(parsed))\nschema = EvidenceReport.model_json_schema()\nprint("Allowed scope aspects:")\npprint(schema["$defs"]["ScopeCheck"]["properties"]["aspect"])'),
            ("markdown", "## Run the same Pydantic schema check\nThe error location identifies the exact nested field. `literal_error` means an unsupported enum value; `extra_forbidden` means an unexpected field. Inspect `schema` for the complete contract."),
            ("code", 'error = expect_failure(lambda: EvidenceReport.model_validate(parsed))\nassert hasattr(error, "errors")\nfor issue in error.errors():\n    pprint({key: issue[key] for key in ("loc", "type", "input", "msg")})'),
            ("markdown", "No scientific conclusion follows from this failure. The response cannot yet be consumed under the declared schema. Changing an enum blindly can change meaning; a valid replacement needs to preserve the intended assertion.")]),
        ("03_invalid_passage_references", "3. Invalid passage references", "Here the JSON and schema pass, but at least one cited passage ID is absent from the supplied paper passages.", [
            ("code", 'raw = example["invalid_output"]\nreport = EvidenceReport.model_validate(_parse_json(raw) if isinstance(raw, str) else raw)\nrecord = EvidenceRecord.model_validate(example["record"])\npassages = [Passage.model_validate(p) for p in example["passages"]]\nprint("JSON and schema checks passed.")'),
            ("code", 'error = expect_failure(lambda: validate_evidence_report(report, record, passages, require_scope=True), "unknown/wrong-paper")\nknown = {p.passage_id for p in passages}\nreferences = {pid for check in report.claim_checks + report.coverage_checks for pid in check.passage_ids}\nreferences |= {pid for claim in report.claim_checks for check in claim.scope_checks or [] for pid in check.passage_ids}\nprint("Unknown references:")\npprint(sorted(references - known))\nprint("Example available passage:")\npprint(passages[0].model_dump())'),
            ("markdown", "An ID-shaped string is not enough: it must identify an actual supplied passage. This prevents nonexistent or wrong-paper evidence from being accepted. A real passage ID alone still does not prove that the text supports the claim.")]),
        ("04_unsupported_direct_evidence", "4. Unsupported direct evidence", "The saved diagnostic says direct coverage did not establish all proposed components. The original rejected response was not retained. The example below deliberately reconstructs that condition using a saved target and passage ID; its claim is illustrative, not a quotation or scientific finding.", [
            ("code", 'print("Actual recorded failure:", example["recorded_error"])\ntarget = TargetSignature.model_validate(example["target"])\npassage = Passage.model_validate(example["passage"])\nprint("Proposed intervention:")\npprint([f.model_dump() for f in target.intervention])'),
            ("markdown", "## Construct a schema-valid but unjustified direct-coverage declaration\nThe example supplies a result claim but no mapping from the prior work to the proposed intervention, comparator, regime and effect."),
            ("code", 'record = EvidenceRecord.model_validate({\n    "claims": [{"claim_id": "demo-result", "text": "Illustrative result only; not asserted to be supported by this passage.", "kind": "result", "passage_ids": [passage.passage_id]}],\n    "relationships": [{"target_id": target.target_id, "intervention": [], "comparator": [], "conditions": [], "outcome": [], "conclusion": ["demo-result"], "coverage": "direct_empirical", "result": "supports_prediction", "inference": None, "proposal_matches": [], "explanation": "Deliberately incomplete teaching example."}]\n})\nerror = expect_failure(lambda: validate_record(record, [target], [passage], require_alignment=True), "requires all proposed")'),
            ("markdown", "The schema accepts the structure, but the relationship check rejects the unsupported declaration. Related background evidence cannot automatically establish that the proposed experiment was tested. This deterministic check verifies mapping completeness; independent evidence review must still judge whether each mapping is scientifically supported.")]),
        ("05_unresolved_evidence_review", "5. Unresolved after evidence review", "This saved review has structured judgments, but some targets remain ineligible because their required evidence has unresolved dependencies. We replay the final saved eligibility decision without asking a model to review it again.", [
            ("code", 'review = EvidenceReview.model_validate(example["review"])\nprint("Saved diagnostic:", example["recorded_error"])\nprint("Review summary:", review.report.summary)\nallowed, supported_claims = eligible(review)\ngaps = scope_citation_gaps(review.report, review.record)\nprint("Eligible targets:", sorted(allowed))\nchecks = {c.claim_id: c for c in review.report.claim_checks}\ncoverage = {c.target_id: c for c in review.report.coverage_checks}\nfor relationship in review.record.relationships:\n    pprint({"target": relationship.target_id, "coverage_review": coverage[relationship.target_id].decision, "eligible": relationship.target_id in allowed, "scope_citation_gaps": {cid: gaps[cid] for cid in sorted(relationship.refs()) if cid in gaps}, "unsupported_claims": {cid: checks[cid].decision for cid in sorted(relationship.refs()) if checks[cid].decision != "supported"}})\nassert len(allowed) < len(review.record.relationships)'),
            ("markdown", "## Inspect one actual blocking claim and its passages\nAn `adequate` coverage judgment is only one requirement. Every claim used by that target must also be supported and have its scope evidence attached."),
            ("code", 'blocked = next(r for r in review.record.relationships if r.target_id not in allowed)\ncid = next((cid for cid in sorted(blocked.refs()) if cid in gaps or checks[cid].decision != "supported"), None)\nif cid is not None:\n    claim = next(c for c in review.record.claims if c.claim_id == cid)\n    pprint(claim.model_dump())\n    pprint(checks[cid].model_dump())\n    ids = set(claim.passage_ids) | set(gaps.get(cid, []))\n    for passage in example["passages"]:\n        if passage["passage_id"] in ids:\n            pprint(passage)\nelse:\n    pprint(coverage[blocked.target_id].model_dump())'),
            ("markdown", "Unresolved means this comparison cannot yet supply an accepted judgment. It does not mean the hypothesis was disproved or already studied. This notebook replays recorded model judgments and deterministic gates; it does not independently establish whether those judgments are correct.")]),
    ]
    for name, title, intro, cells in specs:
        (out / f"{name}.ipynb").write_text(json.dumps(notebook(title, intro, name, cells), indent=2) + "\n")
    print("Built five offline notebooks and provenance-bearing fixtures.")


if __name__ == "__main__":
    main()
