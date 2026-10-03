"""Attempt budget and token estimates for the reasoner.

The attempt counter is the *only* enforcement of --max-calls. It reserves a
slot before every request, so a request that raises, times out, or returns
without a usage block still consumed its attempt. `llm_client.usage` is not
used for enforcement: it records tokens after a successful response only.

Token counts are estimates for sizing prompts (they bound requests, not
provider billing). tiktoken (cl100k_base) is used when installed; otherwise
a characters/4 heuristic. Which one ran is recorded in the output's `run`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

try:                                    # optional dependency
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    TOKEN_COUNTER = "tiktoken:cl100k_base (estimate)"
except Exception:                       # noqa: BLE001 — any import problem → fallback
    _ENC = None
    TOKEN_COUNTER = "chars/4 (estimate)"


def estimate_tokens(text: str) -> int:
    if _ENC is not None:
        return len(_ENC.encode(text))
    return max(1, len(text) // 4)


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class AttemptBudget:
    """Atomic reservation counter shared by all concurrent threads."""
    max_calls: int
    counts: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def used(self) -> int:
        return sum(self.counts.values())

    @property
    def remaining(self) -> int:
        return self.max_calls - self.used

    async def reserve(self, kind: str) -> bool:
        """Take one slot for a request of `kind` (draft / repair / review /
        redraft). Returns False, without reserving, when the budget is spent."""
        async with self._lock:
            if self.used >= self.max_calls:
                return False
            self.counts[kind] = self.counts.get(kind, 0) + 1
            return True

    def snapshot(self) -> dict[str, int]:
        return {**{k: v for k, v in sorted(self.counts.items())},
                "total": self.used, "budget": self.max_calls}
