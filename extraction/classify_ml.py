"""Classify each paper in metadata.json as ML / NON_ML using an LLM, and
assign ML papers to research areas (see AREAS) with PRIMARY/SECONDARY
relevance.

The classifier reads the paper's title and abstract. The abstract comes from
metadata.json when the scraper captured it; otherwise (SIGIR, CVPR, ...) the
PDF is fetched from S3_PAPERS_URL (or read from papers/ if already local), the
abstract page is located, the abstract is extracted verbatim by the LLM, and
that text is classified. PDFs fetched this way are deleted afterwards — local
disk is only a cache, same as ingestion/worker.py. Papers with no abstract
and no PDF anywhere are skipped (no LLM call, no record) and reported; they
are retried on the next run, so uploading their PDFs later is enough.

PDFs are resolved with ingestion.pdf_locator.PdfLocator (one S3 listing;
handles both the <venueid>/<title>.pdf and <paper_id>.pdf layouts).

Results are stored in papers/ml_classification.json, one object per paper_id:

    {
      "<paper_id>": {
        "paper_id": "...", "title": "...", "venueid": "...",
        "label": "ML" | "NON_ML",      # UNCERTAIN only when the LLM call failed
        "confidence": 0.97,
        "primary_topic": "KV-cache-aware scheduling for efficient LLM inference",
        "research_areas": [            # [] unless ML; area names are from AREAS
          {"area": "LLM Inference and Serving", "relevance": "PRIMARY", "confidence": 0.97},
          {"area": "Efficient Training / ML Systems", "relevance": "SECONDARY", "confidence": 0.71}
        ],
        "reason": "one sentence",
        "abstract_source": "metadata" | "pdf",
        "abstract": "...",            # only when extracted from the PDF
        "model": "deepseek-flash",
        "classified_at": "2026-09-24T20:10:00Z"
      }, ...
    }

The file is saved incrementally (every ~30 s, every few hundred papers, and
on a single Ctrl-C), so an interrupted run resumes and reruns only classify
papers that have no entry yet.

Env configuration (falls back to CLASSIFY_*, then the main PROVIDER block):
    ML_CLASSIFY_PROVIDER   provider (e.g. deepseek)
    ML_CLASSIFY_MODEL      model (e.g. deepseek-flash)
    LLM_CONCURRENCY        in-flight LLM requests (default 8; --concurrency overrides)
    S3_PAPERS_URL          where PDFs live, for papers without an abstract

Usage:
    uv run python -m extraction.classify_ml --limit 20          # try a few first
    uv run python -m extraction.classify_ml --concurrency 100   # whole corpus, 100 in flight
    uv run python -m extraction.classify_ml --venues CVPR/2026/Main
    uv run python -m extraction.classify_ml --retry-uncertain   # re-ask UNCERTAIN ones
    uv run python -m extraction.classify_ml --reclassify --concurrency 500
                                            # relabel everything after a prompt/AREAS change
    uv run python -m extraction.classify_ml --summary           # counts per venue
    uv run python -m extraction.classify_ml --title "..." --abstract "..."
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_client import AsyncLLMClient

from ingestion.pdf_locator import PdfLocator, pdf_store

from .extract import page_count
from .pdf_to_markdown import page_text

REPO_ROOT = Path(__file__).resolve().parent.parent

# Labels the model may return. UNCERTAIN is never asked for; it is what a
# paper gets when the call itself fails (see classify()), so it can be
# retried with --retry-uncertain.
LABELS = ("ML", "NON_ML", "UNCERTAIN")

# Research areas for ML papers — the closed vocabulary for `research_areas`.
# Names must match the "# Step 2" headings in CLASSIFY_PROMPT.
AREAS = (
    "Foundation Models / LLM Architectures",
    "Reasoning and Post-Training",
    "Agents / Agentic Systems",
    "LLM Inference and Serving",
    "Efficient Training / ML Systems",
    "Mixture-of-Experts Systems",
    "RAG / Retrieval / Search",
    "Multimodal / VLM / Computer Vision",
    "Evaluation / Benchmarks / Reliability",
    "Alignment, Safety and Interpretability",
)
RELEVANCE = ("PRIMARY", "SECONDARY")

CLASSIFY_PROMPT = """You are an expert classifier of academic research papers.

