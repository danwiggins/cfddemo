# bedrock-chat

A minimal terminal chat app that talks to GPT models on **Amazon Bedrock**
(via Bedrock's OpenAI-compatible responses API). This is the sample project used throughout
**Part 2** of the OpenAI on AWS workshop.

## Prerequisites

- Python 3.9+
- An AWS account with Amazon Bedrock access and the `openai.gpt-5.4` or
  `openai.gpt-5.5` model enabled in your target Region
- AWS credentials available to the AWS SDK (env vars, `aws configure`, SSO, or a named profile)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python -m bedrock_chat
```

Then type messages at the `you>` prompt. Type `exit` or `quit` to leave.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `BEDROCK_MODEL_ID` | `openai.gpt-5.5` | Bedrock model id |
| `BEDROCK_REGION` | `us-east-2` | Region (falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`) |
| `BEDROCK_MAX_TOKENS` | `1024` | Max response tokens |
| `BEDROCK_TEMPERATURE` | _(unset)_ | Sampling temperature. Omitted by default — GPT-5.x reasoning models reject it; set only for models that support it. |

## Test

```bash
pytest
```

The test suite runs fully offline — no AWS credentials required.

## Codex + Bedrock

To drive this project with the Codex CLI/App pointed at Bedrock, see
[`codex-config.example.toml`](./codex-config.example.toml) for a starting
`~/.codex/config.toml`, and [`AGENTS.md`](./AGENTS.md) for agent guidance.
