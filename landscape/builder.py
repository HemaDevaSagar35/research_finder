"""Landscape Builder: PaperContexts -> Landscape.

Orchestrates the pipeline (docs/research_path_generator_architecture_refined.md
sec 3-4, docs/research_path_generator_components_refined.md items 3-7):

    query (build_landscape_for_query only)
        -> research.query_planner.plan_queries -> research.retrieval
           .MultiQueryRetriever -- the already-built query-to-evidence
           foundation, not a direct indexing.search call
        -> PaperContexts (landscape.paper_context)
        -> per-paper raw extraction (landscape.extraction)
        -> concept normalization across all papers (landscape.normalize)
        -> free-text finding aggregation (landscape.aggregation)
        -> flat accumulation into groups / relationships, merging by
           (source, relation, target); contradictions and underexplored
           concepts computed once at the end
        -> Landscape (landscape.schemas)

build_landscape(topic, contexts) takes already-loaded PaperContexts and is
independently testable without S3, an index, or a retriever.

No intermediate graph object: the target output is already a flat shape
(architecture doc sec 4), so this builds it directly with plain dicts keyed
for merge/dedup, rather than through a separate stateful container.

Boundary this module must not cross: it answers "what does this area look
like", not "what is unresolved". Gap-finding is the Cross-Paper Reasoner /
Opportunity Miner's job, reading this Landscape afterwards.

CLI usage (dev, against a real index + S3 on EC2):
    uv run python -m landscape.builder "efficient MoE inference" --papers 15
"""

import argparse
import asyncio
from pathlib import Path

from indexing.search import REPO_ROOT
from landscape.aggregation import aggregate_findings
from landscape.evidence import resolve_evidence
from landscape.extraction import RawExtraction, extract_from_papers
from landscape.normalize import RawLabel, normalize_concepts
from landscape.paper_context import PaperContext, load_paper_context
from landscape.schemas import (VALID_RELATIONS, ConceptEntry, Contradiction,
                               Landscape, Relationship, UnderexploredConcept)
from ingestion.s3store import ArtifactStore
from research.query_planner import plan_queries
from research.retrieval import LocalBackend, MultiQueryRetriever, RetrievalFilters

OPPOSED_RELATION_PAIRS = {
    frozenset({"REDUCES", "HURTS"}),
    frozenset({"REDUCES", "INTRODUCES"}),
    frozenset({"SOLVES", "FAILS_UNDER"}),
}


def load_paper_contexts(paper_ids: list[str], *,
                        matched_records_by_paper: dict[str, list[dict]] | None = None,
                        store: ArtifactStore | None = None) -> dict[str, PaperContext]:
    """Load PaperContexts for a list of paper_ids, reusing one ArtifactStore.
    A paper whose paper.json can't be loaded is skipped, not silently
    substituted -- callers should check len(result) against len(paper_ids)."""
    store = store or ArtifactStore()
    matched_records_by_paper = matched_records_by_paper or {}
    contexts = {}
    for paper_id in paper_ids:
        try:
            contexts[paper_id] = load_paper_context(
                paper_id, store=store,
                matched_records=matched_records_by_paper.get(paper_id))
        except Exception as exc:
            print(f"warning: skipping {paper_id}: {exc}")
    return contexts


def _resolve_facets(extractions: dict[str, RawExtraction]) -> dict[str, str]:
    """A RawTriple carries no facet for its source/target concepts; resolve
    each concept text's real facet from any mention with the same text seen
    anywhere, falling back to "method" if a concept was never also mentioned."""
    facet_by_text: dict[str, str] = {}
    for extraction in extractions.values():
        if isinstance(extraction, Exception):
            continue
        for m in extraction.mentions:
            facet_by_text.setdefault(m.concept, m.facet)
    return facet_by_text


def _find_contradictions(relationships: list[Relationship]) -> list[Contradiction]:
    """Pairs of relationships between the same (source, target) whose
    relations are structurally opposed, or where one side is an explicit
    CONTRADICTS edge. A pattern over the final relationships, computed once
    here rather than left as an on-demand query nobody calls."""
    by_pair: dict[tuple[str, str], list[Relationship]] = {}
    for rel in relationships:
        by_pair.setdefault((rel.source, rel.target), []).append(rel)
    out: list[Contradiction] = []
    for edges in by_pair.values():
        for i, a in enumerate(edges):
            for b in edges[i + 1:]:
                if a.relation == "CONTRADICTS" or b.relation == "CONTRADICTS":
                    out.append(Contradiction(a=a, b=b))
                elif frozenset({a.relation, b.relation}) in OPPOSED_RELATION_PAIRS:
                    out.append(Contradiction(a=a, b=b))
    return out


