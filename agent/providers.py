"""OpenAI structured-output transport for the Werewolf strategy agent.

An ``LLMProvider`` has one job: send chat messages and return a JSON object
that matches a given schema. Game logic never talks to a vendor API
directly, so adding another provider (Claude, Gemini, a local model) means
writing one class with ``complete_structured`` and choosing it in
``provider_from_env`` — nothing else changes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol

import httpx

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
REQUEST_TIMEOUT_SECONDS = 60.0


class ProviderError(RuntimeError):
    """The model request failed or its reply wasn't a JSON object."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _api_error(response: httpx.Response) -> ProviderError:
    # Never print the server's message: it may contain keys or prompt content.
    hints = {
        'insufficient_quota': 'OpenAI quota exhausted; check API billing, credits and project budget',
        'rate_limit_exceeded': 'OpenAI rate limit reached; check request/token limits and concurrent seats',
        'invalid_api_key': 'OpenAI API key was rejected; check OPENAI_API_KEY',
        'model_not_found': 'Configured model unavailable; check model access and OPENAI_MODEL',
        'invalid_json_schema': 'OpenAI rejected the structured-output schema',
        'unsupported_parameter': 'OpenAI rejected an unsupported request parameter',
        'unsupported_value': 'OpenAI rejected an unsupported parameter value',
    }
    try:
        error = response.json().get('error', {})
        code = error.get('code') if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        code = None
    hint = hints.get(code, 'Check API access and configuration') if isinstance(code, str) else 'Check API access and configuration'
    if code == 'rate_limit_exceeded' and isinstance(error.get('message'), str):
        # Extract only allowlisted metric names and numbers, never raw messages,
        # organization identifiers, keys, or prompt content.
        message = error['message']
        dimension = re.search(r'\b(tokens per min|requests per min|tokens per day|requests per day)\b', message, re.I)
        details = [dimension[1].lower()] if dimension else []
        for label in ('Limit', 'Used', 'Requested'):
            match = re.search(r'\b' + label + r'[: ]+(\d+)\b', message, re.I)
            if match:
                details.append(f'{label.lower()}={match[1]}')
        retry = response.headers.get('retry-after', '')
        if re.fullmatch(r'\d+(?:\.\d+)?', retry):
            details.append(f'retry_after_seconds={retry}')
        if details:
            hint += ' (' + ', '.join(details) + ')'
    return ProviderError(f'OpenAI request failed (HTTP {response.status_code}): {hint}',
                         retryable=response.status_code >= 500)


class LLMProvider(Protocol):
    model: str

    def complete_structured(
        self, messages: list[dict], schema_name: str, schema: dict, *, timeout: float | None = None
    ) -> dict:
        """Return the model's answer as a dict matching ``schema``, or raise
        ``ProviderError``. ``timeout`` (seconds), when given, bounds this one
        request: a game with a clock (Pokémon) passes what's left of the
        decision's time. A provider without the parameter still works; the
        agent then only limits how many requests it makes.
        """
        ...


class OpenAIProvider:
    """OpenAI Chat Completions with strict structured output, over the
    starter's existing ``httpx`` dependency (no SDK).

    The API key only ever goes into the Authorization header — never into
    ``repr``, logs, or error messages (provider error bodies can echo parts
    of the key, so only the HTTP status is reported).
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_MODEL,
        *,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"OpenAIProvider(model={self.model!r})"

    def complete_structured(
        self, messages: list[dict], schema_name: str, schema: dict, *, timeout: float | None = None
    ) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        planning = 'vote_order' in schema.get('properties', {})
        body['max_completion_tokens'] = 8192 if planning else 2048
        if self.model.startswith('gpt-6'):
            # Tactical thinking is bounded by the live game clock. Short chat
            # rendering needs no second reasoning pass over the same plan.
            body['reasoning_effort'] = 'low' if planning else 'none'
        # Only a game with a clock passes a timeout; otherwise the client's own applies.
        extra = {"timeout": timeout} if timeout is not None else {}
        try:
            response = self._http.post("/chat/completions", json=body, **extra)
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise _api_error(response)
        try:
            choice = response.json()["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("OpenAI response had no message") from None
        if choice.get('finish_reason') == 'length':
            raise ProviderError('OpenAI output was truncated before completing JSON')
        if message.get("refusal"):
            raise ProviderError("the model refused to answer")
        try:
            data = json.loads(message.get("content") or "")
        except ValueError:
            raise ProviderError("the model's reply was not valid JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("the model's reply was not a JSON object")
        return data


def provider_from_env() -> LLMProvider:
    """``OPENAI_API_KEY`` (required), ``OPENAI_MODEL``, ``OPENAI_BASE_URL``."""
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set — add it to your environment or the starter's .env.")
    return OpenAIProvider(
        api_key,
        os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        base_url=os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL,
    )
