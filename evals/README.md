# Evaluations

`cases.json` defines six synthetic semantic cases. Run all six without AWS:

```bash
uv run python -m evals.harness
```

Run the same cases against the configured Bedrock model:

```bash
uv run python -m evals.harness --live
```

Five cases require one exact tool route. The `raw-aligned-noncomparability`
rubric requires `insufficient_evidence` but accepts either bounded
`source_review` or `insufficient_inputs`, as allowed by the scientific
specification.

Live mode writes metadata-only outcomes to `evals/results/`. That directory is
ignored because even sanitized run records are local evaluation artifacts.
Never commit prompts, private excerpts, credentials, provider payloads, or local
provenance. Only synthetic, de-identified definitions belong here.