Given a paper's **title and abstract**, perform two tasks:

1. Determine whether the paper is substantively about **Machine Learning / Artificial Intelligence**.
2. If it is ML/AI, classify it into one or more of the research areas defined below.

The goal is to separate a large collection of academic papers into **ML papers vs NON-ML papers**, and then organize the ML papers by research area.

# Step 1 — ML vs NON_ML

Classify the paper as:

- `ML`
- `NON_ML`

## Classify as ML when

Machine learning or artificial intelligence is a **substantive research contribution, research object, methodology, algorithm, model, system, evaluation target, or infrastructure focus** of the paper.

This includes, but is not limited to:

- machine learning
- deep learning
- neural networks
- foundation models / LLMs
- NLP
- computer vision
- multimodal learning
- speech/audio ML
- reinforcement learning
- representation learning
- generative models
- diffusion models
- transformers
- graph ML
- recommendation systems
- information retrieval / learned ranking
- agents
- reasoning
- RAG
- model alignment / post-training
- ML evaluation, reliability, robustness, interpretability
- causal ML
- probabilistic ML
- time-series ML
- distributed ML
- efficient training or inference
- ML serving systems
- ML compilers / kernels / runtimes
- pruning / quantization / distillation
- federated learning
- privacy-preserving ML
- continual learning
- self-supervised learning
- data-centric ML
- robotics where learning/AI is central

A paper can be ML even if its main contribution is a **systems contribution**, provided the system is specifically designed for training, inference, serving, scaling, or operating ML models.

## Classify as NON_ML when

ML/AI is absent or is merely an incidental tool used to answer a question in another field.

Examples:

- a medical study that uses an existing classifier to predict a clinical outcome
- a biology paper that applies random forests as one analysis method
- a physics paper that uses a neural network merely for regression
- an economics paper that uses an off-the-shelf ML model but contributes primarily to economics
- traditional databases, networking, distributed systems, compilers, or security research unrelated to ML
- pure mathematics
- conventional optimization unrelated to ML
- traditional signal processing without learning
- conventional control theory without a meaningful learning component

The important distinction is:

**Does the paper research ML/AI itself, or make a meaningful contribution to an ML/AI system? → ML**

**Does the paper merely use ML as a tool for research in another domain? → NON_ML**

When uncertain, infer only from what is actually stated in the title and abstract. Do not assume an unstated ML contribution.

---

# Step 2 — Research Area Classification

Only perform this step when the paper is classified as `ML`.

A paper can belong to **multiple research areas**.

Classify based on the paper's actual contribution, not merely terminology mentioned in the abstract.

## 1. Foundation Models / LLM Architectures

Includes:

- architectural improvements
- foundation-model architecture
- LLM architecture
- model design
- attention mechanisms
- representation learning
- scaling laws
- scaling/model design
- embeddings/tokenization
- architectural improvements to capability, generalization, or efficiency

## 2. Reasoning and Post-Training

Includes:

- LLM reasoning
- mathematical / logical / symbolic reasoning
- chain-of-thought
- inference-time reasoning
- test-time compute
- self-correction
- verification
- reflection
- fine-tuning
- post-training
- RLHF / reinforcement learning for LLMs
- preference optimization
- alignment training methods (RLHF, DPO, reward modelling)
- improving reasoning reliability

## 3. Agents / Agentic Systems

Includes:

- LLM agents
- planning
- tool use
- function calling
- multi-step agents
- multi-agent systems
- agent memory
- agent architectures
- computer-use agents
- coding agents
- web agents
- scientific agents
- agent evaluation
- agent reliability

## 4. LLM Inference and Serving

Includes:

- KV-cache management
- KV-cache compression
- KV-cache quantization
- KV-cache eviction
- batching
- request scheduling
- speculative decoding
- prefix caching
- long-context inference
- heterogeneous serving
- LoRA / multi-adapter serving
- prefill–decode disaggregation
- multimodal model serving
- distributed inference
- GPU utilization
- throughput optimization
- latency optimization
- memory-efficient inference
- inference engines
- model-serving systems

