"""Anthropic adapter. Satisfies the LLMProvider port.

Every SDK exception is translated at this boundary -- see the note in
``gemini_llm`` for why that matters.
"""

from __future__ import annotations

from ..domain.models import Usage
from ..ports import LLMError, RateLimited, Refused


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client=None):
        import anthropic

        self._sdk = anthropic
        # `client` is injectable so error translation can be tested without a key.
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def complete(
        self, model: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage]:
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except self._sdk.RateLimitError as error:
            raise RateLimited(f"anthropic {model}: rate limited ({error})") from error
        except self._sdk.APIError as error:
            raise LLMError(f"anthropic {model}: {error}") from error

        usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            raise Refused(f"anthropic {model} declined the request")

        text = "".join(block.text for block in response.content if block.type == "text")
        return text, usage
