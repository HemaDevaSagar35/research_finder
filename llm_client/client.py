"""Unified LLM client for OpenAI, Gemini, and DeepSeek.

All three providers expose an OpenAI-compatible chat completions API, so a
single code path (the `openai` SDK pointed at different base URLs) covers all
of them. OpenAI's newer Responses API is also supported via respond().

Configuration comes from environment variables (a .env file at the repo root
is loaded automatically; see .env.example). Pick the active provider with:

    PROVIDER=openai | gemini | deepseek

Each provider has its own block of settings, and the selected provider's
block is what takes effect ({P} is OPENAI, GEMINI, or DEEPSEEK):

    {P}_API_KEY       api key (required)
    {P}_MODEL         default model
    {P}_MAX_TOKENS    default max output tokens (chat and respond)
    {P}_TEMPERATURE   default sampling temperature
    {P}_BASE_URL      override the API endpoint

LLM_CONCURRENCY caps in-flight async requests (default 8). Explicit function
arguments always take precedence over env defaults.

Sync usage:
    from llm_client import LLMClient, chat

    text = chat("Say hi in one word.", model="gemini-2.5-flash")
    text = LLMClient("openai").respond("Say hi.")   # Responses API

Async usage (concurrency-limited):
    from llm_client import AsyncLLMClient, chat_many

    client = AsyncLLMClient("deepseek", concurrency=16)
    text = await client.chat("Summarize this abstract...")
    texts = await client.chat_many(["prompt 1", "prompt 2", ...])

    # or from sync code, fan out a batch in one call:
    texts = chat_many(["prompt 1", "prompt 2"], model="deepseek-chat")
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_PROVIDER_DEFAULTS = {
    "openai": {"base_url": None, "default_model": "gpt-5.2"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "default_model": "gemini-2.5-flash"},
    "deepseek": {"base_url": "https://api.deepseek.com",
                 "default_model": "deepseek-chat"},
}


def _provider_config(name: str) -> dict:
    defaults = _PROVIDER_DEFAULTS[name]
    prefix = name.upper()
    max_tokens = os.environ.get(f"{prefix}_MAX_TOKENS")
    temperature = os.environ.get(f"{prefix}_TEMPERATURE")
    return {
        "env_key": f"{prefix}_API_KEY",
        "base_url": os.environ.get(f"{prefix}_BASE_URL") or defaults["base_url"],
        "default_model": os.environ.get(f"{prefix}_MODEL")
                         or defaults["default_model"],
        "max_tokens": int(max_tokens) if max_tokens else None,
        "temperature": float(temperature) if temperature else None,
    }


PROVIDERS = list(_PROVIDER_DEFAULTS)

DEFAULT_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "8"))


def infer_provider(model: str) -> str:
    name = model.lower()
    if name.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith("deepseek"):
        return "deepseek"
    raise ValueError(f"Cannot infer provider from model {model!r}; "
                     f"pass provider= explicitly. Known: {PROVIDERS}")


def _resolve(provider: str | None, api_key: str | None) -> tuple[str, dict, str]:
    provider = provider or os.environ.get("PROVIDER")
    if not provider:
        raise ValueError("No provider: pass provider= or set PROVIDER.")
    provider = provider.lower()
    if provider not in _PROVIDER_DEFAULTS:
        raise ValueError(f"Unknown provider {provider!r}. Known: {PROVIDERS}")
    config = _provider_config(provider)
    api_key = api_key or os.environ.get(config["env_key"])
    if not api_key:
        raise RuntimeError(
            f"No API key for {provider}: set the {config['env_key']} "
            f"environment variable (e.g. in .env) or pass api_key=.")
    return provider, config, api_key


def _chat_params(prompt, messages, model, config, system,
                 temperature, max_tokens, kwargs) -> dict:
    if (prompt is None) == (messages is None):
        raise ValueError("Pass exactly one of prompt= or messages=.")
    if messages is None:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

    if temperature is None:
        temperature = config["temperature"]
    if max_tokens is None:
        max_tokens = config["max_tokens"]

    params = {"model": model or config["default_model"], "messages": messages}
    if temperature is not None:
        params["temperature"] = temperature
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    params.update(kwargs)
    return params


def _response_params(input, model, config, instructions,
                     max_output_tokens, kwargs) -> dict:
    if max_output_tokens is None:
        max_output_tokens = config["max_tokens"]

    params = {"model": model or config["default_model"], "input": input}
    if instructions is not None:
        params["instructions"] = instructions
    if max_output_tokens is not None:
        params["max_output_tokens"] = max_output_tokens
    params.update(kwargs)
    return params


class LLMClient:
    """Synchronous client bound to one provider."""

    def __init__(self, provider: str | None = None, api_key: str | None = None):
        provider, config, api_key = _resolve(provider, api_key)
        self.provider = provider
        self.config = config
        self.default_model = config["default_model"]
        self._client = OpenAI(api_key=api_key, base_url=config["base_url"])

    def chat(self, prompt: str | None = None, *,
             messages: list[dict] | None = None,
             model: str | None = None,
             system: str | None = None,
             temperature: float | None = None,
             max_tokens: int | None = None,
             **kwargs) -> str:
        """Chat Completions API. Pass either a plain `prompt` string or a
        full `messages` list; returns the response text."""
        params = _chat_params(prompt, messages, model, self.config,
                              system, temperature, max_tokens, kwargs)
        response = self._client.chat.completions.create(**params)
        return response.choices[0].message.content

    def respond(self, input, *,
                model: str | None = None,
                instructions: str | None = None,
                max_output_tokens: int | None = None,
                **kwargs) -> str:
        """Responses API (OpenAI's newer endpoint). `input` is a string or a
        structured input list; returns the response text.

        Note: only OpenAI supports this endpoint. Gemini and DeepSeek will
        reject it; use chat() with those providers.
        """
        params = _response_params(input, model, self.config,
                                  instructions, max_output_tokens, kwargs)
        response = self._client.responses.create(**params)
        return response.output_text

    @property
    def raw(self) -> OpenAI:
        """The underlying OpenAI SDK client, for anything chat()/respond()
        don't cover (streaming, tool calls, embeddings, ...)."""
        return self._client


class AsyncLLMClient:
    """Async client bound to one provider, with a cap on in-flight requests.

    The cap comes from `concurrency=`, falling back to the LLM_CONCURRENCY
    environment variable (default 8). All calls made through this client
    share the same limit, including chat_many batches.
    """

    def __init__(self, provider: str | None = None, api_key: str | None = None,
                 concurrency: int | None = None):
        provider, config, api_key = _resolve(provider, api_key)
        self.provider = provider
        self.config = config
        self.default_model = config["default_model"]
        self.concurrency = max(1, concurrency or DEFAULT_CONCURRENCY)
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._client = AsyncOpenAI(api_key=api_key, base_url=config["base_url"])

    async def chat(self, prompt: str | None = None, *,
                   messages: list[dict] | None = None,
                   model: str | None = None,
                   system: str | None = None,
                   temperature: float | None = None,
                   max_tokens: int | None = None,
                   **kwargs) -> str:
        """Async version of LLMClient.chat; respects the concurrency limit."""
        params = _chat_params(prompt, messages, model, self.config,
                              system, temperature, max_tokens, kwargs)
        async with self._semaphore:
            response = await self._client.chat.completions.create(**params)
        return response.choices[0].message.content

    async def respond(self, input, *,
                      model: str | None = None,
                      instructions: str | None = None,
                      max_output_tokens: int | None = None,
                      **kwargs) -> str:
        """Async version of LLMClient.respond (OpenAI Responses API);
        respects the concurrency limit."""
        params = _response_params(input, model, self.config,
                                  instructions, max_output_tokens, kwargs)
        async with self._semaphore:
            response = await self._client.responses.create(**params)
        return response.output_text

    async def chat_many(self, prompts: list[str],
                        return_exceptions: bool = False,
                        **kwargs) -> list:
        """Run many prompts concurrently (at most `concurrency` in flight).

        Returns responses in the same order as `prompts`. With
        return_exceptions=True, failed calls yield the exception object
        instead of raising.
        """
        tasks = [self.chat(prompt, **kwargs) for prompt in prompts]
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)

    @property
    def raw(self) -> AsyncOpenAI:
        """The underlying AsyncOpenAI SDK client."""
        return self._client