## 5. Efficient Training / ML Systems

Includes:

- distributed training
- large-scale training systems
- GPU efficiency
- kernels
- compilers
- ML runtimes
- memory optimization
- communication optimization
- parallel training
- data parallelism
- tensor parallelism
- pipeline parallelism
- checkpointing
- training efficiency
- hardware/software co-design for ML

## 6. Mixture-of-Experts Systems

Includes:

- MoE models
- expert routing
- expert selection
- load balancing
- expert parallelism
- communication overhead
- MoE memory bottlenecks
- efficient MoE inference
- efficient MoE training
- expert placement
- expert caching
- MoE serving / scheduling

## 7. RAG / Retrieval / Search

Includes:

- retrieval-augmented generation
- information retrieval
- dense retrieval
- sparse retrieval
- embedding retrieval
- retrieval quality
- ranking
- reranking
- search
- learned ranking
- recommendation systems
- retrieval + reasoning
- retrieval for LLMs
- query understanding
- indexing for ML retrieval systems
- generative retrieval / differentiable search indices
- probabilistic relevance / ranking models
- retrieval as a latent variable inside the model
- parametric or memory-augmented retrieval

## 8. Multimodal / VLM / Computer Vision

Includes:

- vision-language models
- multimodal foundation models
- multimodal reasoning
- multimodal representation learning
- multimodal alignment
- multimodal generation
- VLM architecture
- multimodal serving

Also includes broader computer vision research:

- vision transformers
- image understanding
- object detection
- segmentation
- image generation
- diffusion models
- video understanding
- video generation
- 3D vision
- visual representation learning

## 9. Evaluation / Benchmarks / Reliability

Includes:

- model evaluation
- evaluation methodology
- benchmarks
- benchmark weaknesses
- benchmark contamination / leakage
- robustness
- reliability
- hallucinations
- consistency
- reproducibility
- failure modes
- uncertainty
- calibration
- agent evaluation
- reasoning evaluation
- contradictions between reported results
- studies questioning whether benchmarks actually measure claimed capabilities

## 10. Alignment, Safety and Interpretability

Includes:

- mechanistic interpretability
- circuits / attention-head analysis / neuron analysis
- probing model representations
- activation patching / causal tracing
- sparse autoencoders / feature discovery / superposition
- steering vectors / representation engineering
- observing or manipulating model layers to change specific behaviours
- model editing / knowledge localization
- machine unlearning
- attribution and explanation methods (influence functions, saliency, concept-based)
- faithfulness of explanations
- safety: jailbreaks, red-teaming, refusal, harmful content, guardrails
- backdoors / trojans / data poisoning
- alignment behaviour: sycophancy, reward hacking, deception, value alignment
- analyzing how post-training changes model internals or behaviour

Does NOT include the training recipe itself (RLHF / DPO variants, reward
modelling) — that belongs to "Reasoning and Post-Training". A paper that
proposes an alignment method AND analyzes its effect on the model's internals
or safety behaviour may be PRIMARY in both.

---

# Classification Rules

1. First decide `ML` or `NON_ML`.
2. If `NON_ML`, do not assign any research areas.
3. If `ML`, assign every research area for which the paper makes a meaningful contribution.
4. Multi-label classification is allowed.
5. Do not classify a paper into an area merely because a technique is mentioned or used.
6. Distinguish between:
   - `PRIMARY`: a central contribution of the paper.
   - `SECONDARY`: a meaningful but non-primary contribution.
7. A paper may have multiple `PRIMARY` areas.
8. Do not force an ML paper into one of the listed areas.
9. If a paper is clearly ML but does not fit these areas well, keep it as `ML` and return an empty or partial `research_areas` list.
10. Be conservative and base the classification only on information available in the title and abstract.

# Output

Return ONLY valid JSON.

For an ML paper:

{
  "label": "ML",
  "confidence": 0.98,
  "primary_topic": "KV-cache-aware scheduling for efficient LLM inference",
  "research_areas": [
    {
      "area": "LLM Inference and Serving",
      "relevance": "PRIMARY",
      "confidence": 0.97
    },
    {
      "area": "Efficient Training / ML Systems",
      "relevance": "SECONDARY",
      "confidence": 0.71
    }
  ],
  "reason": "The paper proposes a serving algorithm for improving LLM inference throughput and memory efficiency."
}

