"""Reserve room for output without dropping input evidence."""
import json
import math
import os
import re

# Measured on DeepSeek runs: provider prompt_tokens / chars-per-4 estimate was
# 1.27-1.34 across three replays. A 10% margin on top of that estimate therefore
# under-reserved output by ~25%, and with reasoning tokens counted against
# max_output_tokens the model truncated with empty text. The fallback margin is
# calibrated to that measurement; a real tokenizer needs far less.
FALLBACK_MARGIN = 1.40
TOKENIZER_MARGIN = 1.15
FRAMING_TOKENS = 8192
# Below this the request is sent only to fail: thinking models spend tens of
# thousands of tokens reasoning before any JSON appears.
DEFAULT_MIN_OUTPUT_TOKENS = 32768


def estimate_margin():
    from reasoning.budget import TOKEN_COUNTER
    override = os.environ.get('LLM_TOKEN_ESTIMATE_MARGIN')
    if override:
        return float(override)
    return FALLBACK_MARGIN if TOKEN_COUNTER.startswith('chars/4') else TOKENIZER_MARGIN


def min_output_tokens(config):
    value = config.get('min_output_tokens')
    if value is None:
        value = os.environ.get('LLM_MIN_OUTPUT_TOKENS')
    return int(value) if value else DEFAULT_MIN_OUTPUT_TOKENS


def fit_context(params, config, key='max_tokens'):
    limit = config.get('context_token_limit')
    if not limit:
        return
    from reasoning.budget import estimate_tokens
    body = {k:v for k,v in params.items() if k not in (key,'model','temperature')}
    estimate = estimate_tokens(json.dumps(body,ensure_ascii=False))
    # Tokenizer differs by provider. Leave margin; never truncate evidence.
    reserved = math.ceil(estimate * estimate_margin())
    available = limit - reserved - FRAMING_TOKENS
    floor = min_output_tokens(config)
    if available < floor:
        raise ValueError(f'context_budget: estimated input {estimate} tokens (reserved {reserved}) leaves {max(available,0)} output tokens '
                         f'in a {limit}-token context, below the {floor}-token floor; reduce the request')
    params[key] = min(params.get(key) or available, available)


def reduce_after_context_error(exc, params, key='max_tokens'):
    """One correction using exact provider counts, only for context overflow."""
    if getattr(exc,'status_code',None) != 400:
        return False
    message = str(exc)
    limit = re.search(r'maximum context length is (\d+) tokens',message)
    inputs = re.search(r'\((\d+) in the messages,',message)
    if not limit or not inputs:
        return False
    available = int(limit[1])-int(inputs[1])-FRAMING_TOKENS
    old = params.get(key)
    if available < 1024 or old is None or available >= old:
        return False
    params[key] = available
    return True
