"""Concept Normalizer: merge raw per-paper concept labels into canonical nodes.

Hybrid approach (design decision): embedding similarity first to cheaply
narrow down candidate pairs (avoids an O(n^2) LLM judgment over every pair of
raw labels), then an LLM judges only the close pairs for genuine equivalence
before merging. This keeps the "only merge concepts that are genuinely
equivalent, not merely related" rule from the architecture doc affordable at
scale.

Input: raw (concept, facet) mentions as extracted per-paper by
landscape.extraction, with no cross-paper identity yet -- "expert swapping",
"host-side expert loading", and "CPU-GPU expert transfer" are three distinct
strings at this point. Output: a mapping from every raw (label, facet) pair
to one canonical concept_id, ready for landscape.builder's flat accumulation
into ConceptEntry / Relationship (landscape.schemas).
"""

import asyncio
import json
import os
import re

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indexing.embeddings import DEFAULT_MODELS, embed_texts
from llm_client import AsyncLLMClient

SIMILARITY_THRESHOLD = 0.80   # candidate pairs below this never reach the LLM
DEFAULT_EMBED_PROVIDER = "local"   # concept clustering, not query/index retrieval --
                                   # doesn't need to match the corpus embedding model


class RawLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    facet: str


class EquivalencePair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    index: int = Field(ge=0)
    equivalent: bool


class EquivalenceJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pairs: list[EquivalencePair]


class NormalizationError(ValueError):
    """The LLM judge did not return a usable equivalence judgment."""


SYSTEM_PROMPT = """You will see numbered pairs of short research-concept
phrases from the same facet (e.g. both "method", both "bottleneck"). For each
pair, decide whether the two phrases genuinely refer to the same concept --
not merely related or often co-occurring concepts. For example "expert
swapping" and "host-side expert loading" are the same concept; "expert
caching" and "expert routing" are related but NOT the same concept. Return
only a JSON object {"pairs": [{"index": <int>, "equivalent": <bool>}, ...]}
with exactly one entry per input pair, in any order. When in doubt, prefer
"equivalent": false -- merging unrelated concepts is worse than keeping two
concepts separate.
"""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "concept"


class UnionFind:
    def __init__(self, items: list):
        self._parent = {item: item for item in items}

    def find(self, item):
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> dict:
        out: dict = {}
        for item in self._parent:
            out.setdefault(self.find(item), []).append(item)
        return out


def _candidate_pairs(labels: list[RawLabel], embeddings: np.ndarray,
                     threshold: float) -> list[tuple[int, int, float]]:
    """Indices into `labels` whose cosine similarity clears `threshold`,
    restricted to same-facet pairs (cross-facet merges are never valid --
    a "method" and a "bottleneck" are never the same concept)."""
    norm = embeddings / np.clip(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9, None)
    sims = norm @ norm.T
    pairs = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            if labels[i].facet != labels[j].facet:
                continue
            score = float(sims[i, j])
            if score >= threshold:
                pairs.append((i, j, score))
    return pairs


async def _judge_pairs(labels: list[RawLabel], pairs: list[tuple[int, int, float]], *,
                       client: AsyncLLMClient, model: str | None) -> list[bool]:
    """One LLM call judging all candidate pairs at once. Returns a list
    aligned with `pairs`; raises NormalizationError if the response can't be
    parsed or doesn't cover every pair."""
    if not pairs:
        return []
    numbered = "\n".join(
        f'{i}: "{labels[a].text}" vs "{labels[b].text}" (facet: {labels[a].facet})'
        for i, (a, b, _score) in enumerate(pairs)
    )
    raw = await client.chat(
        numbered,
        system=SYSTEM_PROMPT,
        model=model or os.environ.get("LANDSCAPE_NORMALIZE_MODEL"),
        response_format={"type": "json_object"},
    )
    try:
        judgment = EquivalenceJudgment.model_validate_json(raw)
    except (ValidationError, TypeError) as exc:
        raise NormalizationError("normalizer judge did not return a usable equivalence judgment") from exc
    by_index = {p.index: p.equivalent for p in judgment.pairs}
    missing = [i for i in range(len(pairs)) if i not in by_index]
    if missing:
        raise NormalizationError(f"normalizer judge omitted pairs {missing}")
    return [by_index[i] for i in range(len(pairs))]


async def normalize_concepts(labels: list[RawLabel], *,
                             threshold: float = SIMILARITY_THRESHOLD,
                             embed_provider: str | None = None,
                             client: AsyncLLMClient | None = None,
                             provider: str | None = None,
                             model: str | None = None) -> dict[RawLabel, str]:
    """Resolve raw (text, facet) labels into canonical concept_ids.

    Returns a dict mapping every input RawLabel to a concept_id string. Two
    labels get the same concept_id iff they are similar enough to be
    considered (>= threshold, same facet) AND the LLM judged them equivalent.
    Labels that never clear the similarity bar keep their own concept_id
    (slugified from their own text) without ever reaching the LLM -- most
    pairs across an unrelated facet space are filtered this way, which is
    what keeps this affordable.
    """
    if not labels:
        return {}
    unique = list(dict.fromkeys(labels))   # de-dup exact (text, facet) repeats
    if len(unique) == 1:
        label = unique[0]
        canonical = {label: _slugify(label.text)}
        return {l: canonical[l] for l in labels}

    embed_provider = embed_provider or os.environ.get("LANDSCAPE_NORMALIZE_EMBED_PROVIDER", DEFAULT_EMBED_PROVIDER)
    # DEFAULT_MODELS (not embed_config/EMBED_MODEL) on purpose: this provider
    # is independent of the corpus's EMBED_PROVIDER/EMBED_MODEL, and falling
    # back to the global EMBED_MODEL would be wrong if it names a model for
    # a different provider than embed_provider here.
    embed_model = os.environ.get("LANDSCAPE_NORMALIZE_EMBED_MODEL") or DEFAULT_MODELS[embed_provider]
    vectors = embed_texts([l.text for l in unique], embed_provider, embed_model, None)
    pairs = _candidate_pairs(unique, vectors, threshold)

    if client is not None and provider is not None:
        raise ValueError("Pass provider or client, not both")
    owned = client is None
    if owned:
        client = AsyncLLMClient(provider or os.environ.get("LANDSCAPE_NORMALIZE_PROVIDER"))
    try:
        verdicts = await _judge_pairs(unique, pairs, client=client, model=model)
    finally:
        if owned:
            await client.raw.close()

    uf = UnionFind(unique)
    for (i, j, _score), equivalent in zip(pairs, verdicts):
        if equivalent:
            uf.union(unique[i], unique[j])

    concept_id_by_root: dict[RawLabel, str] = {}
    for root, members in uf.groups().items():
        # canonical label: the shortest text in the group, for a tidy concept_id
        canonical_text = min((m.text for m in members), key=len)
        concept_id = _slugify(canonical_text)
        for member in members:
            concept_id_by_root[member] = concept_id

    return {label: concept_id_by_root[label] for label in labels}
