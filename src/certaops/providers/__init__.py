"""Saglayici-bagimsiz model katmani.

    contracts   notr sozlesmeler (ModelProvider, ModelRequest/Response, ...)
    gemini      Google Gemini adaptoru (Developer API + Vertex)
    anthropic   Claude adaptoru (opsiyonel bagimlilik)
    fake        testler icin senaryolanabilir saglayici

Core runtime yalnizca ``contracts`` icindeki tipleri bilir.
"""

from __future__ import annotations

import logging
from typing import Any

from .contracts import (
    FunctionCall,
    FunctionDeclaration,
    FunctionResult,
    ModelAuthError,
    ModelBadRequestError,
    ModelMessage,
    ModelProvider,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequest,
    ModelResponse,
    ModelTimeoutError,
    ModelUnavailableError,
    StopReason,
    ThinkingLevel,
    TokenUsage,
    redact_provider_state,
)
from .fake import FakeModelProvider

log = logging.getLogger(__name__)

__all__ = [
    "FakeModelProvider",
    "FunctionCall",
    "FunctionDeclaration",
    "FunctionResult",
    "ModelAuthError",
    "ModelBadRequestError",
    "ModelMessage",
    "ModelProvider",
    "ModelProviderError",
    "ModelRateLimitError",
    "ModelRequest",
    "ModelResponse",
    "ModelTimeoutError",
    "ModelUnavailableError",
    "StopReason",
    "ThinkingLevel",
    "TokenUsage",
    "build_provider",
    "redact_provider_state",
]

#: Desteklenen saglayici anahtarlari. `fake` yalniz test icindir ve uretim
#: profilinde reddedilir (bkz. Settings.production_blockers).
SUPPORTED_PROVIDERS = ("gemini", "anthropic", "fake")


def build_provider(settings: Any, *, client: Any = None) -> ModelProvider:
    """Ayarlara gore saglayici uretir.

    Saglayici secimi **konfigurasyondan** gelir; kod icinde saglayici adi
    kontrol eden bir dallanma birakilmaz. Yeni bir saglayici eklemek bu
    fonksiyona bir dal eklemekten ibarettir.
    """
    cfg = settings.model
    provider = cfg.provider
    if provider == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(
            model=cfg.name,
            api_key=cfg.gemini_api_key,
            backend=cfg.gemini_backend,
            project=cfg.google_cloud_project,
            location=cfg.google_cloud_location,
            timeout_s=cfg.timeout_s,
            max_retries=cfg.max_retries,
            store_interactions=cfg.store_interactions,
            client=client,
        )
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            model=cfg.name,
            api_key=cfg.anthropic_api_key,
            max_retries=cfg.max_retries,
            client=client,
        )
    if provider == "fake":
        log.warning("MODEL_PROVIDER=fake: yalnizca test icindir, model cagrilmaz.")
        return FakeModelProvider(model=cfg.name)
    raise ModelProviderError(
        f"MODEL_PROVIDER '{provider}' desteklenmiyor "
        f"({', '.join(SUPPORTED_PROVIDERS)}).",
        provider=provider,
    )
