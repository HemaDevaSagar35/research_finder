"""Synthetic corpus + landscape for offline runs and tests (no S3, no LLM).

Four small papers about MoE expert caching/offloading, built on top of
indexing.index_contract.synthetic_paper() so every paper.json validates
against PaperAnalysis. Each has three markdown pages whose text carries
the claims the paper.json records point at, so the support review has
something real to read. Papers are named by the canonical paper_id
(indexing.ids.paper_id) exactly like extraction output.

    uv run python -m reasoning.fixtures --out reasoning/fixtures_demo
    uv run python -m reasoning.cross_paper --landscape reasoning/fixtures_demo/landscape.json \
        --root reasoning/fixtures_demo --chat fake --out /tmp/out.json
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from indexing.ids import paper_id as make_paper_id
from indexing.index_contract import synthetic_paper
from reasoning.schemas import (ConceptGroup, Contradiction, LegacyLandscape as Landscape, LandscapeItem,
                               PaperRef, Relationship)

VENUE, YEAR = "ICML 2026", 2026


def _loc(page: int, section: str | None = None, table: str | None = None) -> dict:
    return {"page": page, "section": section, "table": table, "figure": None,
            "equation": None, "appendix": None}


def _base(title: str) -> dict:
    paper, _ = synthetic_paper()
    paper = copy.deepcopy(paper)
    pm = paper["paper_metadata"]
    pm.update(title=title, venue=VENUE, year=YEAR, authors=["A. Author", "B. Author"],
              research_areas=["Mixture-of-Experts Systems", "LLM Inference and Serving"],
              keywords=["MoE", "expert caching", "inference"])
    # Empty the sentinel-filled lists we will fill with meaningful content so
    # the bundle is not polluted by 'S<path>' strings.
    for key in ("key_results", "experiments", "ablations", "scaling_and_sensitivity",
                "interesting_findings", "failure_cases", "claims_and_evidence",
                "contributions", "metrics"):
        paper[key] = []
    paper["limitations"] = {"author_stated": [], "inferred": []}
    paper["future_work"] = {"author_proposed": [], "inferred_research_opportunities": []}
    paper["method"]["components"] = []
    paper["method"]["high_level_idea"] = ""
    paper["method"]["architecture"]["description"] = ""
    paper["experimental_setup"]["hardware"] = []
    paper["experimental_setup"]["software"] = []
    paper["experimental_setup"]["other_details"] = []
    paper["experimental_setup"]["number_of_runs"] = 3
    paper["research_gap"]["gap_description"] = ""
    paper["research_gap"]["limitations_of_prior_work"] = []
    sysc = paper["method"]["systems_characteristics"]
    for key in list(sysc):
        if key == "source_locations":
            continue
        sysc[key] = [] if key == "other" else None
    _normalise_locations(paper)
    return paper


_LOC_KEYS = {"page", "section", "table", "figure", "equation", "appendix"}


def _normalise_locations(node) -> None:
    """The synthetic base fills every SourceLocation with sentinels (page 7,
    'S<...>' strings). Point them all at page 1 so every record resolves
    to a page that exists in the fixture."""
    if isinstance(node, dict):
        if set(node) == _LOC_KEYS:
            node.update(page=1, section=None, table=None, figure=None,
                        equation=None, appendix=None)
            return
        for v in node.values():
            _normalise_locations(v)
    elif isinstance(node, list):
        for v in node:
            _normalise_locations(v)


def _metric(name: str, what: str, direction: str) -> dict:
    return {"name": name, "description": what, "what_it_measures": what,
            "better_direction": direction, "source_locations": [_loc(2)]}


def _experiment(name, setup, comparison, results, key_result, conclusion, page,
                strength="strong", datasets=None, metrics=None):
    return {"experiment_name": name, "research_question": name,
            "hypothesis_being_tested": None, "setup": setup, "comparison": comparison,
            "datasets": datasets or ["WikiText-103"], "models": ["Mixtral-8x7B"],
            "baselines": ["no caching"], "metrics": metrics or ["latency"],
            "results": results, "key_result": key_result,
            "authors_conclusion": conclusion, "evidence_supports_conclusion": strength,
            "source_locations": [_loc(page, table="Table 1")]}


def _result(method, metric, value, raw, unit, setting):
    return {"method": method, "metric": metric, "value": value, "raw_value": raw,
            "unit": unit, "dataset_or_setting": setting}


# --------------------------------------------------------------------------
# The four papers
# --------------------------------------------------------------------------

def paper_caching_locality() -> tuple[dict, list[str]]:
    p = _base("Locality-Aware Expert Caching for Mixture-of-Experts Inference")
    p["method"]["high_level_idea"] = ("Keep recently routed experts resident on the GPU "
                                      "and evict with an LRU policy tuned to routing locality.")
    p["method"]["architecture"]["description"] = "GPU-resident expert cache in front of CPU-offloaded experts."
    p["method"]["architecture"]["source_locations"] = [_loc(2, "Method")]
    p["experimental_setup"]["hardware"] = ["1x NVIDIA A100 80GB", "PCIe 4.0"]
    p["experimental_setup"]["source_locations"] = [_loc(2, "Setup")]
    p["metrics"] = [_metric("latency", "per-token decode latency", "lower")]
    p["key_results"] = [{
        "finding": "Expert caching reduces expert-transfer latency by 41% relative to no caching "
                   "when routing entropy is low (below 2 bits).",
        "quantitative_result": "41% lower per-token latency", "comparison": "vs. no caching",
        "importance": "Main result", "source_locations": [_loc(2, "Results", "Table 1")]}]
    p["experiments"] = [_experiment(
        "Latency under low-entropy routing", "Mixtral-8x7B, batch 1, A100, PCIe 4.0",
        "cache vs no cache",
        [_result("expert cache", "latency", 23.0, "23.0", "ms/token", "entropy < 2 bits"),
         _result("no cache", "latency", 39.0, "39.0", "ms/token", "entropy < 2 bits")],
        "Caching cuts latency from 39.0 to 23.0 ms/token.", "Locality makes caching effective.", 2)]
    p["ablations"] = [{
        "component_or_variable": "cache capacity", "change": "8 -> 4 resident experts",
        "motivation": "memory pressure", "baseline_setting": "8 experts",
        "modified_setting": "4 experts",
        "result": "Latency benefit shrinks from 41% to 27% with half the cache capacity.",
        "quantitative_change": "-14 points", "interpretation": "Benefit depends on capacity.",
        "source_locations": [_loc(3, "Ablations", "Table 2")]}]
    p["limitations"]["author_stated"] = [{
        "limitation": "All experiments use a single A100 GPU; other hardware was not evaluated.",
        "impact": "Transfer costs differ on consumer PCIe 3.0 systems.",
        "source_locations": [_loc(3, "Limitations")]}]
    p["claims_and_evidence"] = [{
        "claim": "Expert caching reduces expert-transfer latency under locality.",
        "claim_type": "efficiency", "evidence": ["Table 1"], "source_locations": [_loc(2)],
        "evidence_strength": "strong", "reasoning": "Direct measurement."}]
    pages = [
        "# Locality-Aware Expert Caching for Mixture-of-Experts Inference\n\n## Abstract\n"
        "We cache recently routed experts on the GPU. Under low routing entropy, caching "
        "substantially reduces expert-transfer latency.",
        "## Method\nA GPU-resident expert cache sits in front of CPU-offloaded experts with an "
        "LRU policy tuned to routing locality.\n\n## Setup\nAll runs use one NVIDIA A100 80GB "
        "over PCIe 4.0 with Mixtral-8x7B at batch size 1.\n\n## Results\n"
        "Table 1: with routing entropy below 2 bits, per-token latency drops from 39.0 ms "
        "(no cache) to 23.0 ms (expert cache), a 41% reduction.",
        "## Ablations\nTable 2: halving cache capacity from 8 to 4 resident experts shrinks the "
        "latency benefit from 41% to 27%.\n\n## Limitations\nAll experiments use a single A100 "
        "GPU; other hardware was not evaluated. Transfer costs differ on PCIe 3.0 systems.",
    ]
    return p, pages


def paper_small_cache() -> tuple[dict, list[str]]:
    p = _base("Small-Cache Expert Residency Policies for Offloaded MoE Serving")
    p["method"]["high_level_idea"] = "Frequency-based residency scoring for a small GPU expert cache."
    p["method"]["architecture"]["description"] = "Residency scorer + 8-slot GPU expert cache."
    p["method"]["architecture"]["source_locations"] = [_loc(2)]
    p["experimental_setup"]["hardware"] = ["1x NVIDIA A100 40GB", "PCIe 4.0"]
    p["experimental_setup"]["source_locations"] = [_loc(2)]
    p["metrics"] = [_metric("PCIe transfers", "expert parameter bytes moved host-to-GPU per token", "lower")]
    p["key_results"] = [{
        "finding": "An 8-slot expert cache reduces PCIe expert transfers by 35% and per-token "
                   "latency by 30% on Mixtral-8x7B.",
        "quantitative_result": "35% fewer transfers, 30% lower latency", "comparison": "vs. no caching",
        "importance": "Main result", "source_locations": [_loc(2, "Results", "Table 1")]}]
    p["experiments"] = [_experiment(
        "Transfer volume", "Mixtral-8x7B, A100 40GB, batch 4", "8-slot cache vs none",
        [_result("8-slot cache", "PCIe transfers", 0.65, "0.65x", "relative", "batch 4"),
         _result("no cache", "PCIe transfers", 1.0, "1.0x", "relative", "batch 4")],
        "Cache moves 35% fewer bytes.", "Even a small cache pays off.", 2,
        metrics=["PCIe transfers"])]
    p["limitations"]["inferred"] = [{
        "limitation": "Only one model family (Mixtral) is evaluated, so generality is unknown.",
        "reasoning": "No other MoE architectures appear in the experiments.",
        "severity": "medium", "evidence": "Table 1 lists Mixtral-8x7B only.",
        "source_locations": [_loc(3)]}]
    pages = [
        "# Small-Cache Expert Residency Policies for Offloaded MoE Serving\n\n## Abstract\n"
        "A frequency-based residency score decides which experts stay on the GPU.",
        "## Results\nTable 1: an 8-slot expert cache reduces PCIe expert transfers by 35% "
        "(0.65x) and per-token latency by 30% on Mixtral-8x7B at batch size 4 on one A100 40GB.",
        "## Discussion\nWe evaluate Mixtral-8x7B only; other MoE architectures are left to future work.",
    ]
    return p, pages


def paper_high_entropy() -> tuple[dict, list[str]]:
    p = _base("When Expert Caching Fails: Routing Entropy in Mixture-of-Experts Serving")
    p["method"]["high_level_idea"] = "Measure cache hit rate as a function of routing entropy."
    p["method"]["architecture"]["description"] = "Instrumented LRU expert cache with entropy probes."
    p["method"]["architecture"]["source_locations"] = [_loc(2)]
    p["experimental_setup"]["hardware"] = ["1x NVIDIA H100 80GB", "PCIe 5.0"]
    p["experimental_setup"]["source_locations"] = [_loc(2)]
    p["metrics"] = [_metric("cache hit rate", "fraction of expert requests served from GPU cache", "higher")]
    p["key_results"] = [{
        "finding": "The latency benefit of expert caching falls below 5% once routing entropy "
                   "exceeds 3.2 bits, because cache hit rate collapses.",
        "quantitative_result": "<5% benefit above 3.2 bits", "comparison": "vs. low-entropy workloads",
        "importance": "Main negative result", "source_locations": [_loc(2, "Results", "Table 1")]}]
    p["failure_cases"] = [{
        "failure": "Expert cache thrashes under high-entropy routing.",
        "conditions": "routing entropy > 3.2 bits, 8-slot LRU cache",
        "observed_behavior": "Hit rate below 20%; latency equals no-cache baseline.",
        "possible_cause": "Near-uniform expert selection defeats recency.",
        "implication": "Caching is workload-dependent.", "source_locations": [_loc(3, "Failure analysis")]}]
    p["interesting_findings"] = [{
        "finding": "Routing entropy varies by more than 2 bits across prompts of the same dataset.",
        "expected_or_unexpected": "unexpected", "why_interesting": "Workload is non-stationary.",
        "possible_implication": "Static cache policies cannot adapt.",
        "authors_discuss_implication": True, "source_locations": [_loc(3)]}]
    pages = [
        "# When Expert Caching Fails: Routing Entropy in Mixture-of-Experts Serving\n\n## Abstract\n"
        "We show that the benefit of expert caching depends on routing entropy.",
        "## Results\nTable 1: once routing entropy exceeds 3.2 bits, the latency benefit of an "
        "8-slot LRU expert cache falls below 5% because the cache hit rate collapses. "
        "Hardware: one H100 80GB, PCIe 5.0.",
        "## Failure analysis\nUnder entropy above 3.2 bits the cache thrashes: hit rate drops below "
        "20% and latency equals the no-cache baseline. Routing entropy varies by more than 2 bits "
        "across prompts of the same dataset.",
    ]
    return p, pages


def paper_offloading() -> tuple[dict, list[str]]:
    p = _base("Expert Offloading Trade-offs for MoE Inference on Consumer Hardware")
    p["method"]["high_level_idea"] = "Offload all experts to host memory and stream them on demand."
    p["method"]["architecture"]["description"] = "CPU-resident experts, GPU-resident attention."
    p["method"]["architecture"]["source_locations"] = [_loc(2)]
    p["experimental_setup"]["hardware"] = ["1x NVIDIA RTX 4090 24GB", "PCIe 4.0 x8"]
    p["experimental_setup"]["source_locations"] = [_loc(2)]
    p["metrics"] = [_metric("throughput", "tokens generated per second", "higher")]
    p["key_results"] = [{
        "finding": "Full expert offloading reduces peak GPU memory by 60% but adds 18 ms of "
                   "expert-transfer latency per token.",
        "quantitative_result": "-60% memory, +18 ms/token", "comparison": "vs. fully GPU-resident",
        "importance": "Main trade-off", "source_locations": [_loc(2, "Results", "Table 1")]}]
    p["experiments"] = [_experiment(
        "Throughput on consumer GPU", "Mixtral-8x7B, RTX 4090, PCIe 4.0 x8, batch 8",
        "offloaded vs resident",
        [_result("offloaded", "throughput", 11.0, "11", "tokens/s", "batch 8"),
         _result("resident (OOM)", "throughput", None, "OOM", None, "batch 8")],
        "Offloading is the only configuration that fits.", "Memory for latency.", 2,
        metrics=["throughput"])]
    p["limitations"]["author_stated"] = [{
        "limitation": "Results are specific to one consumer GPU and a PCIe 4.0 x8 link.",
        "impact": "Transfer latency will differ on server PCIe 5.0 links.",
        "source_locations": [_loc(3)]}]
    pages = [
        "# Expert Offloading Trade-offs for MoE Inference on Consumer Hardware\n\n## Abstract\n"
        "We study full expert offloading on a single consumer GPU.",
        "## Results\nTable 1: full expert offloading reduces peak GPU memory by 60% but adds 18 ms "
        "of expert-transfer latency per token; throughput is 11 tokens/s at batch 8 on an RTX 4090 "
        "over PCIe 4.0 x8, where the resident configuration runs out of memory.",
        "## Limitations\nResults are specific to one consumer GPU and a PCIe 4.0 x8 link; server "
        "PCIe 5.0 links will show different transfer latency.",
    ]
    return p, pages


PAPERS = [paper_caching_locality, paper_small_cache, paper_high_entropy, paper_offloading]


def build_demo_corpus(root: Path, *, drop_pages: dict[str, list[int]] | None = None
                      ) -> tuple[Landscape, dict[str, str]]:
    """Write paper.json + pages for the four papers under root/<paper_id>/ and
    return (landscape, {short_name: paper_id}). `drop_pages` = {short_name:
    [page numbers to leave out]} to simulate failed extraction pages."""
    root = Path(root)
    ids: dict[str, str] = {}
    for short, fn in zip(("P1", "P2", "P3", "P4"), PAPERS):
        paper, pages = fn()
        pid = make_paper_id(paper["paper_metadata"]["title"], VENUE, YEAR)
        ids[short] = pid
        folder = root / pid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "paper.json").write_text(json.dumps(paper, indent=2))
        for n, text in enumerate(pages, start=1):
            if n in (drop_pages or {}).get(short, []):
                continue
            (folder / f"{n:02d}.md").write_text(text)
    land = demo_landscape(ids)
    (root / "landscape.json").write_text(json.dumps(land.model_dump(), indent=2))
    return land, ids


def demo_landscape(ids: dict[str, str]) -> Landscape:
    P1, P2, P3, P4 = ids["P1"], ids["P2"], ids["P3"], ids["P4"]

    def ref(pid, page=2):
        return PaperRef(paper_id=pid, source_locations=[_loc(page)])

    return Landscape(
        schema_version="landscape_v1",
        topic="Efficient Mixture-of-Experts inference: expert caching and offloading",
        paper_ids=[P1, P2, P3, P4],
        groups=[
            ConceptGroup(group_id="g_caching", facet="method", concept="expert_caching",
                         aliases=["expert cache", "expert residency"], paper_ids=[P1, P2, P3]),
            ConceptGroup(group_id="g_transfer", facet="bottleneck", concept="expert_transfer_latency",
                         aliases=["PCIe transfers", "host-to-GPU loading"], paper_ids=[P1, P2, P4]),
            ConceptGroup(group_id="g_entropy", facet="regime", concept="high_routing_entropy",
                         aliases=["routing entropy", "non-stationary routing"], paper_ids=[P3]),
            ConceptGroup(group_id="g_offload", facet="method", concept="expert_offloading",
                         aliases=["CPU offload"], paper_ids=[P4]),
        ],
        items=[
            LandscapeItem(item_id="i_caching_reduces", kind="aggregated_finding",
                          statement="Expert caching reduces expert-transfer latency.",
                          group_ids=["g_caching", "g_transfer"], supporting=[ref(P1), ref(P2)]),
            LandscapeItem(item_id="i_single_hw", kind="recurring_limitation",
                          statement="Evaluations rely on a single hardware configuration.",
                          group_ids=["g_caching", "g_offload"], supporting=[ref(P1, 3), ref(P4, 3)]),
            LandscapeItem(item_id="i_nonstationary", kind="underexplored_regime",
                          statement="Cache behaviour under non-stationary routing is rarely studied.",
                          group_ids=["g_entropy"], supporting=[ref(P3, 3)]),
        ],
        contradictions=[Contradiction(
            item_id="c_caching_entropy",
            side_a=LandscapeItem(item_id="c_caching_entropy_a", kind="aggregated_finding",
                                 statement="Expert caching consistently lowers latency.",
                                 group_ids=["g_caching"], supporting=[ref(P1), ref(P2)]),
            side_b=LandscapeItem(item_id="c_caching_entropy_b", kind="aggregated_finding",
                                 statement="The caching benefit vanishes under high routing entropy.",
                                 group_ids=["g_entropy"], supporting=[ref(P3)]))],
        relationships=[
            Relationship(item_id="r_caching_reduces_transfer", source_group_id="g_caching",
                         relation="REDUCES", target_group_id="g_transfer",
                         supporting=[ref(P1), ref(P2)]),
            Relationship(item_id="r_offload_introduces_transfer", source_group_id="g_offload",
                         relation="INTRODUCES", target_group_id="g_transfer",
                         supporting=[ref(P4)]),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="Folder to write the corpus into")
    args = parser.parse_args()
    land, ids = build_demo_corpus(Path(args.out))
    print(f"Wrote {len(ids)} papers and landscape.json under {args.out}")
    for short, pid in ids.items():
        print(f"  {short} = {pid}")


if __name__ == "__main__":
    main()
