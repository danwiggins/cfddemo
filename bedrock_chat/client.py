"""Client for GPT models on Amazon Bedrock via the OpenAI-compatible API.

The GPT-5.x models (openai.gpt-5.5, openai.gpt-5.4) are served on Bedrock's
OpenAI-compatible **responses** endpoint
(https://bedrock-mantle.<region>.api.aws/openai/v1/responses), the same surface
Codex uses — NOT the Bedrock Converse API. Requests are authenticated with
SigV4 using the standard AWS credential chain (service name "bedrock").

Only botocore (bundled with boto3) and the stdlib are used: botocore signs the
request, urllib sends it. The HTTP transport is injectable so the conversation
logic can be unit tested without AWS access or network calls.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable

from .config import Settings

# Transport takes (url, headers, body_bytes) and returns the parsed JSON dict.
Transport = Callable[[str, "dict[str, str]", bytes], "dict[str, Any]"]


def build_message(role: str, text: str) -> dict[str, Any]:
    """Build a single responses-API input message.

    The responses API takes an ``input`` list of messages, each with a role
    ("user" or "assistant") and string content.
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
    return {"role": role, "content": text}


def extract_text(response: dict[str, Any]) -> str:
    """Pull the assistant's reply text out of a responses-API payload.

    The reply lives in ``output`` as message items whose ``content`` blocks have
    ``type == "output_text"``. Reasoning items (no ``output_text``) are skipped.
    Multiple text blocks are joined so nothing is dropped.
    """
    parts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text" and "text" in block:
                parts.append(block["text"])
    return "".join(parts)


def _sigv4_transport(region: str) -> Transport:
    """Default transport: SigV4-sign with the AWS credential chain, POST via urllib."""
    import boto3  # imported lazily so tests need no AWS/boto3 setup
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials found. Configure the AWS credential chain "
            "(env vars, `aws configure`, SSO, or a named profile)."
        )

    def transport(url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        aws_req = AWSRequest(method="POST", url=url, data=body, headers=headers)
        # Sign against the "bedrock" service; SigV4 needs fresh (unexpired) creds.
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock", region).add_auth(
            aws_req
        )
        http_req = urllib.request.Request(url, data=body, method="POST")
        for key, value in aws_req.headers.items():
            http_req.add_header(key, value)
        with urllib.request.urlopen(http_req, timeout=60) as resp:
            return json.loads(resp.read())

    return transport


class BedrockChatClient:
    """Maintains conversation history and calls the Bedrock responses endpoint."""

    def __init__(self, settings: Settings | None = None, transport: Transport | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.url = (
            f"https://bedrock-mantle.{self.settings.region}.api.aws/openai/v1/responses"
        )
        # Allow injecting a fake transport in tests; only touch AWS/boto3 otherwise.
        self._transport = transport or _sigv4_transport(self.settings.region)
        self.history: list[dict[str, Any]] = []

    def send(self, text: str) -> str:
        """Send a user turn, record the exchange, and return the reply text."""
        self.history.append(build_message("user", text))
        payload: dict[str, Any] = {
            "model": self.settings.model_id,
            "input": self.history,
            "max_output_tokens": self.settings.max_tokens,
        }
        # GPT-5.x reasoning models reject `temperature`; only send it when set.
        if self.settings.temperature is not None:
            payload["temperature"] = self.settings.temperature
        body = json.dumps(payload).encode()
        response = self._transport(
            self.url, {"Content-Type": "application/json"}, body
        )
        reply = extract_text(response)
        self.history.append(build_message("assistant", reply))
        return reply
