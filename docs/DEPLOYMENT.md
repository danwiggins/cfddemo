# Railway deployment

Live service: <https://cfddemo-production.up.railway.app>

This page describes the public Streamlit evidence-review demo served by
`app.py`. Its replay and Bedrock modes are separate from the local `traceback`
runner and unsigned `traceback lab` workflow. The latter writes to a local
operator-controlled root; this Railway deployment does not execute Lab methods
or establish real-input, protocol, scientific, or clinical qualification.

The repository includes a `Procfile` for Streamlit:

```text
web: streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true
```

## Public demo

Set:

```text
TRACEBACK_DEMO_REPLAY=1
```

This uses the checked-in aggregate bundles and replays validated assessments.
No AWS credentials are required and no provider call is made.

## Live Bedrock review

For a private deployment, remove replay mode and configure the AWS credential
chain plus:

```text
BEDROCK_MODEL_ID=openai.gpt-5.5
BEDROCK_REGION=us-east-2
BEDROCK_MAX_TOKENS=1024
```

Do not place AWS credentials, BAMs, sequence files, source documents, read IDs,
or patient identifiers in the repository.
