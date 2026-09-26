"""Interactive terminal entry point: `python -m bedrock_chat`.

Reads settings from the environment, then loops reading user input and printing
the model's reply. Type 'exit' or 'quit' (or press Ctrl-D) to leave.
"""

from __future__ import annotations

import sys

from .client import BedrockChatClient
from .config import Settings


def main() -> int:
    settings = Settings.from_env()
    print(f"bedrock-chat — model={settings.model_id} region={settings.region}")
    print("Type 'exit' or 'quit' to leave.\n")

    try:
        client = BedrockChatClient(settings)
    except Exception as exc:  # noqa: BLE001 - surface setup errors plainly
        print(f"Failed to create Bedrock client: {exc}", file=sys.stderr)
        print(
            "Check your AWS credentials and that the model is available in "
            f"{settings.region}.",
            file=sys.stderr,
        )
        return 1

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        try:
            reply = client.send(user_input)
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive on API errors
            print(f"[error] {exc}", file=sys.stderr)
            continue
        print(f"gpt> {reply}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
