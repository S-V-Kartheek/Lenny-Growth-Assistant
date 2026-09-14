"""LLM provider abstraction.

`app.llm.base` is the contract; everything else in this package is either an
implementation of it or the plumbing shared between implementations. Nothing
outside this package should import a concrete provider class.
"""

from app.llm.base import (
    ChatMessage,
    Completion,
    LLMProvider,
    ProviderHealth,
    StreamEvent,
    TokenUsage,
)
from app.llm.registry import LLMGateway, all_providers, build_provider

__all__ = [
    "ChatMessage",
    "Completion",
    "LLMGateway",
    "LLMProvider",
    "ProviderHealth",
    "StreamEvent",
    "TokenUsage",
    "all_providers",
    "build_provider",
]
