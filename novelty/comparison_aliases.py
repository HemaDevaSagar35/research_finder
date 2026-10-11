"""Short exact source labels for prior passages in comparison requests.

The assessment and critic stages already send ``srcN`` labels and map them back
to immutable passage IDs in code. The comparison stage sent raw
``paper#pN:hash12`` IDs; on real runs the model reproduced them with wrong
hashes or wrong paper prefixes, and each such slip cost a full-size repair
call before the pair was abandoned. Labels are exact identifiers, not
summaries: nothing about the passage text, offsets or hashes changes, and every
saved artifact keeps the original IDs.
"""
import re

ID_PATTERN = re.compile(r'[A-Za-z0-9_.-]+#p\d+:[0-9a-f]{12}')
ALIAS_PATTERN = re.compile(r'\bsrc\d+\b')
NOTE = ('\nSource labels: every prior_passages entry carries a short exact label (srcN) as its passage_id. '
        'Cite those labels unchanged in every passage_ids field; never reconstruct hashed IDs. '
        'candidate_source_passages keep their full IDs and are context, not prior-paper evidence.')


def alias_map(body):
    payload = body.get('payload') if isinstance(body, dict) else None
    if not isinstance(payload, dict) or not payload.get('prior_passages'):
        return {}
    return {p['passage_id']: f'src{i}' for i, p in enumerate(payload['prior_passages'])}


def rewrite(value, pattern, mapping):
    """Replace labelled identifiers inside every string, including dict keys."""
    if isinstance(value, dict):
        return {(rewrite(k, pattern, mapping) if isinstance(k, str) else k): rewrite(v, pattern, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite(v, pattern, mapping) for v in value]
    if isinstance(value, str):
        return pattern.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    return value


def alias_request(body, mapping):
    return rewrite(body, ID_PATTERN, mapping)


def restore_text(text, mapping):
    reverse = {alias: original for original, alias in mapping.items()}
    return rewrite(text, ALIAS_PATTERN, reverse)


def restore_response(parsed, mapping):
    reverse = {alias: original for original, alias in mapping.items()}
    return type(parsed).model_validate(rewrite(parsed.model_dump(), ALIAS_PATTERN, reverse))


class AliasedCalls:
    """Proxy for a NoveltySearcher that labels prior passages around ``_call`` only."""
    def __init__(self, io):
        object.__setattr__(self, '_io', io)

    def __getattr__(self, name):
        return getattr(self._io, name)

    def __setattr__(self, name, value):
        setattr(self._io, name, value)

    async def _call(self, task, system, schema, body):
        from novelty.pipeline import ModelOutputError
        mapping = alias_map(body)
        if not mapping:
            return await self._io._call(task, system, schema, body)
        try:
            parsed, model = await self._io._call(task, system + NOTE, schema, alias_request(body, mapping))
        except ModelOutputError as exc:
            raise ModelOutputError(restore_text(str(exc), mapping), restore_text(exc.raw, mapping)) from exc
        return restore_response(parsed, mapping), model
