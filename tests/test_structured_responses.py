"""Transport tests use fake SDK endpoints; no provider calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel

from llm_client.client import AsyncLLMClient, LLMClient
from llm_client.structured import structured_request


class Example(BaseModel):
    value: str


def response(text='{"value":"ok"}', status='completed', reason=None):
    return SimpleNamespace(output_text=text, status=status,
                           incomplete_details=SimpleNamespace(reason=reason),
                           model='test-model', usage=None)


def client(asynchronous=False, result=None):
    cls = AsyncLLMClient if asynchronous else LLMClient
    instance = object.__new__(cls)
    instance.config = {'default_model': 'test', 'max_tokens': 200,
                       'output_token_limit': 100}
    call = AsyncMock(return_value=result or response()) if asynchronous else Mock(return_value=result or response())
    instance._client = SimpleNamespace(responses=SimpleNamespace(create=call))
    instance._semaphore = asyncio.Semaphore(1)
    return instance, call


def test_schema_is_separate_and_repair_history_is_preserved():
    messages = [{'role': 'system', 'content': 'Review evidence.'},
                {'role': 'user', 'content': '{"task":"review"}'},
                {'role': 'assistant', 'content': 'invalid prior output'},
                {'role': 'user', 'content': 'Repair the reported error.'}]
    request = structured_request(messages=messages, schema=Example)
    assert request['instructions'] == 'Review evidence.'
    assert request['input'] == messages[1:]
    assert request['text']['format']['schema'] == Example.model_json_schema()
    assert request['text']['format']['type'] == 'json_schema'
    assert 'strict' not in request['text']['format']  # Match the tested notebook call.


def test_sync_metadata_and_output_cap():
    instance, call = client()
    result = instance.structured_result('input', schema=Example)
    assert result.text == '{"value":"ok"}'
    assert result.model == 'test-model'
    assert result.finish_reason == 'completed'
    assert call.call_args.kwargs['max_output_tokens'] == 100
    assert 'response_format' not in call.call_args.kwargs
    assert call.call_args.kwargs['input'] == 'input'
    assert instance.respond('input') == result.text


def test_async_metadata_is_per_response_and_incomplete_is_rejected_for_text():
    async def exercise():
        instance, call = client(True, response('{"value":', 'incomplete', 'max_output_tokens'))
        result = await instance.structured_result('input', schema=Example)
        assert result.truncated
        assert result.text == '{"value":'
        with pytest.raises(ValueError, match='did not complete'):
            await instance.structured('input', schema=Example)
        call.return_value = response()
        assert await instance.structured('input', schema=Example) == '{"value":"ok"}'
        assert result.truncated  # Later calls cannot overwrite earlier metadata.
    asyncio.run(exercise())


def test_provider_error_has_no_chat_fallback():
    async def exercise():
        instance, call = client(True)
        call.side_effect = RuntimeError('unsupported schema')
        with pytest.raises(RuntimeError, match='unsupported schema'):
            await instance.structured('input', schema=Example)
        assert call.await_count == 1
    asyncio.run(exercise())


def test_empty_refusal_is_not_accepted():
    instance, _ = client(result=response(''))
    with pytest.raises(ValueError, match='empty or refused'):
        instance.structured('input', schema=Example)

def test_context_retry_reduces_output_only_and_keeps_schema():
    class ContextError(Exception):
        status_code = 400

    async def exercise():
        instance, call = client(True)
        instance.config['output_token_limit'] = 50000
        call.side_effect = [ContextError('maximum context length is 100000 tokens (80000 in the messages,'), response()]
        await instance.structured_result('all source evidence', schema=Example, max_tokens=50000)
        first, second = [c.kwargs for c in call.call_args_list]
        assert first['max_output_tokens'] == 50000
        assert second['max_output_tokens'] == 11808
        assert first['input'] == second['input'] == 'all source evidence'
        assert first['text'] == second['text']
        assert call.await_count == 2
    asyncio.run(exercise())


def test_schema_counts_toward_provider_context_budget():
    instance, call = client()
    instance.config['context_token_limit'] = 10000
    large = {'type': 'object', 'description': 'source context ' * 15000}
    with pytest.raises(ValueError, match='context_budget'):
        instance.structured_result('small input', schema=large)
    call.assert_not_called()
