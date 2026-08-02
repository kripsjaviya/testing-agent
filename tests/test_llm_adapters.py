"""Adapter error translation.

Regression tests for a real failure: a Gemini 429 escaped as a raw SDK exception
and killed a whole run, because nothing above the adapter layer knows what a
`google.genai.errors.ClientError` is. Every vendor error must arrive as an
LLMError so the coordinator can record it and move on.
"""

import pytest

from bugfixer.adapters.anthropic_llm import AnthropicProvider
from bugfixer.adapters.gemini_llm import GeminiProvider
from bugfixer.domain.models import Usage
from bugfixer.ports import LLMError, RateLimited, Refused


class Boom:
    """A client whose only behaviour is to raise."""

    def __init__(self, error):
        self.error = error
        self.models = self
        self.messages = self

    def generate_content(self, **_kwargs):
        raise self.error

    def create(self, **_kwargs):
        raise self.error


# --- gemini ---------------------------------------------------------------


def gemini_error(status: int):
    from google.genai import errors

    return errors.ClientError(status, {"error": {"code": status, "message": "boom"}})


def test_gemini_quota_error_becomes_rate_limited():
    provider = GeminiProvider(client=Boom(gemini_error(429)))

    with pytest.raises(RateLimited):
        provider.complete("gemini-2.5-pro", "sys", "prompt", 100)


def test_gemini_other_api_errors_become_llm_error():
    provider = GeminiProvider(client=Boom(gemini_error(400)))

    with pytest.raises(LLMError) as caught:
        provider.complete("gemini-2.5-pro", "sys", "prompt", 100)

    assert not isinstance(caught.value, RateLimited)


def test_gemini_empty_response_is_a_refusal():
    class Empty:
        text = ""
        usage_metadata = type(
            "M",
            (),
            {"prompt_token_count": 5, "candidates_token_count": 0, "thoughts_token_count": 0},
        )()

    class Client:
        def __init__(self):
            self.models = self

        def generate_content(self, **_kwargs):
            return Empty()

    with pytest.raises(Refused):
        GeminiProvider(client=Client()).complete("gemini-2.5-pro", "s", "p", 100)


def test_gemini_counts_thinking_tokens_as_output():
    """Thinking tokens are billed as output but reported separately."""

    class Response:
        text = "answer"
        usage_metadata = type(
            "M",
            (),
            {
                "prompt_token_count": 100,
                "candidates_token_count": 20,
                "thoughts_token_count": 30,
            },
        )()

    class Client:
        def __init__(self):
            self.models = self

        def generate_content(self, **_kwargs):
            return Response()

    _text, usage = GeminiProvider(client=Client()).complete("m", "s", "p", 100)

    assert usage == Usage(100, 50)


# --- anthropic ------------------------------------------------------------


def anthropic_error(cls_name: str):
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429 if cls_name == "RateLimitError" else 500, request=request)
    cls = getattr(anthropic, cls_name)
    return cls("boom", response=response, body=None)


def test_anthropic_rate_limit_becomes_rate_limited():
    provider = AnthropicProvider(client=Boom(anthropic_error("RateLimitError")))

    with pytest.raises(RateLimited):
        provider.complete("claude-opus-5", "sys", "prompt", 100)


def test_anthropic_server_error_becomes_llm_error():
    provider = AnthropicProvider(client=Boom(anthropic_error("InternalServerError")))

    with pytest.raises(LLMError) as caught:
        provider.complete("claude-opus-5", "sys", "prompt", 100)

    assert not isinstance(caught.value, RateLimited)


def test_anthropic_refusal_stop_reason_is_a_refusal():
    class Response:
        stop_reason = "refusal"
        content = []
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 0})()

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **_kwargs):
            return Response()

    with pytest.raises(Refused):
        AnthropicProvider(client=Client()).complete("claude-opus-5", "s", "p", 100)
