"""Google Gemini adapter. Satisfies the LLMProvider port.

Every SDK exception is translated at this boundary. An adapter that lets a
vendor error escape turns a recoverable failure -- a rate limit, a bad model ID --
into a crashed run, because nothing above this layer knows what a
``google.genai.errors.ClientError`` is.
"""

from __future__ import annotations

from ..domain.models import Usage
from ..ports import LLMError, RateLimited, Refused


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, client=None):
        from google import genai

        self._errors = genai.errors
        self._types = genai.types
        # `client` is injectable so error translation can be tested without a key.
        self._client = client or genai.Client(api_key=api_key)

    def complete(
        self, model: str, system: str, prompt: str, max_tokens: int
    ) -> tuple[str, Usage]:
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=self._types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                ),
            )
        except self._errors.APIError as error:
            code = getattr(error, "code", None)
            if code == 429:
                raise RateLimited(f"gemini {model}: quota exhausted ({error})") from error
            raise LLMError(f"gemini {model}: {error}") from error

        meta = response.usage_metadata
        # Thinking tokens are billed as output but are reported separately from
        # candidates_token_count -- counting only the latter under-reports spend.
        output = (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        usage = Usage(meta.prompt_token_count or 0, output)

        text = response.text
        if not text:
            raise Refused(f"gemini {model} returned no text (blocked or empty)")
        return text, usage
