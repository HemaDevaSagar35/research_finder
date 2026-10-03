"""Finding Aggregator: merge near-duplicate free-text statements across papers.

Fills the three Landscape Output fields (architecture doc sec 4) that
landscape.builder's relation accumulation cannot express, because the underlying
statement isn't shaped as "X relates to Y" -- e.g. "most papers in this
space evaluate only on GSM-8K" or "caching is the dominant approach here"
are findings/limitations/assumptions, not relation triples.

    paper statements (findings, limitations, assumptions)
        -> embed each statement
        -> cluster near-duplicates across papers (same approach as
           landscape.normalize: similarity threshold, same-kind-only)
        -> one LLM call per cluster -> one merged statement
        -> AggregatedFinding / RecurringLimitation / CommonAssumption,
           each with every supporting paper kept

This is a separate step from landscape.extraction's relation triples; a
statement can end up aggregated here even if it never produced a triple.
"""

import asyncio
import os

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from indexing.embeddings import DEFAULT_MODELS, embed_texts
from landscape.normalize import UnionFind
from landscape.schemas import AggregatedItem
from llm_client import AsyncLLMClient
from landscape.paper_context import PaperCard

SIMILARITY_THRESHOLD = 0.80
DEFAULT_EMBED_PROVIDER = "local"


class RawStatement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    kind: str          # "finding" | "limitation" | "assumption"
    paper_id: str


class AggregationError(ValueError):
    """The LLM did not return a usable merged statement for a cluster."""


class ClusterSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    statement: str = Field(min_length=1)


SYSTEM_PROMPT = """You will see several near-duplicate statements from
different papers about the same underlying observation. Write ONE merged
statement that captures what they collectively say, in neutral language that
does not overclaim beyond what every statement supports. Return only a JSON
object {"statement": "..."}. Do not mention paper identifiers or add claims
not present in the inputs.
"""


def _collect_statements(cards: dict[str, PaperCard]) -> list[RawStatement]:
    out: list[RawStatement] = []
    for paper_id, card in cards.items():
        for text in card.findings + card.interesting_findings:
            out.append(RawStatement(text=text, kind="finding", paper_id=paper_id))
        for text in card.limitations + card.inferred_limitations:
            out.append(RawStatement(text=text, kind="limitation", paper_id=paper_id))
        for text in card.assumptions:
            out.append(RawStatement(text=text, kind="assumption", paper_id=paper_id))
    return out


def _cluster(statements: list[RawStatement], embeddings: np.ndarray,
            threshold: float) -> list[list[RawStatement]]:
    """Same-kind-only clustering, mirroring landscape.normalize's same-facet
    rule: a finding and a limitation are never the same cluster even if the
    text is similar."""
    norm = embeddings / np.clip(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9, None)
    sims = norm @ norm.T
    uf = UnionFind(list(range(len(statements))))
    for i in range(len(statements)):
        for j in range(i + 1, len(statements)):
            if statements[i].kind != statements[j].kind:
                continue
            if float(sims[i, j]) >= threshold:
                uf.union(i, j)
    groups = uf.groups()
    return [[statements[i] for i in members] for members in groups.values()]


async def _summarize_cluster(cluster: list[RawStatement], *,
                             client: AsyncLLMClient, model: str | None) -> str:
    if len(cluster) == 1:
        return cluster[0].text
    numbered = "\n".join(f"- {s.text}" for s in cluster)
    raw = await client.chat(
        numbered,
        system=SYSTEM_PROMPT,
        model=model or os.environ.get("LANDSCAPE_AGGREGATION_MODEL"),
        response_format={"type": "json_object"},
    )
    try:
        summary = ClusterSummary.model_validate_json(raw)
    except (ValidationError, TypeError) as exc:
        raise AggregationError("aggregator did not return a usable merged statement") from exc
    return summary.statement


async def aggregate_findings(cards: dict[str, PaperCard], *,
                             threshold: float = SIMILARITY_THRESHOLD,
                             embed_provider: str | None = None,
                             client: AsyncLLMClient | None = None,
                             provider: str | None = None,
                             model: str | None = None) -> tuple[
                                 list[AggregatedItem], list[AggregatedItem], list[AggregatedItem]]:
    """Returns (aggregated_findings, recurring_limitations, common_assumptions),
    matching the architecture doc's Landscape Output fields of the same names.

    A cluster of size 1 (nothing to merge) is kept as-is without an LLM call --
    only genuine near-duplicates spend a call. Clusters are kept even with
    one supporting paper; "recurring"/"common" in the field names describes
    the field's purpose, not a minimum-support filter -- downstream stages
    (e.g. underexplored_regimes) use low support as a signal, which requires
    keeping single-paper items visible rather than dropping them here.
    """
    statements = _collect_statements(cards)
    if not statements:
        return [], [], []

    embed_provider = embed_provider or os.environ.get("LANDSCAPE_AGGREGATION_EMBED_PROVIDER", DEFAULT_EMBED_PROVIDER)
    # DEFAULT_MODELS, not the global EMBED_MODEL -- see landscape/normalize.py
    # for why this provider must stay independent of the corpus embedding config.
    embed_model = os.environ.get("LANDSCAPE_AGGREGATION_EMBED_MODEL") or DEFAULT_MODELS[embed_provider]
    vectors = await asyncio.to_thread(
        embed_texts, [s.text for s in statements], embed_provider, embed_model, None)
    clusters = await asyncio.to_thread(_cluster, statements, vectors, threshold)

    if client is not None and provider is not None:
        raise ValueError("Pass provider or client, not both")
    owned = client is None
    if owned:
        client = AsyncLLMClient(provider or os.environ.get("LANDSCAPE_AGGREGATION_PROVIDER"))
    try:
        findings, limitations, assumptions = [], [], []
        # The client's semaphore bounds concurrent provider calls. Drain all
        # summaries before closing an owned client, even when one fails.
        summaries = await asyncio.gather(
            *(_summarize_cluster(c, client=client, model=model) for c in clusters),
            return_exceptions=True)
        for result in summaries:
            if isinstance(result, BaseException):
                raise result
        for cluster, statement in zip(clusters, summaries):
            item = AggregatedItem(
                statement=statement,
                supporting_papers=sorted({s.paper_id for s in cluster}))
            kind = cluster[0].kind
            if kind == "finding":
                findings.append(item)
            elif kind == "limitation":
                limitations.append(item)
            else:
                assumptions.append(item)
    finally:
        if owned:
            await client.raw.close()

    return findings, limitations, assumptions
