"""Process-wide token accounting for cost analysis.

Every LLMClient / AsyncLLMClient call records the `usage` block of its
response here, keyed by model. Thread-safe, so it works across backfill's
worker threads and their per-thread event loops.

    from llm_client import usage
    ... run some work ...
    print(usage.report())          # table per model + totals
    usage.reset()                  # start a fresh measurement

Columns:
    prompt      input tokens (cached is the subset served from prefix cache,
                billed at a much lower rate; DeepSeek: prompt_cache_hit_tokens,
                OpenAI: prompt_tokens_details.cached_tokens)
    completion  output tokens as billed; `reasoning` is the subset spent on
                thinking (completion_tokens_details.reasoning_tokens) and is
                only >0 for models running in thinking mode
    truncated   responses whose finish_reason was "length" (hit max_tokens)
"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class Usage:
    calls: int = 0
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    reasoning: int = 0
    truncated: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt += other.prompt
        self.cached += other.cached
        self.completion += other.completion
        self.reasoning += other.reasoning
        self.truncated += other.truncated


_lock = threading.Lock()
_by_model: dict[str, Usage] = defaultdict(Usage)
_scoped: ContextVar[dict | None] = ContextVar("llm_usage_scope", default=None)


@contextmanager
def scoped():
    """Request-local accounting inherited by async child tasks; global totals persist."""
    totals = defaultdict(Usage)
    token = _scoped.set(totals)
    try:
        yield totals
    finally:
        _scoped.reset(token)



def _int(x) -> int:
    return int(x) if isinstance(x, (int, float)) else 0


def _was_truncated(response) -> bool:
    choices = getattr(response, "choices", None)
    if choices:
        return getattr(choices[0], "finish_reason", None) == "length"
    if getattr(response, "status", None) == "incomplete":
        details = getattr(response, "incomplete_details", None)
        return getattr(details, "reason", None) == "max_output_tokens"
    return False


def record(response, model: str | None = None) -> None:
    """Add the usage of one SDK response (chat completion or Responses API)."""
    u = getattr(response, "usage", None)
    if u is None:
        return
    model = getattr(response, "model", None) or model or "?"
    # Chat Completions names vs Responses API names.
    prompt = _int(getattr(u, "prompt_tokens", None) or getattr(u, "input_tokens", None))
    completion = _int(getattr(u, "completion_tokens", None)
                      or getattr(u, "output_tokens", None))
    cached = _int(getattr(u, "prompt_cache_hit_tokens", None))
    if not cached:
        details = (getattr(u, "prompt_tokens_details", None)
                   or getattr(u, "input_tokens_details", None))
        cached = _int(getattr(details, "cached_tokens", None))
    out_details = (getattr(u, "completion_tokens_details", None)
                   or getattr(u, "output_tokens_details", None))
    reasoning = _int(getattr(out_details, "reasoning_tokens", None))
    truncated = _was_truncated(response)
    with _lock:
        m = _by_model[model]
        m.calls += 1
        m.prompt += prompt
        m.cached += cached
        m.completion += completion
        m.reasoning += reasoning
        m.truncated += int(truncated)
        local = _scoped.get()
        if local is not None:
            local[model].add(Usage(calls=1, prompt=prompt, cached=cached,
                completion=completion, reasoning=reasoning, truncated=int(truncated)))


def snapshot() -> dict[str, Usage]:
    """Copy of the per-model tallies."""
    with _lock:
        return {k: Usage(**vars(v)) for k, v in _by_model.items()}


def total() -> Usage:
    t = Usage()
    for u in snapshot().values():
        t.add(u)
    return t


def reset() -> None:
    with _lock:
        _by_model.clear()


def report(per_unit: int | None = None, unit: str = "paper") -> str:
    """Human-readable table. With per_unit=N, also shows averages per unit
    (e.g. per paper) so the numbers can be multiplied out to a corpus."""
    snap = snapshot()
    if not snap:
        return "LLM usage: no calls recorded."

    def row(name: str, u: Usage) -> str:
        return (f"{name:<18}{u.calls:>7}{u.prompt:>14,}{u.cached:>12,}"
                f"{u.completion:>13,}{u.reasoning:>13,}{u.truncated:>10}")

    rows = [f"{'model':<18}{'calls':>7}{'prompt':>14}{'(cached)':>12}"
            f"{'completion':>13}{'(reasoning)':>13}{'truncated':>10}"]
    for model, u in sorted(snap.items()):
        rows.append(row(model, u))
    t = total()
    if len(snap) > 1:
        rows.append(row("TOTAL", t))
    if per_unit:
        rows.append(f"per {unit} (n={per_unit}): {t.calls / per_unit:.1f} calls, "
                    f"{t.prompt / per_unit:,.0f} prompt "
                    f"({t.cached / per_unit:,.0f} cached), "
                    f"{t.completion / per_unit:,.0f} completion "
                    f"({t.reasoning / per_unit:,.0f} reasoning) tokens")
    return "\n".join(rows)
