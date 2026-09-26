"""bedrock-chat: a minimal chat app for GPT models on Amazon Bedrock."""

from .client import BedrockChatClient, extract_text
from .config import Settings

__all__ = ["BedrockChatClient", "extract_text", "Settings"]
__version__ = "0.1.0"
