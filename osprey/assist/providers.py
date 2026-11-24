"""Model providers for Osprey Assist: OpenAI-style chat and OpenRouter-style decisions.

The implementation is raptor-vision's (``third_party/raptor_vision/providers.py``); this module keeps the old import path.
"""

from __future__ import annotations

from third_party.raptor_vision.providers import (
    Budget,
    ChatProvider,
    DecisionsProvider,
    ModelReply,
    ProviderConfig,
    ProviderError,
    build_provider,
    parse_provider,
)

__all__ = ["Budget", "ChatProvider", "DecisionsProvider", "ModelReply", "ProviderConfig", "ProviderError", "build_provider", "parse_provider"]