def _find_underexplored(groups: list[ConceptEntry], relationships: list[Relationship],
                        min_support: int = 2) -> list[UnderexploredConcept]:
    """Concepts with weak support: few mentions and no strongly-backed
    relation. A cheap proxy for underexplored_regimes, not a novelty claim."""
    out = []
    for group in groups:
        mention_count = len(group.paper_ids)
        related = [r for r in relationships if r.source == group.concept_id or r.target == group.concept_id]
        max_support = max((len(r.supporting_papers) for r in related), default=0)
        if mention_count < min_support and max_support < min_support:
            out.append(UnderexploredConcept(
                concept_id=group.concept_id, facet=group.facet, label=group.label,
                mention_count=mention_count, max_relation_support=max_support))
    return out


async def build_landscape(topic: str, contexts: dict[str, PaperContext], *,
                          extraction_provider: str | None = None,
                          normalize_provider: str | None = None,
                          aggregation_provider: str | None = None) -> Landscape:
    """The core orchestration, given already-loaded PaperContexts (so this
    is independently testable with synthetic contexts, without S3 or search)."""
    cards = {pid: ctx.card for pid, ctx in contexts.items()}

    extractions = await extract_from_papers(cards, provider=extraction_provider)
    for paper_id, extraction in extractions.items():
        if isinstance(extraction, Exception):
            print(f"warning: extraction failed for {paper_id}: {extraction}")

    facet_by_text = _resolve_facets(extractions)

    def label_for(text: str, fallback_facet: str) -> RawLabel:
        return RawLabel(text=text, facet=facet_by_text.get(text, fallback_facet))

    # -- gather every raw label that needs a canonical concept_id -----------
    mention_labels_by_paper: dict[str, list[RawLabel]] = {}
    triple_labels_by_paper: dict[str, list[tuple[RawLabel, str, RawLabel, str]]] = {}
    all_labels: list[RawLabel] = []

    for paper_id, extraction in extractions.items():
        if isinstance(extraction, Exception):
            continue
        mentions = [label_for(m.concept, m.facet) for m in extraction.mentions]
        mention_labels_by_paper[paper_id] = mentions
        all_labels.extend(mentions)

        triples = []
        for t in extraction.triples:
            if t.relation not in VALID_RELATIONS:
                continue   # extraction already filters this; defensive here too
            source = label_for(t.source_concept, "method")
            target = label_for(t.target_concept, "method")
            triples.append((source, t.relation, target, t.evidence_quote))
            all_labels.extend([source, target])
        triple_labels_by_paper[paper_id] = triples

    concept_ids = await normalize_concepts(all_labels, provider=normalize_provider)

    # -- flat accumulation: groups (ConceptEntry) -----------------------------
    paper_ids_by_concept: dict[str, set[str]] = {}
    label_by_concept: dict[str, RawLabel] = {}
    for paper_id, mentions in mention_labels_by_paper.items():
        for label in mentions:
            concept_id = concept_ids[label]
            paper_ids_by_concept.setdefault(concept_id, set()).add(paper_id)
            label_by_concept.setdefault(concept_id, label)
    # Concepts that only ever appear as a relation endpoint (never directly
    # mentioned) still need a group entry; attribute them to the triple's paper.
    for paper_id, triples in triple_labels_by_paper.items():
        for source, _relation, target, _evidence_quote in triples:
            for label in (source, target):
                concept_id = concept_ids[label]
                paper_ids_by_concept.setdefault(concept_id, set()).add(paper_id)
                label_by_concept.setdefault(concept_id, label)

    groups = [
        ConceptEntry(concept_id=cid, facet=label_by_concept[cid].facet,
                    label=label_by_concept[cid].text, paper_ids=sorted(pids))
        for cid, pids in paper_ids_by_concept.items()
    ]

    # -- flat accumulation: relationships, merging by (source, relation, target) --
    # evidence_quote -> real record_id/source_locations is a code-only lookup
    # against this paper's matched_records (landscape.evidence), not another
    # LLM call; no match found just leaves that triple's evidence empty.
    relation_accum: dict[tuple[str, str, str], dict] = {}
    for paper_id, triples in triple_labels_by_paper.items():
        matched_records = contexts[paper_id].matched_records if paper_id in contexts else []
        for source, relation, target, evidence_quote in triples:
            key = (concept_ids[source], relation, concept_ids[target])
            entry = relation_accum.setdefault(key, {
                "papers": set(), "record_ids": set(), "locations": [], "seen_locations": set()})
            entry["papers"].add(paper_id)
            record_ids, locations = resolve_evidence(evidence_quote, matched_records)
            entry["record_ids"].update(record_ids)
            for location in locations:
                loc_key = (paper_id, repr(sorted(location.items())) if isinstance(location, dict) else repr(location))
                if loc_key not in entry["seen_locations"]:
                    entry["seen_locations"].add(loc_key)
                    entry["locations"].append(location)
    relationships = [
        Relationship(source=s, relation=r, target=t,
                    supporting_papers=sorted(entry["papers"]),
                    evidence_record_ids=sorted(entry["record_ids"]),
                    source_locations=entry["locations"])
        for (s, r, t), entry in relation_accum.items()
    ]

    contradictions = _find_contradictions(relationships)
    underexplored = _find_underexplored(groups, relationships)

    aggregated_findings, recurring_limitations, common_assumptions = await aggregate_findings(
        cards, provider=aggregation_provider)

    return Landscape(
        topic=topic,
        paper_ids=sorted(contexts),
        groups=groups,
        aggregated_findings=aggregated_findings,
        recurring_limitations=recurring_limitations,
        common_assumptions=common_assumptions,
        contradictions=contradictions,
        underexplored_regimes=underexplored,
        relationships=relationships,
    )


