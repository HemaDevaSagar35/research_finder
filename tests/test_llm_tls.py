"""Preload native certificate contexts rather than mutating them at handshake."""
import asyncio
import ssl
from llm_client.client import LLMClient, AsyncLLMClient


def test_native_verified_tls_contexts(monkeypatch):
    from llm_client import client as module
    sync_factory = module.DefaultHttpxClient
    async_factory = module.DefaultAsyncHttpxClient
    contexts = []
    def capture(factory):
        def create(**kwargs):
            ctx = kwargs['verify']
            assert type(ctx) is ssl.SSLContext
            assert ctx.verify_mode == ssl.CERT_REQUIRED
            assert ctx.check_hostname
            assert ctx.cert_store_stats()['x509_ca'] > 0
            contexts.append(ctx)
            return factory(**kwargs)
        return create
    monkeypatch.setattr(module, 'DefaultHttpxClient', capture(sync_factory))
    monkeypatch.setattr(module, 'DefaultAsyncHttpxClient', capture(async_factory))
    sync = LLMClient('deepseek', api_key='offline-test')
    sync.raw.close()
    async def check():
        client = AsyncLLMClient('deepseek', api_key='offline-test', concurrency=100)
        assert client.concurrency == 100
        await client.raw.close()
    asyncio.run(check())
    assert len(contexts) == 2
