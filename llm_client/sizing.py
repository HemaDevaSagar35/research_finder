"""Reserve room for output without dropping input evidence."""
import json
import math
import re


def fit_context(params, config, key='max_tokens'):
    limit = config.get('context_token_limit')
    if not limit:
        return
    from reasoning.budget import estimate_tokens
    body = {k:v for k,v in params.items() if k not in (key,'model','temperature')}
    estimate = estimate_tokens(json.dumps(body,ensure_ascii=False))
    # Tokenizer differs by provider. Leave margin; never truncate evidence.
    available = limit - math.ceil(estimate * 1.10) - 8192
    if available < 1024:
        raise ValueError(f'context_budget: estimated input {estimate} leaves insufficient output room in {limit}-token context')
    params[key] = min(params.get(key) or available, available)


def reduce_after_context_error(exc, params):
    """One correction using exact provider counts, only for context overflow."""
    if getattr(exc,'status_code',None) != 400:
        return False
    message = str(exc)
    limit = re.search(r'maximum context length is (\d+) tokens',message)
    inputs = re.search(r'\((\d+) in the messages,',message)
    if not limit or not inputs:
        return False
    available = int(limit[1])-int(inputs[1])-8192
    old = params.get('max_tokens')
    if available < 1024 or old is None or available >= old:
        return False
    params['max_tokens'] = available
    return True
