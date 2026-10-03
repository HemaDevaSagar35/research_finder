"""Shared Landscape Builder → reasoner contract (architecture sections 2.5–6).

See docs/landscape_builder.md. The builder's flat collections
remain the public shape; IDs and attributable evidence support downstream use.
"""

import hashlib
import json
import re
from typing import Literal

from extraction.research_extract import SourceLocation

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VALID_RELATIONS = {
    "SOLVES", "REDUCES", "INTRODUCES", "CAUSES", "DEPENDS_ON",
    "REQUIRES", "FAILS_UNDER", "TRADES_OFF_WITH", "HURTS", "CONTRADICTS",
}

VALID_FACETS = {
    "problem", "bottleneck", "method", "mechanism",
    "evaluation_regime", "hardware_regime", "model_scale",
    "optimization_target", "failure_mode",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConceptEntry(Strict):
    """One normalized concept and every paper that mentions it. This folds
    what was a separate MENTIONS edge into the group itself -- paper
    membership on a concept IS the "faceted grouping" the architecture doc
    asks for; no separate structure needed to answer "papers touching X"."""
    concept_id: str = Field(min_length=1)
    facet: str
    label: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)

    @field_validator("facet")
    @classmethod
    def _facet_known(cls, value: str) -> str:
        if value not in VALID_FACETS:
            raise ValueError(f"unknown facet {value!r}; must be one of {sorted(VALID_FACETS)}")
        return value


def stable_id(kind: str, *parts) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{kind}_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


class EvidenceReference(Strict):
    paper_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    source_locations: list[SourceLocation] = Field(default_factory=list)


class Relationship(Strict):
    """One evidence-backed typed edge between two concepts. supporting_papers
    is mandatory and non-empty -- a relationship with no supporting paper is
    not a valid Relationship, by construction, not by convention."""
    item_id: str = ""
    evidence: list[EvidenceReference] = Field(default_factory=list)
    source: str = Field(min_length=1)
    relation: str
    target: str = Field(min_length=1)
    supporting_papers: list[str] = Field(min_length=1)
    evidence_record_ids: list[str] = Field(default_factory=list)
    source_locations: list[dict] = Field(default_factory=list)

    @field_validator("relation")
    @classmethod
    def _relation_known(cls, value: str) -> str:
        if value not in VALID_RELATIONS:
            raise ValueError(f"unknown relation {value!r}; must be one of {sorted(VALID_RELATIONS)}")
        return value

    @model_validator(mode="after")
    def identify(self):
        if not self.item_id:
            self.item_id = stable_id("relationship", self.source, self.relation, self.target)
        for ref in self.evidence:
            if ref.paper_id not in self.supporting_papers:
                raise ValueError("relationship evidence paper is not a supporting paper")
        return self

    def key(self) -> tuple[str, str, str]:
        return (self.source, self.relation, self.target)


LimitationOrigin = Literal["author_stated", "model_inferred"]


def limitation_origin(value_path: str | None) -> LimitationOrigin | None:
    """Extraction category, not a claim that the original page verified it."""
    if value_path and re.fullmatch(r"/limitations/author_stated/[0-9]+", value_path):
        return "author_stated"
    if value_path and re.fullmatch(r"/limitations/inferred/[0-9]+", value_path):
        return "model_inferred"
    return None


class LimitationSource(Strict):
    paper_id: str = Field(min_length=1)
    origin: LimitationOrigin
    value_path: str | None = None
    statement: str = Field(min_length=1)
    source_locations: list[SourceLocation] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_origin(self):
        if self.value_path is not None and limitation_origin(self.value_path) != self.origin:
            raise ValueError("limitation origin must agree with its paper.json path")
        return self


class AggregatedItem(Strict):
    """One merged free-text statement (architecture doc's aggregated_findings /
    recurring_limitations / common_assumptions) -- for observations that are
    not shaped as a relation triple between two concepts."""
    item_id: str = ""
    statement: str = Field(min_length=1)
    limitation_sources: list[LimitationSource] = Field(default_factory=list)
    supporting_papers: list[str] = Field(min_length=1)


