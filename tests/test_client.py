"""Unit tests for the pure helpers and the client's conversation handling.

These tests never touch AWS: BedrockChatClient accepts an injected fake
transport, and the helpers are plain functions. Run with `pytest` from the
bedrock-chat folder.
"""

import json

import pytest

from bedrock_chat.client import BedrockChatClient, build_message, extract_text
from bedrock_chat.config import Settings


def test_build_message_shape():
    assert build_message("user", "hi") == {"role": "user", "content": "hi"}


def test_build_message_rejects_bad_role():
    with pytest.raises(ValueError):
        build_message("system", "nope")


def test_extract_text_joins_output_text_blocks():
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Hello "},
                    {"type": "output_text", "text": "world"},
                ],
            }
        ]
    }
    assert extract_text(response) == "Hello world"


def test_extract_text_skips_reasoning_items():
    # Reasoning items and non-output_text blocks must not leak into the reply.
    response = {
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "hmm"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "READY"}]},
        ]
    }
    assert extract_text(response) == "READY"


def test_extract_text_handles_empty_response():
    assert extract_text({}) == ""


class _FakeTransport:
    """Records requests and returns a canned responses-API payload."""

    def __init__(self, reply="pong"):
        self.reply = reply
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": self.reply}],
                }
            ]
        }


def test_send_records_history_and_returns_reply():
    fake = _FakeTransport(reply="pong")
    client = BedrockChatClient(settings=Settings(), transport=fake)

    reply = client.send("ping")

    assert reply == "pong"
    # One user turn + one assistant turn recorded, in responses-API shape.
    assert client.history == [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]
    # The request `input` carries the conversation up to (and including) this
    # user turn; the assistant reply is appended to history afterward.
    sent = fake.calls[0]["body"]
    assert sent["input"] == [{"role": "user", "content": "ping"}]
    assert sent["model"] == Settings().model_id


def test_send_targets_regional_responses_endpoint():
    fake = _FakeTransport()
    client = BedrockChatClient(settings=Settings(region="us-east-2"), transport=fake)

    client.send("hi")

    assert (
        fake.calls[0]["url"]
        == "https://bedrock-mantle.us-east-2.api.aws/openai/v1/responses"
    )


def test_send_passes_inference_params():
    fake = _FakeTransport()
    settings = Settings(max_tokens=256, temperature=0.1)
    client = BedrockChatClient(settings=settings, transport=fake)

    client.send("hi")

    sent = fake.calls[0]["body"]
    assert sent["max_output_tokens"] == 256
    assert sent["temperature"] == 0.1


def test_send_omits_temperature_by_default():
    # GPT-5.x reasoning models reject `temperature`; it must not be sent unless set.
    fake = _FakeTransport()
    client = BedrockChatClient(settings=Settings(), transport=fake)

    client.send("hi")

    assert "temperature" not in fake.calls[0]["body"]


def test_failed_send_does_not_append_assistant_history():
    def failing_transport(_url, _headers, _body):
        raise RuntimeError("offline")

    client = BedrockChatClient(settings=Settings(), transport=failing_transport)

    with pytest.raises(RuntimeError, match="offline"):
        client.send("ping")

    assert client.history == [{"role": "user", "content": "ping"}]