For a NON_ML paper:

{
  "label": "NON_ML",
  "confidence": 0.96,
  "primary_topic": "Clinical prediction of cardiovascular outcomes",
  "research_areas": [],
  "reason": "The paper uses an existing machine-learning classifier as an analysis tool, while the research contribution is primarily clinical."
}

# Paper

Title:
{{TITLE}}

Abstract:
{{ABSTRACT}}
"""

ABSTRACT_PROMPT = """Below is the raw text of the first page(s) of an academic
paper. Find the paper's abstract and return it verbatim (fix only line-break
hyphenation and merge wrapped lines). Do not include the "Abstract" heading,
keywords, CCS concepts, author lists, footnotes, or the introduction.

Return ONLY valid JSON:

{"abstract": "..."}

If there is no abstract on these pages, return {"abstract": ""}.

Page text:
"""

# Providers whose OpenAI-compatible endpoint honours response_format=json_object.
JSON_MODE_PROVIDERS = {"openai", "deepseek"}

# How many leading pages to look at for the abstract; it is on page 1 for
# nearly every venue, occasionally page 2 (title-page-only layouts).
ABSTRACT_SEARCH_PAGES = 3

# Save results + print progress at least this often, whatever the
# concurrency, so a crash never loses more than ~30 s of work.
SAVE_INTERVAL_S = 30


# ------------------------------------------------------------------ helpers

def _json_kwargs(client: AsyncLLMClient) -> dict:
    if client.provider in JSON_MODE_PROVIDERS:
        return {"response_format": {"type": "json_object"}}
    return {}


def _extract_json(raw: str) -> dict | None:
    """Pull the first JSON object out of a model reply, tolerating code
    fences and stray prose around it."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return None
        text = m.group(0)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _norm(text: str) -> str:
    """Case/space/punctuation-insensitive key for matching area names."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


_AREA_BY_NORM = {_norm(a): a for a in AREAS}
# Short forms the model tends to use; keys are normalised.
_AREA_ALIASES = {
    "foundation models": AREAS[0], "llm architectures": AREAS[0],
    "llm architecture": AREAS[0], "architectures": AREAS[0],
    "reasoning": AREAS[1], "post training": AREAS[1], "rlhf": AREAS[1],
    "agents": AREAS[2], "agentic systems": AREAS[2],
    "inference": AREAS[3], "serving": AREAS[3], "llm inference": AREAS[3],
    "inference and serving": AREAS[3],
    "efficient training": AREAS[4], "ml systems": AREAS[4], "training systems": AREAS[4],
    "moe": AREAS[5], "mixture of experts": AREAS[5], "mixture of experts systems": AREAS[5],
    "rag": AREAS[6], "retrieval": AREAS[6], "search": AREAS[6],
    "information retrieval": AREAS[6], "recommendation": AREAS[6],
    "multimodal": AREAS[7], "vlm": AREAS[7], "computer vision": AREAS[7],
    "vision": AREAS[7],
    "evaluation": AREAS[8], "benchmarks": AREAS[8], "reliability": AREAS[8],
    "alignment": AREAS[9], "safety": AREAS[9], "interpretability": AREAS[9],
    "mechanistic interpretability": AREAS[9], "mech interp": AREAS[9],
    "alignment and safety": AREAS[9], "safety and interpretability": AREAS[9],
    "alignment safety and interpretability": AREAS[9],
}


def alias_area(text: str) -> str | None:
    """Canonical AREAS entry for an exact (normalised) name or a known
    short alias ('moe', 'rag', 'agents', ...); None otherwise. Unlike
    match_area this never guesses by substring, so callers can apply
    their own ambiguity rules."""
    norm = _norm(text)
    return _AREA_BY_NORM.get(norm) or _AREA_ALIASES.get(norm)


def match_area(text: str) -> str | None:
    """Map a model-returned area name onto the canonical AREAS entry: exact
    (normalised) match, then a known alias, then a heading that contains
    the string (e.g. 'LLM Inference and Serving' with stray words)."""
    norm = _norm(text)
    if not norm:
        return None
    exact = alias_area(text)
    if exact:
        return exact
    # Digits/enumeration like "4. LLM Inference and Serving"
    norm = re.sub(r"^\d+\s+", "", norm)
    for cand_norm, canonical in _AREA_BY_NORM.items():
        if norm in cand_norm or cand_norm in norm:
            return canonical
    return None


def _confidence(value, default: float = 0.0) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def parse_research_areas(value) -> list[dict]:
    """Canonical [{area, relevance, confidence}] from the model's list.
    Off-vocabulary areas are dropped; duplicates keep the first mention;
    a missing/unknown relevance is recorded as SECONDARY (the conservative
    reading of rule 6). Accepts bare strings as PRIMARY areas too."""
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item = {"area": item, "relevance": "PRIMARY"}
        if not isinstance(item, dict):
            continue
        area = match_area(item.get("area") or item.get("name") or "")
        if not area or area in seen:
            continue
        seen.add(area)
        relevance = str(item.get("relevance", "")).strip().upper()
        if relevance not in RELEVANCE:
            relevance = "SECONDARY"
        out.append({"area": area, "relevance": relevance,
                    "confidence": _confidence(item.get("confidence"))})
    return out


def parse_classification(raw: str) -> dict | None:
    """{label, confidence, primary_topic, research_areas, reason} from the
    classifier reply, or None when unusable. `research_areas` is always []
    unless label is ML."""
    obj = _extract_json(raw)
    if obj is None:
        return None
    label = str(obj.get("label", "")).strip().upper().replace("-", "_")
    if label not in ("ML", "NON_ML"):
        return None
    areas = parse_research_areas(obj.get("research_areas")) if label == "ML" else []
    return {"label": label,
            "confidence": _confidence(obj.get("confidence")),
            "primary_topic": str(obj.get("primary_topic", "")).strip(),
            "research_areas": areas,
            "reason": str(obj.get("reason", "")).strip()}


def build_prompt(title: str, abstract: str) -> str:
    # str.replace, not str.format: the prompt contains literal JSON braces.
    return (CLASSIFY_PROMPT
            .replace("{{TITLE}}", (title or "").strip() or "(untitled)")
            .replace("{{ABSTRACT}}", abstract.strip()))


# --------------------------------------------------------------- abstract

def find_abstract_page(pdf_path: Path) -> int:
    """1-based page most likely to hold the abstract: the first of the
    leading pages that mentions 'abstract'; page 1 if none does."""
    total = page_count(pdf_path)
    for n in range(1, min(total, ABSTRACT_SEARCH_PAGES) + 1):
        if re.search(r"\babstract\b", page_text(pdf_path, n), re.I):
            return n
    return 1


def _abstract_page_text(pdf_path: Path) -> str:
    """Blocking pymupdf work for one PDF: pick the abstract page and return
    its text (plus the next page when the first is a short title page)."""
    page = find_abstract_page(pdf_path)
    text = page_text(pdf_path, page)
    if len(text) < 1500 and page < page_count(pdf_path):
        text += "\n\n" + page_text(pdf_path, page + 1)
    return text


async def extract_abstract(client: AsyncLLMClient, pdf_path: Path, *,
                           model: str | None = None) -> str:
    """Locate the abstract page and have the LLM return the abstract
    verbatim. Empty string when the paper has none / extraction fails."""
    text = await asyncio.to_thread(_abstract_page_text, pdf_path)
    raw = await client.chat(ABSTRACT_PROMPT + text, model=model,
                            **_json_kwargs(client))
    obj = _extract_json(raw) or {}
    return re.sub(r"\s+", " ", str(obj.get("abstract", ""))).strip()


# ------------------------------------------------------------------ classify

async def classify(client: AsyncLLMClient, title: str, abstract: str,
                   *, model: str | None = None, retries: int = 1) -> dict:
    """Classify one title+abstract. Never raises on a bad reply: after
    `retries` unusable answers it returns UNCERTAIN with the failure as
    the reason and `error: True`."""
    prompt = build_prompt(title, abstract)
    last_error = "empty response"
    for _ in range(retries + 1):
        try:
            raw = await client.chat(prompt, model=model, **_json_kwargs(client))
        except Exception as e:  # noqa: BLE001 — surfaced in the record
            last_error = f"{type(e).__name__}: {e}"
            continue
        parsed = parse_classification(raw)
        if parsed:
            return parsed
        last_error = f"unparseable response: {(raw or '')[:120]!r}"
    return {"label": "UNCERTAIN", "confidence": 0.0, "primary_topic": "",
            "research_areas": [],
            "reason": f"classification failed ({last_error})", "error": True}




# ------------------------------------------------------------ one paper

async def classify_paper(meta: dict, client: AsyncLLMClient, *,
                         model: str | None = None,
                         locator: PdfLocator,
                         download_sem: asyncio.Semaphore | None = None,
                         cached_abstract: str | None = None
                         ) -> dict | None:
    """Full record for one metadata entry: abstract from metadata, else
    `cached_abstract` (one we extracted from the PDF on an earlier run),
    else extracted from the PDF now; then the classification. Returns None
    (and makes no LLM call) when there is no abstract to classify on — no
    abstract in metadata and no PDF found / no abstract in it. LLM calls
    are awaited; S3 + pymupdf run in threads."""
    record = {"paper_id": meta["paper_id"], "title": meta.get("title", ""),
              "venueid": meta.get("venueid", "")}

    abstract = (meta.get("abstract") or "").strip()
    if abstract:
        record["abstract_source"] = "metadata"
    elif (cached_abstract or "").strip():
        abstract = cached_abstract.strip()
        record["abstract_source"] = "pdf"
        record["abstract"] = abstract
    else:
        # Hold the slot for the PDF's whole life (download -> extract ->
        # delete), so at most `download_concurrency` PDFs are ever on disk.
        sem = download_sem or asyncio.Semaphore(1)
        async with sem:
            try:
                pdf, downloaded = await asyncio.to_thread(locator.fetch, meta)
            except Exception as e:  # noqa: BLE001 — S3 hiccup: skip paper
                print(f"[{meta['paper_id']}] PDF fetch failed: "
                      f"{type(e).__name__}: {e}", flush=True)
                return None
            if pdf is None:
                return None
            try:
                abstract = await extract_abstract(client, pdf, model=model)
            except Exception as e:  # noqa: BLE001 — skip paper
                print(f"[{meta['paper_id']}] abstract extraction failed: "
                      f"{type(e).__name__}: {e}", flush=True)
                abstract = ""
            finally:
                if downloaded:
                    pdf.unlink(missing_ok=True)
                    try:
                        pdf.parent.rmdir()  # only if we left it empty
                    except OSError:
                        pass
        if not abstract:
            return None
        record["abstract_source"] = "pdf"
        record["abstract"] = abstract

    record.update(await classify(client, record["title"], abstract, model=model))
    record["model"] = model or client.default_model
    record["classified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return record


# ------------------------------------------------------------ results file

def load_results(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text()) if path.exists() else {}


def save_results(path: Path, results: dict[str, dict]) -> None:
    """Atomic write so a crash mid-save can't truncate the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    tmp.replace(path)


