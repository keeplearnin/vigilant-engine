"""Thin, provider-aware wrapper for structured Claude calls.

* Streams every request (`get_final_message()`), so long backlogs do not hit HTTP timeouts.
* Uses structured outputs (`output_config.format`) so responses match the schema.
* Sets effort explicitly: Claude Opus 5.5 always thinks adaptively and defaults to `medium`.
* Anthropic API: opts into server-side refusal fallback (`fallbacks="default"`).
  Amazon Bedrock does not offer server-side fallback; a refusal there is surfaced as an error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Provider = Literal["anthropic", "bedrock"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL = "claude-opus-5-5"
BEDROCK_MODEL_PREFIX = "anthropic."
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Current Claude models allow 128K output tokens; adaptive thinking counts toward the cap,
# and a large backlog at high effort can exceed 64K. Streaming keeps this within timeouts.
DEFAULT_MAX_TOKENS = 128000


class LLMError(RuntimeError):
    """The model call finished without a usable answer."""


class RefusalError(LLMError):
    def __init__(self, category: str | None, explanation: str | None):
        self.category = category
        super().__init__(
            f"Claude declined the request (category: {category or 'unspecified'}). "
            f"{explanation or ''}".strip()
        )


@dataclass
class LLMResult:
    text: str
    model: str
    stop_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    fell_back: bool = False
    request_id: str | None = None


class StructuredLLM(Protocol):
    model: str

    def structured(
        self, *, system: str, prompt: str, schema: dict[str, Any], effort: Effort,
        max_tokens: int, preamble: str | None = None,
    ) -> LLMResult: ...


class ClaudeLLM:
    """Structured calls to Claude through the Anthropic API or Amazon Bedrock."""

    def __init__(
        self,
        provider: Provider = "anthropic",
        model: str | None = None,
        client: Any | None = None,
        aws_region: str | None = None,
    ):
        self.provider = provider
        if provider == "bedrock":
            base = model or DEFAULT_MODEL
            prefixed = base.startswith(BEDROCK_MODEL_PREFIX)
            self.model = base if prefixed else BEDROCK_MODEL_PREFIX + base
            if client is None:
                from anthropic import AnthropicBedrockMantle

                region = aws_region or os.environ.get("AWS_REGION")
                if not region:
                    raise LLMError("Set AWS_REGION (or --aws-region) to use Amazon Bedrock.")
                client = AnthropicBedrockMantle(aws_region=region)
        else:
            self.model = model or DEFAULT_MODEL
            if client is None:
                from anthropic import Anthropic

                client = Anthropic()
        self.client = client

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        effort: Effort = "high",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        preamble: str | None = None,
    ) -> LLMResult:
        """`preamble` is material shared by several calls (the idea and its context). It is
        sent as a separate, cached user block ahead of `prompt`."""
        content: list[dict[str, Any]] = []
        if preamble:
            content.append({"type": "text", "text": preamble,
                            "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": prompt})
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        if self.provider == "anthropic":
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
            stream_fn = self.client.beta.messages.stream
        else:
            stream_fn = self.client.messages.stream

        with stream_fn(**kwargs) as stream:
            message = stream.get_final_message()

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise RefusalError(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )
        if message.stop_reason == "max_tokens":
            raise LLMError(
                "The response hit max_tokens before the backlog was complete. "
                "Narrow the idea or lower the backlog limits."
            )

        text, fell_back = final_text(message.content)
        if not text:
            raise LLMError(f"No text in the response (stop_reason={message.stop_reason}).")

        usage = getattr(message, "usage", None)
        return LLMResult(
            text=text,
            model=getattr(message, "model", self.model),
            stop_reason=message.stop_reason,
            usage={
                k: getattr(usage, k, None)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            }
            if usage is not None
            else {},
            fell_back=fell_back,
            request_id=getattr(message, "_request_id", None),
        )


def final_text(content: list[Any]) -> tuple[str, bool]:
    """Text of the answer, ignoring anything a declining model wrote before a fallback."""
    last_fallback = -1
    for i, block in enumerate(content):
        if getattr(block, "type", None) == "fallback":
            last_fallback = i
    texts = [
        block.text
        for block in content[last_fallback + 1 :]
        if getattr(block, "type", None) == "text"
    ]
    return "".join(texts).strip(), last_fallback >= 0
