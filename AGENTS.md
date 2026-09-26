# AGENTS.md — bedrock-chat

Guidance for Codex (and other coding agents) working in this project.

## What this project is

A minimal terminal chat app that talks to GPT models on **Amazon Bedrock**
using Bedrock's OpenAI-compatible **responses** endpoint
(`bedrock-mantle.<region>.api.aws/openai/v1/responses`) with SigV4 auth. This
is the same surface Codex uses — the GPT-5.x models are not served by the
Converse API. It is the sample project for
Part 2 of the OpenAI on AWS workshop.

## Layout

- `bedrock_chat/config.py` — environment-driven `Settings` (model, region, inference params). No AWS calls.
- `bedrock_chat/client.py` — `BedrockChatClient` (conversation history + SigV4-signed responses-API call) and pure helpers `build_message` / `extract_text`. The HTTP transport is injectable for offline tests.
- `bedrock_chat/__main__.py` — interactive REPL: `python -m bedrock_chat`.
- `tests/` — pytest suite. Fully offline: the client accepts an injected fake, helpers are pure.

## Run it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Talk to Bedrock (needs AWS credentials + model access):
python -m bedrock_chat
```

Config comes from the environment: `BEDROCK_MODEL_ID`, `BEDROCK_REGION`
(falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`), `BEDROCK_MAX_TOKENS`,
`BEDROCK_TEMPERATURE`.

## Test

```bash
pytest
```

Tests must never require live AWS access — keep new tests offline by injecting a
fake client or exercising pure helpers.

## Conventions for changes

- Keep AWS-touching code isolated in `client.py`; keep helpers pure and tested.
- `boto3` is imported lazily so the test suite runs without AWS setup — preserve that.
- Add a test with every behavior change; run `pytest` before finishing.
- Match the existing style: type hints, module docstrings, small focused functions.