def print_summary(results: dict[str, dict]) -> None:
    if not results:
        print("No classifications yet.")
        return
    counts = Counter(r["label"] for r in results.values())
    sources = Counter(r.get("abstract_source", "?") for r in results.values())
    errors = sum(1 for r in results.values() if r.get("error"))
    print(f"{len(results)} classified: "
          + ", ".join(f"{k}={counts.get(k, 0)}" for k in LABELS)
          + f"  | abstract from {dict(sources)}"
          + (f"  | {errors} failed calls recorded as UNCERTAIN" if errors else ""))
    by_venue: dict[str, Counter] = {}
    for r in results.values():
        by_venue.setdefault(r.get("venueid", "?"), Counter())[r["label"]] += 1
    for venue, c in sorted(by_venue.items()):
        print(f"  {venue:<45} {sum(c.values()):>6}  "
              + "  ".join(f"{k}={c.get(k, 0)}" for k in LABELS))
    primary: Counter = Counter()
    secondary: Counter = Counter()
    unassigned = 0
    for r in results.values():
        if r["label"] != "ML":
            continue
        areas = r.get("research_areas", [])
        if not areas:
            unassigned += 1
        for a in areas:
            (primary if a["relevance"] == "PRIMARY" else secondary)[a["area"]] += 1
    if primary or secondary or unassigned:
        print(f"Research areas (ML papers; {unassigned} ML papers with no area):")
        print(f"  {'':<40} {'PRIMARY':>8} {'SECONDARY':>10}")
        for area in AREAS:
            print(f"  {area:<40} {primary.get(area, 0):>8} {secondary.get(area, 0):>10}")


