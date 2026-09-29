"""Landscape Builder: PaperContexts -> LandscapeGraph.

Orchestrates the pipeline (docs/research_path_generator_architecture_refined.md
sec 3, docs/research_path_generator_components_refined.md items 3-7):

    PaperContexts (landscape.paper_context)
        -> per-paper raw extraction (landscape.extraction)
        -> concept normalization across all papers (landscape.normalize)
        -> upsert into the graph (landscape.graph), merging by
           (source, relation, target)
        -> LandscapeGraph

Boundary this module must not cross: it answers "what does this area look
like", not "what is unresolved". Gap-finding is the Cross-Paper Reasoner /
Opportunity Miner's job, reading this graph afterwards -- do not add
opportunity-shaped fields (e.g. "this seems promising") here.

CLI usage (dev, against a real index + S3 on EC2):
    uv run python -m landscape.builder "efficient MoE inference" --papers 15
"""

import argparse
import asyncio
import json
from pathlib import Path

from indexing.search import search as index_search, REPO_ROOT
from landscape.extraction import RawExtraction, extract_from_papers
from landscape.graph import LandscapeGraph
from landscape.normalize import RawLabel, normalize_concepts
from landscape.paper_context import PaperContext, load_paper_context
from ingestion.s3store import ArtifactStore


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


def _raw_labels_from_extractions(extractions: dict[str, RawExtraction]) -> tuple[
        list[RawLabel], dict[str, list[tuple[RawLabel, list[str]]]],
        dict[str, list[tuple[RawLabel, str, RawLabel, list[str]]]]]:
    """Flatten per-paper RawExtractions into: the full label list (for
    normalize_concepts), a per-paper list of (mention_label, evidence) for
    MENTIONS edges, and a per-paper list of (source, relation, target,
    evidence) for relation edges -- all still in raw per-paper text."""
    all_labels: list[RawLabel] = []
    mentions_by_paper: dict[str, list] = {}
    triples_by_paper: dict[str, list] = {}

    for paper_id, extraction in extractions.items():
        if isinstance(extraction, Exception):
            continue
        mentions = []
        for m in extraction.mentions:
            label = RawLabel(m.concept, m.facet)
            all_labels.append(label)
            mentions.append((label, [m.evidence_quote]))
        mentions_by_paper[paper_id] = mentions

        triples = []
        for t in extraction.triples:
            source = RawLabel(t.source_concept, "method")   # facet resolved below
            target = RawLabel(t.target_concept, "method")
            all_labels.append(source)
            all_labels.append(target)
            triples.append((source, t.relation, target, [t.evidence_quote]))
        triples_by_paper[paper_id] = triples

    return all_labels, mentions_by_paper, triples_by_paper


async def build_landscape(topic: str, contexts: dict[str, PaperContext], *,
                          extraction_provider: str | None = None,
                          normalize_provider: str | None = None) -> LandscapeGraph:
    """The core orchestration, given already-loaded PaperContexts (so this
    is independently testable with synthetic contexts, without S3 or search)."""
    cards = {pid: ctx.card for pid, ctx in contexts.items()}
    extractions = await extract_from_papers(cards, provider=extraction_provider)

    for paper_id, extraction in extractions.items():
        if isinstance(extraction, Exception):
            print(f"warning: extraction failed for {paper_id}: {extraction}")

    all_labels, mentions_by_paper, triples_by_paper = _raw_labels_from_extractions(extractions)

    # Relation source/target concepts were placeholder-tagged "method" above
    # since RawTriple carries no facet; resolve their real facet from any
    # matching mention with the same text, falling back to "method".
    facet_by_text: dict[str, str] = {}
    for extraction in extractions.values():
        if isinstance(extraction, Exception):
            continue
        for m in extraction.mentions:
            facet_by_text.setdefault(m.concept, m.facet)
    resolved_labels = [RawLabel(l.text, facet_by_text.get(l.text, l.facet)) for l in all_labels]

    concept_ids = await normalize_concepts(resolved_labels, provider=normalize_provider)
    label_to_id = {RawLabel(l.text, facet_by_text.get(l.text, l.facet)): cid
                  for l, cid in concept_ids.items()}

    graph = LandscapeGraph(topic=topic)
    for label, concept_id in label_to_id.items():
        graph.add_concept(concept_id, label.facet, label.text)

    for paper_id, mentions in mentions_by_paper.items():
        for label, _evidence in mentions:
            resolved = RawLabel(label.text, facet_by_text.get(label.text, label.facet))
            graph.add_mention(paper_id, label_to_id[resolved])

    for paper_id, triples in triples_by_paper.items():
        for source, relation, target, _evidence in triples:
            source_r = RawLabel(source.text, facet_by_text.get(source.text, source.facet))
            target_r = RawLabel(target.text, facet_by_text.get(target.text, target.facet))
            graph.add_relation(label_to_id[source_r], relation, label_to_id[target_r],
                              supporting_paper=paper_id)

    return graph


async def build_landscape_for_query(topic: str, *, index_dir: Path | None = None,
                                    top_papers: int = 15) -> LandscapeGraph:
    """End-to-end from a query: search -> load evidence -> build landscape.
    Deliberately skips reranking/dedup (indexing.search already dedupes and
    ranks by paper) -- this is the minimal real pipeline, not the final one."""
    index_dir = index_dir or (REPO_ROOT / "index")
    results = index_search(topic, index_dir, top_k=50)[:top_papers]
    matched_records_by_paper = {r["paper_id"]: r["records"] for r in results}
    contexts = load_paper_contexts(list(matched_records_by_paper),
                                   matched_records_by_paper=matched_records_by_paper)
    return await build_landscape(topic, contexts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("topic")
    parser.add_argument("--papers", type=int, default=15)
    parser.add_argument("--index-dir", default="index")
    parser.add_argument("--out", help="Write the landscape JSON here instead of stdout")
    args = parser.parse_args()

    graph = asyncio.run(build_landscape_for_query(
        args.topic, index_dir=REPO_ROOT / args.index_dir, top_papers=args.papers))
    payload = json.dumps(graph.to_dict(), indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"wrote {args.out}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