def _pick_provider(provider: str | None, model: str | None) -> str:
    """Explicit provider wins, then the PROVIDER env var, then inference
    from the model name."""
    provider = provider or os.environ.get("PROVIDER")
    if provider:
        return provider.lower()
    if model is None:
        raise ValueError("No model or provider: pass model=/provider= "
                         "or set PROVIDER.")
    return infer_provider(model)


def chat(prompt: str | None = None, *,
         model: str | None = None,
         provider: str | None = None,
         **kwargs) -> str:
    """One-off sync chat call; provider comes from provider=, the PROVIDER
    env var, or the model name. See LLMClient.chat for other arguments."""
    return LLMClient(_pick_provider(provider, model)).chat(
        prompt, model=model, **kwargs)


def chat_many(prompts: list[str], *,
              model: str | None = None,
              provider: str | None = None,
              concurrency: int | None = None,
              return_exceptions: bool = False,
              **kwargs) -> list:
    """Fan out many prompts concurrently from sync code and return the
    responses in order. Concurrency defaults to LLM_CONCURRENCY (or 8)."""
    client = AsyncLLMClient(_pick_provider(provider, model),
                            concurrency=concurrency)
    return asyncio.run(client.chat_many(
        prompts, model=model, return_exceptions=return_exceptions, **kwargs))
