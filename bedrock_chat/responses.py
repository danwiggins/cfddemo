"""Stateless Bedrock Responses API client with enforceable request deadlines."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .client import extract_text
from .config import Settings

MAX_OUTPUT_BYTES = 32 * 1024
MAX_OUTPUT_TOKENS = 2_000

ResponseTransport = Callable[
    [str, dict[str, str], bytes, float], dict[str, Any]
]
Clock = Callable[[], float]


class ResponsesError(RuntimeError):
    """Base error for a failed stateless response."""


class ResponsesTimeout(ResponsesError):
    """The provider request timed out or returned after its deadline."""


class ResponsesProtocolError(ResponsesError):
    """The provider returned an empty, oversized, or malformed response."""


def _sigv4_response_transport(region: str) -> ResponseTransport:
    """Build a no-retry SigV4 transport using the standard AWS credential chain."""

    import boto3  # imported lazily to keep offline tests independent of AWS
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.Session(region_name=region)

    def transport(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        credentials = session.get_credentials()
        if credentials is None:
            raise ResponsesError(
                "No AWS credentials found in the standard credential chain."
            )
        aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
        SigV4Auth(
            credentials.get_frozen_credentials(), "bedrock", region
        ).add_auth(aws_request)
        request = urllib.request.Request(url, data=body, method="POST")
        for key, value in aws_request.headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(MAX_OUTPUT_BYTES + 1)
        except (TimeoutError, socket.timeout) as exc:
            raise ResponsesTimeout("Bedrock request timed out.") from exc
        except urllib.error.HTTPError as exc:
            raise ResponsesError(f"Bedrock returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ResponsesTimeout("Bedrock request timed out.") from exc
            raise ResponsesError("Bedrock request failed.") from exc
        if len(raw) > MAX_OUTPUT_BYTES:
            raise ResponsesProtocolError("Bedrock response exceeded 32 KiB.")
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResponsesProtocolError("Bedrock returned malformed JSON.") from exc
        if not isinstance(parsed, dict):
            raise ResponsesProtocolError("Bedrock response must be a JSON object.")
        return parsed

    return transport


class StatelessResponsesClient:
    """Issue independent OpenAI-compatible Responses calls without retries or history."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: ResponseTransport | None = None,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.url = (
            f"https://bedrock-mantle.{self.settings.region}.api.aws/openai/v1/responses"
        )
        self._transport = transport or _sigv4_response_transport(self.settings.region)
        self._clock = clock

    def create(
        self,
        input_messages: str | Sequence[Mapping[str, Any]],
        *,
        timeout_seconds: float,
        max_output_tokens: int | None = None,
        json_schema: Mapping[str, Any] | None = None,
        schema_name: str = "response",
    ) -> str:
        """Return response text, rejecting empty, oversized, or late output."""

        if timeout_seconds <= 0:
            raise ResponsesTimeout("No request time remains.")
        token_limit = (
            self.settings.max_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        if not 1 <= token_limit <= MAX_OUTPUT_TOKENS:
            raise ValueError(f"max_output_tokens must be between 1 and {MAX_OUTPUT_TOKENS}")

        payload: dict[str, Any] = {
            "model": self.settings.model_id,
            "input": input_messages,
            "max_output_tokens": token_limit,
        }
        if self.settings.temperature is not None:
            payload["temperature"] = self.settings.temperature
        if json_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(json_schema),
                }
            }

        started = self._clock()
        try:
            response = self._transport(
                self.url,
                {"Content-Type": "application/json"},
                json.dumps(payload, allow_nan=False, separators=(",", ":")).encode(),
                timeout_seconds,
            )
        except ResponsesError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ResponsesTimeout("Bedrock request timed out.") from exc
        except Exception as exc:
            raise ResponsesError("Bedrock request failed.") from exc
        if self._clock() - started >= timeout_seconds:
            raise ResponsesTimeout("Bedrock response arrived after its deadline.")

        try:
            encoded = json.dumps(response, allow_nan=False, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise ResponsesProtocolError("Bedrock returned a non-JSON response.") from exc
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ResponsesProtocolError("Bedrock response exceeded 32 KiB.")
        try:
            text = extract_text(response)
        except (AttributeError, TypeError) as exc:
            raise ResponsesProtocolError(
                "Bedrock returned an invalid Responses payload."
            ) from exc
        if not text:
            raise ResponsesProtocolError("Bedrock returned no output text.")
        if len(text.encode()) > MAX_OUTPUT_BYTES:
            raise ResponsesProtocolError("Bedrock output text exceeded 32 KiB.")
        return text


__all__ = [
    "MAX_OUTPUT_BYTES",
    "MAX_OUTPUT_TOKENS",
    "ResponseTransport",
    "ResponsesError",
    "ResponsesProtocolError",
    "ResponsesTimeout",
    "StatelessResponsesClient",
]
