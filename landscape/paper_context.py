"""Load a paper's evidence for the Landscape Builder: S3 paper.json -> PaperCard.

This is the "Paper and Evidence Loading" step of the query-to-evidence
foundation (docs/research_path_generator_architecture_refined.md sec 2.4).
Retrieval (indexing.search) already returns ranked, deduplicated paper_ids;
this module turns each paper_id into a compact PaperCard by field projection
over the existing paper.json (PaperAnalysis schema), per the mapping table in
docs/offline_ingestion_design.md. No new LLM extraction happens here.

Library usage:
    from landscape.paper_context import load_paper_context

    ctx = load_paper_context("0a1a3b2fff54")
    ctx.card.problem          # -> ...

CLI usage (dev sanity check):
    uv run python -m landscape.paper_context 0a1a3b2fff54
"""

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from ingestion.s3store import ArtifactStore
from landscape.schemas import LimitationSource

# ingestion.s3store reads S3_ARTIFACTS_URL from the environment but never
# loads .env itself (it's meant to be used from a process that already has);
# run standalone (this module's CLI, or any import chain that doesn't also
# pull in llm_client/indexing.embeddings, which load it as a side effect),
# .env would otherwise silently never be read.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class PaperContextError(ValueError):
    """paper.json for a paper_id could not be loaded or projected."""


class PaperCard(BaseModel):
    """Compact per-paper view for landscape/cross-paper reasoning.

    A pure field projection of paper.json (PaperAnalysis). Fields are kept
    as lists of short statements (not paragraphs) so each statement can
    later become its own concept-extraction unit; nothing here is dropped
    silently -- an empty list means paper.json actually had nothing there.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_id: str
    title: str
    venue: str | None
    year: int | None

    problem: str
    research_questions: list[str]
    hypotheses: list[str]

    method_summary: str
    method_components: list[str]

    findings: list[str]
    interesting_findings: list[str]

    limitations: list[str]
    inferred_limitations: list[str]
    limitation_sources: list[LimitationSource] = Field(default_factory=list)
    assumptions: list[str]
    future_work: list[str]

    claims: list[str]


class PaperContext(BaseModel):
    """What the Landscape Builder consumes for one retrieved paper."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_id: str
    card: PaperCard
    matched_records: list[dict] = Field(default_factory=list)   # record hits from indexing.search, if any
    missing_fields: list[str] = Field(default_factory=list)     # PaperCard fields that were empty/absent in
                                                                 # paper.json -- surfaced, never hidden


def _project(paper_id: str, analysis: dict) -> tuple[PaperCard, list[str]]:
    """Project paper.json (PaperAnalysis dict) into a PaperCard.

    Returns (card, missing_fields). A field is "missing" only when the
    source paper.json section itself is absent -- an intentionally empty
    list from the schema (e.g. no inferred limitations) is not "missing".
    """
    missing: list[str] = []

    def require(mapping: dict, path: str):
        node = mapping
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(path)
                return None
            node = node[part]
        return node

    meta = analysis.get("paper_metadata") or {}
    problem_section = analysis.get("research_problem") or {}
    if not analysis.get("research_problem"):
        missing.append("research_problem")
    method = analysis.get("method") or {}
    if not analysis.get("method"):
        missing.append("method")
    limitations = analysis.get("limitations") or {}
    future_work = analysis.get("future_work") or {}

    problem = problem_section.get("specific_problem") or problem_section.get("general_problem") or ""
    if not problem:
        missing.append("research_problem.specific_problem")

    method_components = [c.get("name", "") for c in method.get("components", []) if c.get("name")]

    key_results = [r.get("finding", "") for r in analysis.get("key_results", []) if r.get("finding")]
    interesting = [f.get("finding", "") for f in analysis.get("interesting_findings", []) if f.get("finding")]

    author_limits = [l.get("limitation", "") for l in limitations.get("author_stated", []) if l.get("limitation")]
    inferred_limits = [l.get("limitation", "") for l in limitations.get("inferred", []) if l.get("limitation")]

    limitation_sources = [
        LimitationSource(paper_id=paper_id, origin=origin,
                         value_path=f"/limitations/{section}/{i}",
                         statement=record["limitation"],
                         source_locations=record.get("source_locations") or [])
        for section, origin in (("author_stated", "author_stated"), ("inferred", "model_inferred"))
        for i, record in enumerate(limitations.get(section, [])) if record.get("limitation")
    ]

    assumptions = list(problem_section.get("why_difficult", []) or [])

    future = [f.get("direction", "") for f in future_work.get("author_proposed", []) if f.get("direction")]
    future += [o.get("research_question", "") for o in future_work.get("inferred_research_opportunities", [])
              if o.get("research_question")]

    claims = [c.get("claim", "") for c in analysis.get("claims_and_evidence", []) if c.get("claim")]

    if not analysis.get("key_results") and not analysis.get("interesting_findings"):
        missing.append("key_results/interesting_findings")
    if not analysis.get("limitations"):
        missing.append("limitations")
    if not analysis.get("future_work"):
        missing.append("future_work")
    if not analysis.get("claims_and_evidence"):
        missing.append("claims_and_evidence")

    card = PaperCard(
        paper_id=paper_id,
        title=meta.get("title", ""),
        venue=meta.get("venue"),
        year=meta.get("year"),
        problem=problem,
        research_questions=list(problem_section.get("research_questions", []) or []),
        hypotheses=list(problem_section.get("hypotheses", []) or []),
        method_summary=method.get("high_level_idea", ""),
        method_components=method_components,
        findings=key_results,
        interesting_findings=interesting,
        limitations=author_limits,
        inferred_limitations=inferred_limits,
        limitation_sources=limitation_sources,
        assumptions=assumptions,
        future_work=future,
        claims=claims,
    )
    return card, missing


def load_paper_context(paper_id: str, *, store: ArtifactStore | None = None,
                       matched_records: list[dict] | None = None) -> PaperContext:
    """Fetch <paper_id>/paper.json from S3 and project it into a PaperContext.

    store lets a caller reuse one ArtifactStore across many papers (one S3
    client instead of one per call); a temporary one is created otherwise.
    matched_records are the record hits from indexing.search for this
    paper_id, if the caller has them -- passed through unmodified.
    Raises PaperContextError if paper.json is missing or not valid JSON.
    """
    if not isinstance(paper_id, str) or not paper_id.strip():
        raise ValueError("paper_id must be a nonempty string")
    store = store or ArtifactStore()
    key = store.key(paper_id, "paper.json")
    try:
        analysis = store.get_json(key)
    except Exception as exc:
        raise PaperContextError(f"could not load paper.json for {paper_id}: {exc}") from exc
    if not isinstance(analysis, dict):
        raise PaperContextError(f"paper.json for {paper_id} is not a JSON object")

    card, missing = _project(paper_id, analysis)
    return PaperContext(
        paper_id=paper_id,
        card=card,
        matched_records=matched_records or [],
        missing_fields=missing,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paper_id")
    args = parser.parse_args()
    ctx = load_paper_context(args.paper_id)
    print(json.dumps({
        "paper_id": ctx.paper_id,
        "card": ctx.card.model_dump(),
        "missing_fields": ctx.missing_fields,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
