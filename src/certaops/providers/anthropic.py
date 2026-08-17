"""Anthropic saglayici adaptoru (opsiyonel bagimlilik).

Gemini'ye gecis sonrasi Anthropic destegi **opsiyonel** hale getirildi.
Amac: saglayici sozlesmesinin gercekten saglayici-bagimsiz oldugunu iki farkli
SDK ile kanitlamak. Core runtime bu dosyadaki hicbir tipi gormez.

`cache_control` gibi Anthropic'e ozgu prompt-cache mantigi buraya tasindi;
runtime'da izi kalmadi.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .contracts import (
    FunctionCall,
    ModelAuthError,
    ModelBadRequestError,
    ModelMessage,
    ModelProviderError,
    ModelRateLimitError,
    ModelRequest,
    ModelResponse,
    ModelTimeoutError,
    ModelUnavailableError,
    StopReason,
    TokenUsage,
)

log = logging.getLogger(__name__)

__all__ = ["AnthropicProvider"]

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    "refusal": "safety",
}


def _classify(exc: Exception, provider: str = "anthropic") -> ModelProviderError:
    status = getattr(exc, "status_code", None)
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, TimeoutError) or "timeout" in lowered:
        return ModelTimeoutError(text, provider=provider, status=status)
    if status in (401, 403):
        return ModelAuthError(text, provider=provider, status=status)
    if status == 429 or "rate limit" in lowered:
        return ModelRateLimitError(text, provider=provider, status=status)
    if isinstance(status, int) and 500 <= status < 600:
        return ModelUnavailableError(text, provider=provider, status=status)
    if isinstance(status, int) and 400 <= status < 500:
        return ModelBadRequestError(text, provider=provider, status=status)
    return ModelProviderError(text, provider=provider, status=status)


class AnthropicProvider:
    """Claude Messages API uzerinden erisim."""

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5",
        api_key: str = "",
        max_retries: int = 2,
        use_prompt_cache: bool = True,
        client: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.model = model
        self.max_retries = max(1, max_retries)
        self.use_prompt_cache = use_prompt_cache
        self._sleep = sleep
        if client is not None:
            self._client = client
        else:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover
                raise ModelProviderError(
                    "anthropic paketi kurulu degil. `pip install .[anthropic]` "
                    "calistirin veya MODEL_PROVIDER=gemini kullanin.",
                    provider=self.name,
                ) from exc
            if not api_key:
                raise ModelAuthError("ANTHROPIC_API_KEY tanimli degil.", provider=self.name)
            self._client = Anthropic(api_key=api_key)

    # --- Donusum ------------------------------------------------------------
    def _tools(self, request: ModelRequest) -> list[dict[str, Any]]:
        tools = [
            {"name": f.name, "description": f.description, "input_schema": dict(f.parameters)}
            for f in request.functions
        ]
        if tools and self.use_prompt_cache:
            # Prompt cache isareti Anthropic'e OZGUDUR ve yalniz burada durur.
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tools

    def _messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "user":
                out.append({"role": "user", "content": message.text})
            elif message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.text:
                    blocks.append({"type": "text", "text": message.text})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": dict(call.arguments),
                    }
                    for call in message.function_calls
                )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            else:  # tool
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": result.call_id,
                                "content": result.content,
                                "is_error": result.is_error,
                            }
                            for result in message.function_results
                        ],
                    }
                )
        return out

    def generate(self, request: ModelRequest) -> ModelResponse:
        system: Any = request.system
        if self.use_prompt_cache and request.system:
            system = [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": system,
            "messages": self._messages(request),
        }
        tools = self._tools(request)
        if tools:
            kwargs["tools"] = tools

        last: ModelProviderError | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self._client.messages.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                error = _classify(exc)
                last = error
                if not error.retryable or attempt >= self.max_retries:
                    raise error from exc
                self._sleep(min(8.0, 2.0**attempt))
                continue
            return self._parse(raw)
        raise last or ModelProviderError("Anthropic cagrisi tamamlanamadi.", provider=self.name)

    def _parse(self, raw: Any) -> ModelResponse:
        texts: list[str] = []
        calls: list[FunctionCall] = []
        for block in getattr(raw, "content", None) or []:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(str(getattr(block, "text", "")))
            elif kind == "tool_use":
                calls.append(
                    FunctionCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=dict(getattr(block, "input", None) or {}),
                    )
                )
        usage_raw = getattr(raw, "usage", None)
        usage = TokenUsage(
            input_tokens=int(getattr(usage_raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage_raw, "output_tokens", 0) or 0),
            cached_input_tokens=int(getattr(usage_raw, "cache_read_input_tokens", 0) or 0),
        )
        stop_raw = str(getattr(raw, "stop_reason", "") or "")
        return ModelResponse(
            text="\n".join(t for t in texts if t).strip(),
            function_calls=tuple(calls),
            usage=usage,
            stop_reason="tool_use" if calls else _STOP_REASONS.get(stop_raw, "end_turn"),
            model=self.model,
            provider=self.name,
        )

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "backend": "messages-api"}

    def close(self) -> None:
        return None


def _unused(_: ModelMessage) -> None:  # pragma: no cover - tip importunu korur
    return None
