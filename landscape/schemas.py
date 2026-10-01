"""Landscape Builder output contracts: the flat shape from the architecture
doc's Landscape Output spec (docs/research_path_generator_architecture_refined.md
sec 4), built independently of any other component's schema -- see landscape/
module docstring for why. Every object here is Pydantic (extra="forbid"),
matching the convention used elsewhere in this codebase for shared contracts
(research/retrieval.py, and -- independently -- reasoning/schemas.py on
another branch) rather than only at LLM-call boundaries.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class Relationship(Strict):
    """One evidence-backed typed edge between two concepts. supporting_papers
    is mandatory and non-empty -- a relationship with no supporting paper is
    not a valid Relationship, by construction, not by convention."""
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

    def key(self) -> tuple[str, str, str]:
        return (self.source, self.relation, self.target)


class AggregatedItem(Strict):
    """One merged free-text statement (architecture doc's aggregated_findings /
    recurring_limitations / common_assumptions) -- for observations that are
    not shaped as a relation triple between two concepts."""
    statement: str = Field(min_length=1)
    supporting_papers: list[str] = Field(min_length=1)


class Contradiction(Strict):
    """Two relationships between the same (source, target) whose relations
    are structurally opposed, or an explicit CONTRADICTS edge alongside
    another. A graph pattern over the final relationships, materialized once
    at build time rather than left for a caller to re-derive."""
    a: Relationship
    b: Relationship


class UnderexploredConcept(Strict):
    """A concept with weak support: few mentions and no strongly-backed
    relation. A cheap proxy for "little evidence touches this here", not a
    claim about novelty -- that judgment belongs to the Opportunity Miner."""
    concept_id: str = Field(min_length=1)
    facet: str
    label: str = Field(min_length=1)
    mention_count: int = Field(ge=0)
    max_relation_support: int = Field(ge=0)


class Landscape(Strict):
    """The Landscape Builder's complete output -- architecture doc sec 4.
    Answers "what does this research area look like"; does not decide what
    is unresolved (that is the Cross-Paper Reasoner / Opportunity Miner)."""
    topic: str = Field(min_length=1)
    paper_ids: list[str] = Field(min_length=1)
    groups: list[ConceptEntry] = Field(default_factory=list)
    aggregated_findings: list[AggregatedItem] = Field(default_factory=list)
    recurring_limitations: list[AggregatedItem] = Field(default_factory=list)
    common_assumptions: list[AggregatedItem] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    underexplored_regimes: list[UnderexploredConcept] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
