"""Gemini Interactions API adapter with client-managed, stateless tool history."""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
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

_ALLOWED_THINKING_LEVELS = {"low", "medium", "high"}
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _integer(source: Any, name: str) -> int:
    value = _value(source, name, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _json_copy(value: Any) -> Any:
    """Copy an SDK dump without serialising or altering opaque signatures."""
    return copy.deepcopy(value)


def _dump_sdk_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _json_copy(dict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return _json_copy(dict(dumped))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, Mapping):
            return _json_copy(dict(dumped))

    # Test doubles and future SDK versions may expose plain attributes only.
    names = (
        "type",
        "id",
        "name",
        "arguments",
        "content",
        "signature",
        "summary",
    )
    dumped = {name: _json_copy(getattr(value, name)) for name in names if hasattr(value, name)}
    if dumped:
        return dumped
    raise ModelProtocolError(
        "Gemini returned a step that cannot be represented safely",
        provider="gemini",
    )


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        if _value(content, "type") == "text":
            return str(_value(content, "text", "") or "")
        if "content" in content:
            return _text_from_content(content["content"])
        if "text" in content:
            return str(content["text"] or "")
        return json.dumps(dict(content), ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(content, Sequence) and not isinstance(content, bytes | bytearray):
        return "\n".join(part for item in content if (part := _text_from_content(item)))
    text = getattr(content, "text", None)
    if text is not None:
        return str(text)
    return str(content)


def _message_transcript(messages: Sequence[Any]) -> str:
    """Render neutral history as data, never as forged Gemini model steps."""
    lines: list[str] = []
    labels = {
        "user": "KULLANICI",
        "assistant": "ONCEKI ASISTAN YANITI (veri)",
        "tool": "ONCEKI TOOL SONUCU (veri)",
        "system": "ONCEKI SISTEM MESAJI (veri)",
    }
    for message in messages:
        if isinstance(message, str):
            role = "user"
            content = message
        elif isinstance(message, Mapping):
            role = str(message.get("role", "user")).lower()
            content = message.get("content", message.get("text", ""))
        else:
            role = str(getattr(message, "role", "user")).lower()
            content = getattr(message, "content", message)
        text = _text_from_content(content).strip()
        if text:
            lines.append(f"{labels.get(role, role.upper())}:\n{text}")
    return "\n\n".join(lines)


def _messages_to_user_steps(messages: Sequence[Any]) -> list[dict[str, Any]]:
    # A caller may already provide a user_input value.  It is safe to retain
    # that input, but provider/model/thought steps are never accepted here.
    if messages and all(
        isinstance(message, Mapping) and message.get("type") == "user_input" for message in messages
    ):
        return [_json_copy(dict(message)) for message in messages]

    transcript = _message_transcript(messages)
    if not transcript:
        return []
    return [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": transcript}],
        }
    ]


def _function_result_step(result: FunctionResult) -> dict[str, Any]:
    if isinstance(result.result, str):
        text = result.result
    else:
        text = json.dumps(
            result.result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    return {
        "type": "function_result",
        "name": result.name,
        "call_id": result.call_id,
        "is_error": result.is_error,
        "result": [{"type": "text", "text": text}],
    }


def _function_declaration(tool: FunctionDeclaration) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": _json_copy(tool.parameters),
    }


def _retryable_exception(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) in _RETRYABLE_HTTP_STATUS
    except (TypeError, ValueError):
        return isinstance(exc, TimeoutError | ConnectionError)


class GeminiModelProvider:
    """Manual-function-calling adapter for Gemini's Interactions API.

    The provider always sends ``store=False``.  Opaque model steps are kept in
    memory only while a single user turn is waiting for function results.  The
    list is replaced transactionally after successful responses and erased as
    soon as the turn produces a final response.
    """

    provider_name = "gemini"

    def __init__(self, settings: Any, *, client: Any | None = None) -> None:
        cfg = resolve_agent_settings(settings)
        self._settings = cfg
        configured_backend = str(getattr(cfg, "gemini_backend", "developer") or "developer")
        configured_backend = configured_backend.strip().lower().replace("-", "_")
        if configured_backend in {"vertex_ai", "enterprise"}:
            configured_backend = "vertex"
        if configured_backend not in {"developer", "vertex"}:
            raise ModelConfigurationError(
                "GEMINI_BACKEND must be developer or vertex",
                provider=self.provider_name,
            )
        if bool(getattr(cfg, "gemini_store_interactions", False)):
            raise ModelConfigurationError(
                "Gemini interaction storage is disabled by the privacy contract",
                provider=self.provider_name,
            )

        thinking_level = str(getattr(cfg, "gemini_thinking_level", "low") or "low").lower()
        if thinking_level not in _ALLOWED_THINKING_LEVELS:
            raise ModelConfigurationError(
                "GEMINI_THINKING_LEVEL must be low, medium, or high",
                provider=self.provider_name,
            )

        self.backend = configured_backend
        self.model = str(getattr(cfg, "model", "gemini-3.7-flash") or "gemini-3.7-flash")
        self._thinking_level = thinking_level
        self._lock = threading.RLock()
        self._turn_history: list[dict[str, Any]] = []
        self._owns_client = client is None
        self.client = client if client is not None else self._create_client(cfg)

    def _create_client(self, cfg: Any) -> Any:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ModelConfigurationError(
                "Gemini provider requires the google-genai package",
                provider=self.provider_name,
            ) from exc

        if self.backend == "developer":
            api_key = str(getattr(cfg, "api_key", "") or "")
            if not api_key:
                raise ModelConfigurationError(
                    "GEMINI_API_KEY is required for the developer backend",
                    provider=self.provider_name,
                )
            return genai.Client(api_key=api_key, http_options=self._http_options(types))

        project = str(getattr(cfg, "google_cloud_project", "") or "")
        location = str(getattr(cfg, "google_cloud_location", "") or "")
        if not project or not location:
            raise ModelConfigurationError(
                "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are required for vertex",
                provider=self.provider_name,
            )
        # The current Interactions API uses the enterprise endpoint.  The
        # public configuration name remains ``vertex`` for deployment
        # compatibility with existing installations.
        return genai.Client(
            enterprise=True,
            project=project,
            location=location,
            http_options=self._http_options(types),
        )

    def _http_options(self, types: Any) -> Any:
        timeout_ms = max(
            1,
            int(float(getattr(self._settings, "timeout_seconds", 60.0)) * 1000),
        )
        retries = max(0, int(getattr(self._settings, "max_retries", 2)))
        return types.HttpOptions(
            api_version="v1",
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(
                attempts=retries + 1,
                http_status_codes=sorted(_RETRYABLE_HTTP_STATUS),
            ),
        )

    def _request_kwargs(
        self, request: ModelRequest, history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        model = request.model or self.model
        if not model:
            raise ModelConfigurationError("A Gemini model is required", provider=self.provider_name)

        kwargs: dict[str, Any] = {
            "model": model,
            "store": False,
            "input": _json_copy(history),
        }
        if request.tools:
            # JSON declarations force manual execution.  Python callables are
            # never passed, so the SDK cannot auto-run local functions.
            kwargs["tools"] = [_function_declaration(tool) for tool in request.tools]
        if request.system_instruction:
            kwargs["system_instruction"] = request.system_instruction

        generation_config: dict[str, Any] = {}
        max_output_tokens = request.max_output_tokens or int(
            getattr(self._settings, "max_tokens", 0) or 0
        )
        if max_output_tokens > 0:
            generation_config["max_output_tokens"] = max_output_tokens
        thinking_level = (request.thinking_level or self._thinking_level).lower()
        if thinking_level not in _ALLOWED_THINKING_LEVELS:
            raise ModelConfigurationError(
                "thinking_level must be low, medium, or high",
                provider=self.provider_name,
            )
        generation_config["thinking_level"] = thinking_level
        if generation_config:
            kwargs["generation_config"] = generation_config
        if request.stream:
            kwargs["stream"] = True
        return kwargs

    def generate(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        with self._lock:
            if request.new_turn:
                history = _messages_to_user_steps(request.messages)
            elif self._turn_history:
                history = _json_copy(self._turn_history)
            else:
                history = _messages_to_user_steps(request.messages)

            if request.function_results or (not request.new_turn and self._turn_history):
                self._validate_function_results(history, request.function_results)
            history.extend(_function_result_step(result) for result in request.function_results)
            if not history:
                raise ModelConfigurationError(
                    "A model request needs messages or function results",
                    provider=self.provider_name,
                )

            kwargs = self._request_kwargs(request, history)
            try:
                raw_response = self.client.interactions.create(**kwargs)
                if request.stream:
                    steps, raw_final, streamed_text = self._consume_stream(raw_response, on_text)
                    response = self._normalise_response(
                        raw_final,
                        steps=steps,
                        text=streamed_text,
                        requested_model=str(kwargs["model"]),
                    )
                else:
                    response_steps = [
                        _dump_sdk_object(step) for step in (_value(raw_response, "steps", []) or [])
                    ]
                    response = self._normalise_response(
                        raw_response,
                        steps=response_steps,
                        requested_model=str(kwargs["model"]),
                    )
            except ModelProviderError:
                raise
            except Exception as exc:
                # Do not leak provider bodies: they may echo prompts, function
                # results, credentials, or signed reasoning state.
                raise ModelProviderError(
                    "Gemini model request failed",
                    provider=self.provider_name,
                    retryable=_retryable_exception(exc),
                ) from None

            if response.function_calls:
                retained = history + steps if request.stream else history + response_steps
                self._validate_retained_history(retained)
                self._turn_history = _json_copy(retained)
            else:
                self._turn_history.clear()
            return response

    def complete(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        return self.generate(request, on_text=on_text)

    def _normalise_response(
        self,
        raw_response: Any,
        *,
        steps: list[dict[str, Any]],
        requested_model: str,
        text: str = "",
    ) -> ModelResponse:
        function_calls = self._parse_function_calls(steps)
        if not text:
            text = str(_value(raw_response, "output_text", "") or "")
        if not text:
            text = self._text_from_model_steps(steps)

        raw_status = _value(raw_response, "status", "")
        status = str(raw_status or ("requires_action" if function_calls else "completed"))
        usage = self._token_usage(_value(raw_response, "usage", None))
        model = str(_value(raw_response, "model", requested_model) or requested_model)
        return ModelResponse(
            text=text,
            function_calls=function_calls,
            usage=usage,
            status=status,
            stop_reason=status,
            provider=self.provider_name,
            model=model,
            backend=self.backend,
        )

    def _parse_function_calls(self, steps: Sequence[Mapping[str, Any]]) -> tuple[FunctionCall, ...]:
        calls: list[FunctionCall] = []
        seen: dict[str, tuple[str, dict[str, Any]]] = {}
        for step in steps:
            if _value(step, "type") != "function_call":
                continue
            call_id = str(_value(step, "id", "") or "")
            name = str(_value(step, "name", "") or "")
            arguments = _value(step, "arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError as exc:
                    raise ModelProtocolError(
                        "Gemini returned malformed function arguments",
                        provider=self.provider_name,
                    ) from exc
            if not call_id or not name or not isinstance(arguments, Mapping):
                raise ModelProtocolError(
                    "Gemini returned an invalid function call",
                    provider=self.provider_name,
                )
            arguments_dict = _json_copy(dict(arguments))
            prior = seen.get(call_id)
            current = (name, arguments_dict)
            if prior is not None:
                if prior != current:
                    raise ModelProtocolError(
                        "Gemini reused a function call id with different arguments",
                        provider=self.provider_name,
                    )
                continue
            seen[call_id] = current
            calls.append(FunctionCall(call_id=call_id, name=name, arguments=arguments_dict))
        return tuple(calls)

    @staticmethod
    def _text_from_model_steps(steps: Sequence[Mapping[str, Any]]) -> str:
        parts: list[str] = []
        for step in steps:
            if _value(step, "type") == "model_output":
                part = _text_from_content(_value(step, "content", []))
                if part:
                    parts.append(part)
        return "".join(parts)

    @staticmethod
    def _token_usage(usage: Any) -> TokenUsage:
        if usage is None:
            return TokenUsage()
        return TokenUsage(
            input_tokens=_integer(usage, "total_input_tokens"),
            output_tokens=_integer(usage, "total_output_tokens"),
            cache_read_tokens=_integer(usage, "total_cached_tokens"),
            thought_tokens=_integer(usage, "total_thought_tokens"),
            tool_use_tokens=_integer(usage, "total_tool_use_tokens"),
        )

    def _consume_stream(
        self, stream: Iterable[Any], on_text: TextCallback | None
    ) -> tuple[list[dict[str, Any]], Any, str]:
        steps_by_index: dict[int, dict[str, Any]] = {}
        argument_fragments: dict[int, str] = {}
        text_parts: list[str] = []
        final_interaction: Any = {}
        completed = False

        for event in stream:
            event_type = str(_value(event, "event_type", "") or "")
            index_value = _value(event, "index", len(steps_by_index))
            try:
                index = int(index_value)
            except (TypeError, ValueError):
                index = len(steps_by_index)

            if event_type == "step.start":
                step = _value(event, "step", None)
                if step is not None:
                    dumped = _dump_sdk_object(step)
                    steps_by_index[index] = dumped
                    arguments = dumped.get("arguments")
                    if isinstance(arguments, str):
                        argument_fragments[index] = arguments
                    elif isinstance(arguments, Mapping) and arguments:
                        argument_fragments[index] = json.dumps(
                            dict(arguments), separators=(",", ":"), sort_keys=True
                        )
                continue

            if event_type == "step.delta":
                delta = _value(event, "delta", {})
                delta_type = str(_value(delta, "type", "") or "")
                if delta_type == "text":
                    fragment = str(_value(delta, "text", "") or "")
                    if fragment:
                        text_parts.append(fragment)
                        if on_text:
                            on_text(fragment)
                        step = steps_by_index.setdefault(
                            index, {"type": "model_output", "content": []}
                        )
                        content = step.setdefault("content", [])
                        if isinstance(content, list):
                            content.append({"type": "text", "text": fragment})
                elif delta_type in {"arguments", "arguments_delta"}:
                    # ``arguments``/``partial_arguments`` is the current v1
                    # SDK shape.  ``arguments_delta``/``arguments`` is also
                    # accepted so an SDK field rename cannot silently drop a
                    # partially streamed function call.
                    fragment_field = (
                        "partial_arguments" if delta_type == "arguments" else "arguments"
                    )
                    fragment = str(_value(delta, fragment_field, "") or "")
                    argument_fragments[index] = argument_fragments.get(index, "") + fragment
                elif delta_type == "thought_signature":
                    signature = _value(delta, "signature", "")
                    steps_by_index.setdefault(index, {"type": "thought", "summary": []})[
                        "signature"
                    ] = signature
                elif delta_type == "thought_summary":
                    content_value = _value(delta, "content", None)
                    if content_value is not None:
                        if isinstance(content_value, Mapping):
                            content_dump = _json_copy(dict(content_value))
                        else:
                            content_dump = _dump_sdk_object(content_value)
                        summary = steps_by_index.setdefault(
                            index, {"type": "thought", "summary": []}
                        ).setdefault("summary", [])
                        if isinstance(summary, list):
                            summary.append(content_dump)
                continue

            if event_type == "step.stop":
                # A stop event's SDK object is preferred over synthetic deltas;
                # it preserves every opaque signature field exactly.
                step = _value(event, "step", None)
                if step is not None:
                    steps_by_index[index] = _dump_sdk_object(step)
                continue

            if event_type in {"interaction.completed", "interaction.complete"}:
                final_interaction = _value(event, "interaction", event)
                completed = True

            if event_type == "error":
                raise ModelProviderError(
                    "Gemini stream failed",
                    provider=self.provider_name,
                )

        if not completed:
            raise ModelProtocolError(
                "Gemini stream ended before interaction completion",
                provider=self.provider_name,
            )

        final_steps = _value(final_interaction, "steps", None)
        if final_steps:
            steps = [_dump_sdk_object(step) for step in final_steps]
        else:
            for index, arguments in argument_fragments.items():
                step = steps_by_index.get(index)
                if step is not None and step.get("type") == "function_call":
                    step["arguments"] = arguments
            steps = [steps_by_index[index] for index in sorted(steps_by_index)]
        return steps, final_interaction, "".join(text_parts)

    def _pending_function_calls(self, history: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        """Return unresolved calls while rejecting ambiguous retained state."""
        pending: dict[str, str] = {}
        resolved: set[str] = set()
        for step in history:
            step_type = str(_value(step, "type", "") or "")
            if step_type == "function_call":
                call_id = str(_value(step, "id", "") or "")
                name = str(_value(step, "name", "") or "")
                if not call_id or not name or call_id in pending or call_id in resolved:
                    raise ModelProtocolError(
                        "Gemini retained ambiguous function call state",
                        provider=self.provider_name,
                    )
                pending[call_id] = name
            elif step_type == "function_result":
                call_id = str(_value(step, "call_id", "") or "")
                name = str(_value(step, "name", "") or "")
                expected_name = pending.pop(call_id, None)
                if not call_id or not name or expected_name != name:
                    raise ModelProtocolError(
                        "Gemini retained an unmatched function result",
                        provider=self.provider_name,
                    )
                resolved.add(call_id)
        return pending

    def _validate_function_results(
        self,
        history: Sequence[Mapping[str, Any]],
        results: Sequence[FunctionResult],
    ) -> None:
        pending = self._pending_function_calls(history)
        supplied: dict[str, str] = {}
        for result in results:
            call_id = str(result.call_id or "")
            name = str(result.name or "")
            if call_id in supplied or pending.get(call_id) != name:
                raise ModelProtocolError(
                    "Function result does not match a retained Gemini call",
                    provider=self.provider_name,
                )
            supplied[call_id] = name
        if set(supplied) != set(pending):
            raise ModelProtocolError(
                "Function results must cover every retained Gemini call",
                provider=self.provider_name,
            )

    def _validate_retained_history(self, history: Sequence[Mapping[str, Any]]) -> None:
        self._pending_function_calls(history)
        for step in history:
            if _value(step, "type") == "thought" and not _value(step, "signature", ""):
                raise ModelProtocolError(
                    "Gemini omitted a thought signature required for stateless continuation",
                    provider=self.provider_name,
                )

    def reset(self) -> None:
        with self._lock:
            self._turn_history.clear()

    def close(self) -> None:
        self.reset()
        if not self._owns_client:
            return
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
