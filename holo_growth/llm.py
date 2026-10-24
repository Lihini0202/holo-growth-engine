"""LLM client abstraction.

Structured output uses forced tool use with the Pydantic model's JSON schema as the
tool input schema, and the result is validated again with Pydantic. On validation
failure the request is retried once with the error included, then LLMError is
raised. Invalid output is never returned.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def structured(self, system: str, prompt: str, schema: type[T]) -> T: ...

    def text(self, system: str, prompt: str, max_tokens: int = 800) -> str: ...


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60)
        self._client: Any = client
        self.model = model

    def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        tool = {
            "name": "record_result",
            "description": f"Record the {schema.__name__} result.",
            "input_schema": schema.model_json_schema(),
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_error: str | None = None
        for attempt in range(2):
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": "record_result"},
                messages=messages,
            )
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            if block is None:
                last_error = "model did not call the tool"
                continue
            try:
                return schema.model_validate(block.input)
            except ValidationError as exc:
                last_error = str(exc)
                log.warning("structured output failed validation", extra={"attempt": attempt})
                messages = messages + [
                    {"role": "assistant", "content": resp.content},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "is_error": True,
                                "content": f"Validation failed, fix and call again:\n{exc}",
                            }
                        ],
                    },
                ]
        raise LLMError(f"no valid {schema.__name__} after retry: {last_error}")

    def text(self, system: str, prompt: str, max_tokens: int = 800) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content).strip()


class ScriptedLLM:
    """Deterministic LLM stand-in for tests and offline runs.

    `structured_fn` and `text_fn` map a prompt to a response payload, so tests can
    script exact responses, including invalid ones.
    """

    name = "scripted"

    def __init__(self, structured_fn=None, text_fn=None) -> None:
        self._structured_fn = structured_fn
        self._text_fn = text_fn
        self.calls: list[str] = []

    def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        self.calls.append(prompt)
        if self._structured_fn is None:
            raise LLMError("no structured_fn configured")
        payload = self._structured_fn(prompt)
        try:
            return schema.model_validate(payload if isinstance(payload, dict) else json.loads(payload))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise LLMError(str(exc)) from exc

    def text(self, system: str, prompt: str, max_tokens: int = 800) -> str:
        self.calls.append(prompt)
        if self._text_fn is None:
            raise LLMError("no text_fn configured")
        return str(self._text_fn(prompt))


def from_settings(settings: Any) -> LLM | None:
    """Return a client when an API key is configured, otherwise None (callers fall back to rules)."""
    if settings.use_real_llm:
        return AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model)
    return None
