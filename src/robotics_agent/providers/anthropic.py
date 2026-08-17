"""Lazy Anthropic compatibility adapter for existing deployments."""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from .base import (
    FunctionCall,
    FunctionDeclaration,
    FunctionResult,
    ModelConfigurationError,
    ModelProtocolError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    TextCallback,
    TokenUsage,
    resolve_agent_settings,
)


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _integer(source: Any, name: str) -> int:
    try:
        return int(_value(source, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _json_result(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _normalise_messages(messages: Sequence[Any]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, str):
            role, content = "user", message
        elif isinstance(message, Mapping):
            role = str(message.get("role", "user")).lower()
            content = copy.deepcopy(message.get("content", message.get("text", "")))
        else:
            role = str(getattr(message, "role", "user")).lower()
            content = copy.deepcopy(getattr(message, "content", str(message)))
        if role not in {"user", "assistant"}:
            role = "user"
        normalised.append({"role": role, "content": content})
    return normalised


def _tool_definition(tool: FunctionDeclaration) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": copy.deepcopy(tool.parameters),
    }


def _tool_results(results: Sequence[FunctionResult]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": _json_result(result.result),
                "is_error": result.is_error,
            }
            for result in results
        ],
    }


def _dump_content_block(block: Any) -> dict[str, Any]:
    if isinstance(block, Mapping):
        return copy.deepcopy(dict(block))
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return copy.deepcopy(dict(dumped))
    block_type = str(getattr(block, "type", "") or "")
    if block_type == "text":
        return {"type": "text", "text": str(getattr(block, "text", "") or "")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": str(getattr(block, "id", "") or ""),
            "name": str(getattr(block, "name", "") or ""),
            "input": copy.deepcopy(getattr(block, "input", {}) or {}),
        }
    raise ModelProtocolError(
        "Anthropic returned an unsupported content block",
        provider="anthropic",
    )


class AnthropicModelProvider:
    """Compatibility provider that imports the optional SDK only when used."""

    provider_name = "anthropic"

    def __init__(self, settings: Any, *, client: Any | None = None) -> None:
        cfg = resolve_agent_settings(settings)
        self._settings = cfg
        self.model = str(getattr(cfg, "model", "") or "")
        self.backend = "anthropic"
        self._lock = threading.RLock()
        self._turn_messages: list[dict[str, Any]] = []
        self._owns_client = client is None
        self.client = client if client is not None else self._create_client(cfg)

    def _create_client(self, cfg: Any) -> Any:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ModelConfigurationError(
                "Anthropic provider requires the optional anthropic package",
                provider=self.provider_name,
            ) from exc
        api_key = str(getattr(cfg, "api_key", "") or "")
        if not api_key:
            raise ModelConfigurationError(
                "ANTHROPIC_API_KEY is required for the Anthropic provider",
                provider=self.provider_name,
            )
        return Anthropic(
            api_key=api_key,
            timeout=float(getattr(cfg, "timeout_seconds", 60.0)),
            max_retries=int(getattr(cfg, "max_retries", 2)),
        )

    def _request_kwargs(
        self, request: ModelRequest, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        model = request.model or self.model
        if not model:
            raise ModelConfigurationError(
                "An Anthropic model is required",
                provider=self.provider_name,
            )
        max_tokens = request.max_output_tokens or int(getattr(self._settings, "max_tokens", 0) or 0)
        if max_tokens <= 0:
            raise ModelConfigurationError(
                "max_output_tokens must be positive",
                provider=self.provider_name,
            )
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": copy.deepcopy(messages),
        }
        if request.system_instruction:
            kwargs["system"] = request.system_instruction
        if request.tools:
            kwargs["tools"] = [_tool_definition(tool) for tool in request.tools]
        temperature = getattr(self._settings, "temperature", None)
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        return kwargs

    def generate(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        with self._lock:
            if request.new_turn:
                messages = _normalise_messages(request.messages)
            elif self._turn_messages:
                messages = copy.deepcopy(self._turn_messages)
            else:
                messages = _normalise_messages(request.messages)
            if request.function_results:
                messages.append(_tool_results(request.function_results))
            if not messages:
                raise ModelConfigurationError(
                    "A model request needs messages or function results",
                    provider=self.provider_name,
                )

            kwargs = self._request_kwargs(request, messages)
            try:
                if request.stream:
                    with self.client.messages.stream(**kwargs) as stream:
                        for fragment in stream.text_stream:
                            if on_text:
                                on_text(fragment)
                        raw_response = stream.get_final_message()
                else:
                    raw_response = self.client.messages.create(**kwargs)
                response, assistant_content = self._normalise_response(raw_response)
            except ModelProviderError:
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", 0)
                retryable = status in {408, 409, 425, 429, 500, 502, 503, 504} or isinstance(
                    exc, TimeoutError | ConnectionError
                )
                raise ModelProviderError(
                    "Anthropic model request failed",
                    provider=self.provider_name,
                    retryable=retryable,
                ) from None

            if response.function_calls:
                messages.append({"role": "assistant", "content": assistant_content})
                self._turn_messages = copy.deepcopy(messages)
            else:
                self._turn_messages.clear()
            return response

    def complete(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        return self.generate(request, on_text=on_text)

    def _normalise_response(self, raw_response: Any) -> tuple[ModelResponse, list[dict[str, Any]]]:
        blocks = [
            _dump_content_block(block) for block in (_value(raw_response, "content", []) or [])
        ]
        text = "".join(
            str(block.get("text", "")) for block in blocks if block.get("type") == "text"
        )
        calls: list[FunctionCall] = []
        seen: set[str] = set()
        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            call_id = str(block.get("id", "") or "")
            name = str(block.get("name", "") or "")
            arguments = block.get("input", {})
            if not call_id or not name or not isinstance(arguments, Mapping):
                raise ModelProtocolError(
                    "Anthropic returned an invalid function call",
                    provider=self.provider_name,
                )
            if call_id in seen:
                continue
            seen.add(call_id)
            calls.append(FunctionCall(call_id, name, copy.deepcopy(dict(arguments))))

        usage = _value(raw_response, "usage", None)
        token_usage = TokenUsage(
            input_tokens=_integer(usage, "input_tokens"),
            output_tokens=_integer(usage, "output_tokens"),
            cache_read_tokens=_integer(usage, "cache_read_input_tokens"),
            cache_write_tokens=_integer(usage, "cache_creation_input_tokens"),
        )
        stop_reason = str(_value(raw_response, "stop_reason", "") or "")
        response = ModelResponse(
            text=text,
            function_calls=tuple(calls),
            usage=token_usage,
            status="requires_action" if calls else "completed",
            stop_reason=stop_reason,
            provider=self.provider_name,
            model=str(_value(raw_response, "model", self.model) or self.model),
            backend=self.backend,
        )
        return response, blocks

    def reset(self) -> None:
        with self._lock:
            self._turn_messages.clear()

    def close(self) -> None:
        self.reset()
        if not self._owns_client:
            return
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
