"""Tell "this call failed" apart from "every call will fail from now on".

Batch drivers (ingestion.backfill) should stop the whole run on the second
kind — no credits, revoked key, no access to the model — instead of marking
hundreds of papers failed one by one. The OpenAI SDK does not retry these
(it retries 408/409/429/5xx only), so they surface immediately.

    from llm_client import errors
    try:
        ...
    except Exception as e:
        if errors.is_fatal(e):
            sys.exit(f"LLM provider rejected us: {errors.describe(e)}")
"""

import re

import openai

# HTTP statuses that mean "fix the account, not the request".
FATAL_STATUSES = {401, 402, 403}

# Provider-specific messages for the same conditions, in case the status
# code is generic (e.g. a 400 carrying "Insufficient Balance").
_FATAL_MESSAGE = re.compile(
    r"insufficient[ _](balance|quota|funds)|quota exceeded|exceeded your current quota"
    r"|billing|payment required|account (is )?(suspended|disabled)|invalid api key"
    r"|incorrect api key|authentication",
    re.I)


def _chain(exc: BaseException):
    """exc and everything it was raised from / during."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_fatal(exc: BaseException) -> bool:
    """True when the failure is an account/credential/quota problem that
    will make every subsequent LLM call fail the same way."""
    for e in _chain(exc):
        if isinstance(e, openai.APIStatusError):
            if e.status_code in FATAL_STATUSES:
                return True
            if _FATAL_MESSAGE.search(str(e)):
                return True
    return False


def describe(exc: BaseException) -> str:
    """Short, human-readable reason (status + provider message)."""
    for e in _chain(exc):
        if isinstance(e, openai.APIStatusError):
            body = e.body if isinstance(e.body, dict) else {}
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
            return f"HTTP {e.status_code}: {msg or str(e)}"
    return f"{type(exc).__name__}: {exc}"
