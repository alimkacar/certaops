"""Model provider construction."""

from __future__ import annotations

from typing import Any

from .anthropic import AnthropicModelProvider
from .base import ModelConfigurationError, ModelProvider, resolve_agent_settings
from .gemini import GeminiModelProvider


def build_model_provider(settings: Any, client: Any | None = None) -> ModelProvider:
    """Build the configured provider, optionally using an injected SDK client."""
    cfg = resolve_agent_settings(settings)
    provider = str(getattr(cfg, "provider", "gemini") or "gemini").strip().lower()
    if provider in {"gemini", "google"}:
        return GeminiModelProvider(settings, client=client)
    if provider in {"anthropic", "claude"}:
        return AnthropicModelProvider(settings, client=client)
    raise ModelConfigurationError(
        "MODEL_PROVIDER must be gemini or anthropic",
        provider=provider,
    )
