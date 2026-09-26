"""Offline tests for the stateless, deadline-aware Responses adapter."""

import json

import pytest

from bedrock_chat.config import Settings
from bedrock_chat.responses import (
    ResponsesError,
    ResponsesProtocolError,
    ResponsesTimeout,
    StatelessResponsesClient,
)


def _response(text: str) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_stateless_client_builds_regional_schema_request_without_history() -> None:
    calls = []

    def transport(url, headers, body, timeout):
        calls.append((url, headers, json.loads(body), timeout))
        return _response('{"ok":true}')

    client = StatelessResponsesClient(
        Settings(region="us-west-2", max_tokens=500),
        transport=transport,
    )
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    assert (
        client.create(
            [{"role": "user", "content": "one"}],
            timeout_seconds=3.5,
            json_schema=schema,
            schema_name="answer",
        )
        == '{"ok":true}'
    )
    client.create("two", timeout_seconds=2)

    assert calls[0][0] == (
        "https://bedrock-mantle.us-west-2.api.aws/openai/v1/responses"
    )
    assert calls[0][2]["input"] == [{"role": "user", "content": "one"}]
    assert calls[0][2]["text"]["format"]["strict"] is True
    assert calls[0][2]["max_output_tokens"] == 500
    assert calls[0][3] == 3.5
    assert calls[1][2]["input"] == "two"


def test_stateless_client_omits_temperature_unless_configured() -> None:
    bodies = []

    def transport(_url, _headers, body, _timeout):
        bodies.append(json.loads(body))
        return _response("ok")

    StatelessResponsesClient(Settings(), transport=transport).create(
        "x", timeout_seconds=1
    )
    StatelessResponsesClient(
        Settings(temperature=0.2), transport=transport
    ).create("x", timeout_seconds=1)

    assert "temperature" not in bodies[0]
    assert bodies[1]["temperature"] == 0.2


def test_stateless_client_rejects_invalid_limits_before_transport() -> None:
    client = StatelessResponsesClient(Settings(), transport=lambda *_: _response("x"))

    with pytest.raises(ResponsesTimeout, match="No request time"):
        client.create("x", timeout_seconds=0)
    with pytest.raises(ValueError, match="between 1 and 2000"):
        client.create("x", timeout_seconds=1, max_output_tokens=2_001)


def test_stateless_client_has_no_automatic_retry() -> None:
    calls = 0

    def transport(*_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider down")

    client = StatelessResponsesClient(Settings(), transport=transport)
    with pytest.raises(ResponsesError, match="request failed"):
        client.create("x", timeout_seconds=1)
    assert calls == 1


def test_stateless_client_rejects_empty_and_oversized_output() -> None:
    empty = StatelessResponsesClient(Settings(), transport=lambda *_: {})
    with pytest.raises(ResponsesProtocolError, match="no output text"):
        empty.create("x", timeout_seconds=1)

    oversized = StatelessResponsesClient(
        Settings(), transport=lambda *_: _response("x" * (32 * 1024 + 1))
    )
    with pytest.raises(ResponsesProtocolError, match="exceeded 32 KiB"):
        oversized.create("x", timeout_seconds=1)

    malformed = StatelessResponsesClient(
        Settings(), transport=lambda *_: {"output": ["not-an-item"]}
    )
    with pytest.raises(ResponsesProtocolError, match="invalid Responses payload"):
        malformed.create("x", timeout_seconds=1)


def test_stateless_client_rejects_transport_timeout_and_late_output() -> None:
    timeout_client = StatelessResponsesClient(
        Settings(),
        transport=lambda *_: (_ for _ in ()).throw(TimeoutError()),
    )
    with pytest.raises(ResponsesTimeout, match="timed out"):
        timeout_client.create("x", timeout_seconds=1)

    clock = _Clock()

    def late_transport(*_args):
        clock.now = 1.01
        return _response("late")

    late_client = StatelessResponsesClient(
        Settings(), transport=late_transport, clock=clock
    )
    with pytest.raises(ResponsesTimeout, match="after its deadline"):
        late_client.create("x", timeout_seconds=1)