class Contradiction(Strict):
    """Two relationships between the same (source, target) whose relations
    are structurally opposed, or an explicit CONTRADICTS edge alongside
    another. A graph pattern over the final relationships, materialized once
    at build time rather than left for a caller to re-derive."""
    item_id: str = ""
    a: Relationship
    b: Relationship


class UnderexploredConcept(Strict):
    """A concept with weak support: few mentions and no strongly-backed
    relation. A cheap proxy for "little evidence touches this here", not a
    claim about novelty -- that judgment belongs to the Opportunity Miner."""
    item_id: str = ""
    concept_id: str = Field(min_length=1)
    facet: str
    label: str = Field(min_length=1)
    mention_count: int = Field(ge=0)
    max_relation_support: int = Field(ge=0)


class Landscape(Strict):
    """The Landscape Builder's complete output -- architecture doc sec 4.
    Answers "what does this research area look like"; does not decide what
    is unresolved (that is the Cross-Paper Reasoner / Opportunity Miner)."""
    schema_version: Literal["landscape_builder_v1"] = "landscape_builder_v1"
    topic: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)
    groups: list[ConceptEntry] = Field(default_factory=list)
    aggregated_findings: list[AggregatedItem] = Field(default_factory=list)
    recurring_limitations: list[AggregatedItem] = Field(default_factory=list)
    common_assumptions: list[AggregatedItem] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    underexplored_regimes: list[UnderexploredConcept] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def identify_and_validate(self):
        inventory = set(self.paper_ids)
        if len(inventory) != len(self.paper_ids):
            raise ValueError("duplicate paper_id in inventory")
        groups = {g.concept_id: g for g in self.groups}
        if len(groups) != len(self.groups):
            raise ValueError("duplicate concept_id")
        seen = set()

        def item_id(obj, kind, *parts):
            if not obj.item_id:
                obj.item_id = stable_id(kind, *parts)
            if obj.item_id in seen:
                raise ValueError(f"duplicate item_id: {obj.item_id}")
            seen.add(obj.item_id)

        def papers(ids):
            if len(ids) != len(set(ids)) or not set(ids) <= inventory:
                raise ValueError("duplicate or unknown supporting paper")

        for g in self.groups:
            papers(g.paper_ids)
        for name in ("aggregated_findings", "recurring_limitations", "common_assumptions"):
            for item in getattr(self, name):
                papers(item.supporting_papers)
                if any(src.paper_id not in item.supporting_papers for src in item.limitation_sources):
                    raise ValueError("limitation source paper is not a supporting paper")
                item_id(item, name, item.statement, sorted(item.supporting_papers))
        relationships = {}
        for r in self.relationships:
            papers(r.supporting_papers)
            if r.source not in groups or r.target not in groups:
                raise ValueError("relationship references unknown concept")
            item_id(r, "relationship", r.source, r.relation, r.target)
            relationships[r.item_id] = r
        for c in self.contradictions:
            for side in (c.a, c.b):
                if side.item_id not in relationships or side != relationships[side.item_id]:
                    raise ValueError("contradiction side must match a landscape relationship")
            if c.a.item_id == c.b.item_id:
                raise ValueError("contradiction sides must differ")
            item_id(c, "contradiction", sorted([c.a.item_id, c.b.item_id]))
        for u in self.underexplored_regimes:
            if u.concept_id not in groups:
                raise ValueError("underexplored regime references unknown concept")
            g = groups[u.concept_id]
            expected = max((len(r.supporting_papers) for r in self.relationships
                            if u.concept_id in (r.source, r.target)), default=0)
            if (u.facet, u.label, u.mention_count, u.max_relation_support) != (
                    g.facet, g.label, len(g.paper_ids), expected):
                raise ValueError("underexplored counts/labels must match the landscape")
            item_id(u, "underexplored", u.concept_id)
        return self