# ------------------------------------------------------------ batch driver

async def classify_many(entries: list[dict], client: AsyncLLMClient, *,
                        model: str | None, out_path: Path,
                        results: dict[str, dict],
                        locator: PdfLocator,
                        download_concurrency: int | None = None,
                        save_every: int | None = None,
                        cached_abstracts: dict[str, str] | None = None
                        ) -> Counter:
    """Classify `entries` concurrently, merging into `results` and saving
    to out_path every `save_every` papers (and at the end). Papers with
    nothing to classify on are counted under 'SKIPPED' and not recorded,
    so a later run (e.g. after more PDFs are uploaded) picks them up.
    `cached_abstracts` (paper_id -> abstract) are PDF abstracts from an
    earlier run, used instead of fetching the PDF again.

    download_concurrency defaults to the client's LLM concurrency, so the
    PDF path (S3 fetch + pymupdf, which run in threads) keeps pace with
    the LLM path instead of queueing behind asyncio's small default pool."""
    download_concurrency = download_concurrency or client.concurrency
    save_every = save_every or max(50, 2 * client.concurrency)
    counts: Counter = Counter()
    total = len(entries)
    start = time.time()
    last_save = start
    download_sem = asyncio.Semaphore(download_concurrency)
    cached_abstracts = cached_abstracts or {}
    lock = asyncio.Lock()
    # asyncio.to_thread uses the loop's default executor, which is only
    # min(32, cpu+4) threads; size it to what we actually want in flight.
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=download_concurrency,
                           thread_name_prefix="pdf"))

    async def one(meta: dict) -> None:
        nonlocal last_save
        record = await classify_paper(
            meta, client, model=model, locator=locator,
            download_sem=download_sem,
            cached_abstract=cached_abstracts.get(meta["paper_id"]))
        async with lock:
            if record is None:
                counts["SKIPPED"] += 1
            else:
                results[record["paper_id"]] = record
                counts[record["label"]] += 1
            n = sum(counts.values())
            now = time.time()
            if (n % save_every == 0 or n == total
                    or now - last_save >= SAVE_INTERVAL_S):
                last_save = now
                save_results(out_path, results)
                elapsed = now - start
                rate = n / max(elapsed, 1e-9)
                print(f"[{n}/{total}] {dict(counts)}  {rate * 60:.0f}/min, "
                      f"~{(total - n) / max(rate, 1e-9) / 60:.0f} min left",
                      flush=True)

    try:
        await asyncio.gather(*(one(m) for m in entries))
    finally:
        save_results(out_path, results)  # keep partial progress on Ctrl-C
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--metadata", default="papers/metadata.json")
    parser.add_argument("--papers-root", default="papers",
                        help="Local PDF cache / scraper output (default: papers)")
    parser.add_argument("--out", default="papers/ml_classification.json",
                        help="Results JSON keyed by paper_id "
                             "(default: papers/ml_classification.json)")
    parser.add_argument("--venues", nargs="+", default=None,
                        help="Only these venueids (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Classify at most this many unclassified papers")
    parser.add_argument("--retry-uncertain", action="store_true",
                        help="Also re-classify papers currently labeled UNCERTAIN")
    parser.add_argument("--reclassify", action="store_true",
                        help="Re-run the classifier on every selected paper, "
                             "ignoring existing labels (use after changing the "
                             "prompt / AREAS). Abstracts already extracted from "
                             "PDFs are reused, not re-downloaded. Back up --out "
                             "first if you want the old labels.")
    parser.add_argument("--provider", default=None,
                        help="LLM provider (default: ML_CLASSIFY_PROVIDER, "
                             "CLASSIFY_PROVIDER, or PROVIDER)")
    parser.add_argument("--model", default=None,
                        help="Model (default: ML_CLASSIFY_MODEL, CLASSIFY_MODEL, "
                             "or the provider's model)")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Concurrent LLM calls (default: LLM_CONCURRENCY or 8)")
    parser.add_argument("--download-concurrency", type=int, default=None,
                        help="Concurrent PDF fetches/parses (default: same as "
                             "--concurrency)")
    parser.add_argument("--summary", action="store_true",
                        help="Print label counts from --out and exit")
    parser.add_argument("--title", default=None,
                        help="One-off mode: classify this title + --abstract and exit")
    parser.add_argument("--abstract", default=None,
                        help="Abstract for one-off mode (required with --title)")
    args = parser.parse_args()
    if args.title is not None and not (args.abstract or "").strip():
        parser.error("--title needs --abstract; the classifier does not "
                     "run on a title alone.")

    out_path = REPO_ROOT / args.out
    if args.summary:
        print_summary(load_results(out_path))
        return

    provider = (args.provider or os.environ.get("ML_CLASSIFY_PROVIDER")
                or os.environ.get("CLASSIFY_PROVIDER"))
    model = (args.model or os.environ.get("ML_CLASSIFY_MODEL")
             or os.environ.get("CLASSIFY_MODEL"))
    client = AsyncLLMClient(provider, concurrency=args.concurrency)
    print(f"Provider: {client.provider} | model: {model or client.default_model}"
          f" | concurrency: {client.concurrency}")

    if args.title is not None:
        result = asyncio.run(classify(client, args.title, args.abstract, model=model))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    meta_path = REPO_ROOT / args.metadata
    if not meta_path.exists():
        sys.exit(f"Metadata not found: {meta_path}")
    entries = json.loads(meta_path.read_text())
    if args.venues:
        entries = [e for e in entries if e.get("venueid") in args.venues]
    missing = [e for e in entries if not e.get("paper_id")]
    if missing:
        sys.exit(f"{len(missing)} metadata entries have no paper_id "
                 f"(e.g. {missing[0].get('title')!r}).")

    results = load_results(out_path)
    # PDF abstracts we already paid to extract: reuse them whenever the
    # paper is classified again (--reclassify / --retry-uncertain).
    cached_abstracts = {pid: r["abstract"] for pid, r in results.items()
                        if r.get("abstract_source") == "pdf" and r.get("abstract")}
    seen: set[str] = set()
    todo = []
    for e in entries:
        pid = e["paper_id"]
        if pid in seen:
            continue
        seen.add(pid)
        prev = results.get(pid)
        if prev and not args.reclassify and not (
                args.retry_uncertain and prev["label"] == "UNCERTAIN"):
            continue
        todo.append(e)
    if args.limit:
        todo = todo[:args.limit]

    pdfs = pdf_store()
    locator = PdfLocator(REPO_ROOT / args.papers_root, pdfs)
    need_pdf = sum(1 for e in todo if not (e.get("abstract") or "").strip()
                   and e["paper_id"] not in cached_abstracts)
    print(f"{len(seen)} papers selected | {len(results)} already classified"
          + (" (ignored: --reclassify)" if args.reclassify else "")
          + f" | {len(todo)} to classify now ({need_pdf} need a PDF, "
          f"PDFs from {pdfs.url() if pdfs else args.papers_root}) -> {out_path}")
    if not todo:
        print_summary(results)
        return

    run_counts = asyncio.run(classify_many(
        todo, client, model=model, out_path=out_path, results=results,
        locator=locator, download_concurrency=args.download_concurrency,
        cached_abstracts=cached_abstracts))
    skipped = run_counts.pop("SKIPPED", 0)
    print(f"\nThis run: {dict(run_counts)}"
          + (f" | {skipped} skipped (no abstract in metadata and no PDF / "
             f"no abstract in PDF) — not recorded, will be retried next run"
             if skipped else ""))
    print_summary(load_results(out_path))


if __name__ == "__main__":
    main()
