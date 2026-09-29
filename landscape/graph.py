"""The Landscape graph: a query-scoped knowledge graph over retrieved papers.

Two node kinds (docs/research_path_generator_architecture_refined.md sec 3-4):
  - concept nodes: canonical ideas (problem/method/mechanism/regime/...),
    tagged by facet. One node kind instead of five parallel taxonomies.
  - paper nodes: paper_id, from retrieval.

Two edge families, kept as genuinely distinct signals (not derived from one
another):
  - MENTIONS (paper -> concept): a paper touches a concept at all. This is
    the "faceted grouping" step -- "papers touching X" is a graph query.
  - typed relations (concept -> concept): REDUCES, INTRODUCES, DEPENDS_ON,
    CONTRADICTS, ... . These *are* the aggregated findings: supporting_papers
    on the edge is what a separate "aggregated_findings" list would hold.

No graph database for V1: an in-memory structure, serializable to plain JSON.
Every edge (both families) must carry provenance -- an edge with no
supporting paper is not constructed.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


VALID_RELATIONS = {
    "SOLVES", "REDUCES", "INTRODUCES", "CAUSES", "DEPENDS_ON",
    "REQUIRES", "FAILS_UNDER", "TRADES_OFF_WITH", "HURTS", "CONTRADICTS",
}

VALID_FACETS = {
    "problem", "bottleneck", "method", "mechanism",
    "evaluation_regime", "hardware_regime", "model_scale",
    "optimization_target", "failure_mode",
}


@dataclass
class Concept:
    concept_id: str          # canonical, normalized (e.g. "expert_transfer")
    facet: str                # one of VALID_FACETS
    label: str                # human-readable form for display


@dataclass
class RelationEdge:
    source: str                # concept_id
    relation: str               # one of VALID_RELATIONS
    target: str                 # concept_id
    supporting_papers: set[str] = field(default_factory=set)
    evidence_record_ids: set[str] = field(default_factory=set)

    def key(self) -> tuple[str, str, str]:
        return (self.source, self.relation, self.target)


class LandscapeError(ValueError):
    """Invalid graph construction: unknown facet/relation, or no provenance."""


class LandscapeGraph:
    """Mutable builder for one query's landscape. Call to_dict() when done."""

    def __init__(self, topic: str):
        self.topic = topic
        self._concepts: dict[str, Concept] = {}
        self._mentions: dict[tuple[str, str], set[str]] = {}
        # (paper_id, concept_id) -> record_ids that grounded the mention
        self._relations: dict[tuple[str, str, str], RelationEdge] = {}

    # -- concepts ----------------------------------------------------------

    def add_concept(self, concept_id: str, facet: str, label: str) -> Concept:
        if facet not in VALID_FACETS:
            raise LandscapeError(f"unknown facet {facet!r}; must be one of {sorted(VALID_FACETS)}")
        existing = self._concepts.get(concept_id)
        if existing is not None:
            if existing.facet != facet:
                raise LandscapeError(
                    f"concept {concept_id!r} already registered with facet "
                    f"{existing.facet!r}, got {facet!r}")
            return existing
        concept = Concept(concept_id=concept_id, facet=facet, label=label)
        self._concepts[concept_id] = concept
        return concept

    def has_concept(self, concept_id: str) -> bool:
        return concept_id in self._concepts

    # -- MENTIONS (paper -> concept) ----------------------------------------

    def add_mention(self, paper_id: str, concept_id: str,
                    record_ids: list[str] | None = None) -> None:
        """A paper touches a concept. record_ids are the specific matched
        records that grounded this mention, if known (optional -- a mention
        can come from a broader read of the PaperCard, not just one record)."""
        self._require_concept(concept_id)
        key = (paper_id, concept_id)
        self._mentions.setdefault(key, set())
        if record_ids:
            self._mentions[key].update(record_ids)

    def papers_mentioning(self, concept_id: str) -> set[str]:
        return {pid for (pid, cid) in self._mentions if cid == concept_id}

    def concepts_mentioned_by(self, paper_id: str) -> set[str]:
        return {cid for (pid, cid) in self._mentions if pid == paper_id}

    # -- typed relations (concept -> concept) -------------------------------

    def add_relation(self, source: str, relation: str, target: str,
                     supporting_paper: str, evidence_record_id: str | None = None) -> RelationEdge:
        """Add or strengthen a typed edge. Merging is by (source, relation,
        target): a second paper backing the same triple extends
        supporting_papers on the existing edge instead of duplicating it.
        An edge with no supporting paper is refused -- provenance is
        mandatory, not optional metadata."""
        if relation not in VALID_RELATIONS:
            raise LandscapeError(f"unknown relation {relation!r}; must be one of {sorted(VALID_RELATIONS)}")
        self._require_concept(source)
        self._require_concept(target)
        if not supporting_paper:
            raise LandscapeError("add_relation requires a supporting_paper")

        key = (source, relation, target)
        edge = self._relations.get(key)
        if edge is None:
            edge = RelationEdge(source=source, relation=relation, target=target)
            self._relations[key] = edge
        edge.supporting_papers.add(supporting_paper)
        if evidence_record_id:
            edge.evidence_record_ids.add(evidence_record_id)
        return edge

    def relations_for(self, concept_id: str) -> list[RelationEdge]:
        return [e for e in self._relations.values()
                if e.source == concept_id or e.target == concept_id]

    def contradictions(self) -> list[tuple[RelationEdge, RelationEdge]]:
        """Pairs of edges between the same (source, target) whose relations
        are structurally opposed. A first cut: REDUCES vs HURTS/INTRODUCES,
        or an explicit CONTRADICTS edge alongside any other edge on the pair.
        This is a graph pattern, not a separate LLM-run detector."""
        opposed = {
            frozenset({"REDUCES", "HURTS"}),
            frozenset({"REDUCES", "INTRODUCES"}),
            frozenset({"SOLVES", "FAILS_UNDER"}),
        }
        by_pair: dict[tuple[str, str], list[RelationEdge]] = {}
        for edge in self._relations.values():
            by_pair.setdefault((edge.source, edge.target), []).append(edge)
        out = []
        for edges in by_pair.values():
            for i, a in enumerate(edges):
                for b in edges[i + 1:]:
                    if a.relation == "CONTRADICTS" or b.relation == "CONTRADICTS":
                        out.append((a, b))
                    elif frozenset({a.relation, b.relation}) in opposed:
                        out.append((a, b))
        return out

    def underexplored(self, min_support: int = 2) -> list[Concept]:
        """Concepts with weak support: few mentions and no strong relation
        backing. A cheap proxy for underexplored_regimes -- not a claim
        about novelty, just "little evidence touches this here"."""
        weak = []
        for concept in self._concepts.values():
            mentions = len(self.papers_mentioning(concept.concept_id))
            relations = self.relations_for(concept.concept_id)
            max_support = max((len(e.supporting_papers) for e in relations), default=0)
            if mentions < min_support and max_support < min_support:
                weak.append(concept)
        return weak

    # -- internal ------------------------------------------------------------

    def _require_concept(self, concept_id: str) -> None:
        if concept_id not in self._concepts:
            raise LandscapeError(
                f"concept {concept_id!r} not registered; call add_concept first")

    # -- serialization ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "concepts": [
                {"concept_id": c.concept_id, "facet": c.facet, "label": c.label}
                for c in self._concepts.values()
            ],
            "mentions": [
                {"paper_id": pid, "concept_id": cid, "record_ids": sorted(rids)}
                for (pid, cid), rids in self._mentions.items()
            ],
            "relations": [
                {"source": e.source, "relation": e.relation, "target": e.target,
                 "supporting_papers": sorted(e.supporting_papers),
                 "evidence_record_ids": sorted(e.evidence_record_ids)}
                for e in self._relations.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LandscapeGraph":
        graph = cls(topic=data["topic"])
        for c in data.get("concepts", []):
            graph.add_concept(c["concept_id"], c["facet"], c["label"])
        for m in data.get("mentions", []):
            graph.add_mention(m["paper_id"], m["concept_id"], m.get("record_ids"))
        for r in data.get("relations", []):
            for paper in r["supporting_papers"]:
                graph.add_relation(r["source"], r["relation"], r["target"], paper)
            edge = graph._relations[(r["source"], r["relation"], r["target"])]
            edge.evidence_record_ids.update(r.get("evidence_record_ids", []))
        return graph

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "LandscapeGraph":
        return cls.from_dict(json.loads(Path(path).read_text()))