async def build_landscape_for_query(topic: str, *, index_dir: Path | None = None,
                                    top_papers: int = 15, record_k: int = 50,
                                    max_queries: int = 5,
                                    planner_provider: str | None = None,
                                    embedding: str | None = None) -> Landscape:
    """End-to-end from a query: plan -> multi-query retrieve -> load evidence
    -> build landscape. Goes through the already-built query-to-evidence
    foundation (research.query_planner + research.retrieval) rather than
    calling indexing.search directly, so paper selection reflects every
    planned query (not just the raw topic string) and each matched record
    keeps its real source_locations and per-query provenance. Deliberately
    skips reranking (research/retrieval.py sec 2.3 reranking is still
    pending there) and uses the local backend; swap in OpenSearchBackend for
    production without changing anything downstream of retrieval."""
    index_dir = index_dir or (REPO_ROOT / "index")
    plan = await plan_queries(topic, max_queries=max_queries, provider=planner_provider)

    backend = LocalBackend(index_dir, embedding=embedding)
    try:
        async with MultiQueryRetriever(backend) as retriever:
            result = await retriever.retrieve(
                plan, record_k=record_k, paper_k=top_papers,
                filters=RetrievalFilters())
    finally:
        backend.close()

    matched_records_by_paper = {
        paper.paper_id: [record.model_dump() for record in paper.records]
        for paper in result.papers
    }
    contexts = load_paper_contexts(list(matched_records_by_paper),
                                   matched_records_by_paper=matched_records_by_paper)
    return await build_landscape(topic, contexts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("topic")
    parser.add_argument("--papers", type=int, default=15)
    parser.add_argument("--record-k", type=int, default=50)
    parser.add_argument("--max-queries", type=int, default=5)
    parser.add_argument("--index-dir", default="index")
    parser.add_argument("--embedding", help="Embedding variant slug (default: active one)")
    parser.add_argument("--planner-provider", help="LLM provider for query planning")
    parser.add_argument("--out", help="Write the landscape JSON here instead of stdout")
    args = parser.parse_args()

    landscape = asyncio.run(build_landscape_for_query(
        args.topic, index_dir=REPO_ROOT / args.index_dir, top_papers=args.papers,
        record_k=args.record_k, max_queries=args.max_queries,
        planner_provider=args.planner_provider, embedding=args.embedding))
    payload = landscape.model_dump_json(indent=2)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"wrote {args.out}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
