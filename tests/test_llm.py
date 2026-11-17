"""Exercise ClaudeLLM through the real Anthropic SDK against a mocked HTTP stream."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest
from anthropic import Anthropic

from vigilant_engine.intake.models import ClarifyingQuestions, output_schema
from vigilant_engine.llm import FALLBACK_BETA, ClaudeLLM, LLMError, RefusalError


def sse(text: str, stop_reason: str = "end_turn") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def make_llm(body: bytes, captured: list[httpx.Request]) -> ClaudeLLM:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    client = Anthropic(api_key="test-key", base_url="http://api.test",
                       http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return ClaudeLLM(client=client)


def call(llm: ClaudeLLM):
    return llm.structured(system="sys", prompt="hello",
                          schema=output_schema(ClarifyingQuestions), effort="high",
                          max_tokens=64000)


def test_request_shape_and_result():
    captured: list[httpx.Request] = []
    result = call(make_llm(sse('{"questions": []}'), captured))

    assert result.text == '{"questions": []}' and result.stop_reason == "end_turn"
    assert result.usage["input_tokens"] == 12 and not result.fell_back
    body = json.loads(captured[0].content)
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in captured[0].headers["anthropic-beta"]
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_refusal_raises():
    with pytest.raises(RefusalError):
        call(make_llm(sse("", stop_reason="refusal"), []))


def test_truncation_raises():
    with pytest.raises(LLMError, match="max_tokens"):
        call(make_llm(sse('{"questions": [', stop_reason="max_tokens"), []))


def test_bedrock_model_id_is_prefixed():
    llm = ClaudeLLM(provider="bedrock", client=object())
    assert llm.model == "anthropic.claude-opus-5-5"
