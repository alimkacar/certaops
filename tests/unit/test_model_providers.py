from __future__ import annotations

import copy
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from robotics_agent.providers import (
    AnthropicModelProvider,
    FunctionDeclaration,
    FunctionResult,
    GeminiModelProvider,
    ModelConfigurationError,
    ModelProtocolError,
    ModelProviderError,
    ModelRequest,
    build_model_provider,
)


def _settings(provider: str = "gemini", **overrides: Any) -> SimpleNamespace:
    values = {
        "provider": provider,
        "api_key": "test-key",
        "model": "gemini-3.7-flash" if provider == "gemini" else "claude-test",
        "max_tokens": 2048,
        "timeout_seconds": 60.0,
        "max_retries": 2,
        "temperature": 0.2,
        "gemini_backend": "developer",
        "gemini_thinking_level": "low",
        "gemini_store_interactions": False,
        "google_cloud_project": "test-project",
        "google_cloud_location": "europe-west4",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeStep:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        for key, value in payload.items():
            setattr(self, key, value)

    def model_dump(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


class FakeInteractions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeGeminiClient:
    def __init__(self, responses: list[Any]) -> None:
        self.interactions = FakeInteractions(responses)


def _fake_http_types() -> SimpleNamespace:
    return SimpleNamespace(
        HttpOptions=lambda **values: SimpleNamespace(**values),
        HttpRetryOptions=lambda **values: SimpleNamespace(**values),
    )


def _install_fake_google_sdk(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    constructor_calls: list[dict[str, Any]] = []

    def client_constructor(**kwargs: Any) -> SimpleNamespace:
        constructor_calls.append(kwargs)
        return SimpleNamespace()

    fake_types = _fake_http_types()
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = client_constructor  # type: ignore[attr-defined]
    fake_genai.types = fake_types  # type: ignore[attr-defined]
    fake_google = ModuleType("google")
    fake_google.__path__ = []  # type: ignore[attr-defined]
    fake_google.genai = fake_genai  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    return constructor_calls


def _gemini_response(
    *steps: FakeStep,
    text: str = "",
    status: str = "completed",
) -> SimpleNamespace:
    return SimpleNamespace(
        steps=list(steps),
        output_text=text,
        status=status,
        model="gemini-3.7-flash",
        usage=SimpleNamespace(
            total_input_tokens=11,
            total_output_tokens=7,
            total_cached_tokens=3,
            total_thought_tokens=2,
            total_tool_use_tokens=1,
        ),
    )


def test_function_declaration_and_factory_are_provider_neutral() -> None:
    tool = FunctionDeclaration(
        "get_status",
        "Read status",
        {"type": "object", "properties": {"id": {"type": "string"}}},
    )
    assert tool.to_dict() == {
        "name": "get_status",
        "description": "Read status",
        "parameters": tool.parameters,
    }

    gemini_client = FakeGeminiClient([])
    assert isinstance(build_model_provider(_settings(), client=gemini_client), GeminiModelProvider)

    anthropic_client = SimpleNamespace(messages=SimpleNamespace())
    provider = build_model_provider(_settings("anthropic"), client=anthropic_client)
    assert isinstance(provider, AnthropicModelProvider)


def test_gemini_maps_json_tools_without_deprecated_sampling_parameters() -> None:
    thought = {"type": "thought", "signature": "opaque-secret-signature", "summary": []}
    function_call = {
        "type": "function_call",
        "id": "call-1",
        "name": "get_status",
        "arguments": {"id": "4500000010"},
    }
    client = FakeGeminiClient(
        [
            _gemini_response(
                FakeStep(thought),
                FakeStep(function_call),
                status="requires_action",
            )
        ]
    )
    provider = GeminiModelProvider(_settings(temperature=0.99), client=client)
    tool = FunctionDeclaration(
        "get_status",
        "Read a document status",
        {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )

    response = provider.generate(
        ModelRequest(
            messages=({"role": "user", "content": "4500000010 durumunu getir"},),
            tools=(tool,),
            system_instruction="Use only declared tools.",
        )
    )

    assert response.function_calls[0].call_id == "call-1"
    assert response.function_calls[0].arguments == {"id": "4500000010"}
    assert response.usage.cache_read_tokens == 3
    request = client.interactions.calls[0]
    assert request["store"] is False
    assert request["tools"] == [
        {
            "type": "function",
            "name": "get_status",
            "description": "Read a document status",
            "parameters": tool.parameters,
        }
    ]
    assert request["generation_config"] == {
        "max_output_tokens": 2048,
        "thinking_level": "low",
    }
    forbidden = {
        "temperature",
        "top_p",
        "top_k",
        "candidate_count",
        "automatic_function_calling",
        "previous_interaction_id",
    }
    assert forbidden.isdisjoint(request)
    assert forbidden.isdisjoint(request["generation_config"])
    assert all(isinstance(declaration, dict) for declaration in request["tools"])


def test_gemini_preserves_opaque_steps_only_for_the_current_tool_turn() -> None:
    thought = {"type": "thought", "signature": "opaque-signature", "summary": []}
    call = {
        "type": "function_call",
        "id": "call-9",
        "name": "read_po",
        "arguments": {"po": "9"},
    }
    client = FakeGeminiClient(
        [
            _gemini_response(FakeStep(thought), FakeStep(call), status="requires_action"),
            _gemini_response(
                FakeStep(
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "Siparis acik."}],
                    }
                ),
                text="Siparis acik.",
            ),
            _gemini_response(
                FakeStep(
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "Yeni tur."}],
                    }
                ),
                text="Yeni tur.",
            ),
        ]
    )
    provider = GeminiModelProvider(_settings(), client=client)
    request = ModelRequest(messages=({"role": "user", "content": "PO 9"},))

    first = provider.generate(request)
    assert first.needs_action
    second = provider.generate(
        ModelRequest(
            new_turn=False,
            function_results=(FunctionResult("call-9", "read_po", {"status": "OPEN"}),),
        )
    )
    assert second.text == "Siparis acik."

    continued_history = client.interactions.calls[1]["input"]
    assert thought in continued_history
    assert call in continued_history
    assert continued_history[-1] == {
        "type": "function_result",
        "name": "read_po",
        "call_id": "call-9",
        "is_error": False,
        "result": [{"type": "text", "text": '{"status":"OPEN"}'}],
    }

    provider.generate(ModelRequest(messages=({"role": "user", "content": "Yeni soru"},)))
    new_history = client.interactions.calls[2]["input"]
    assert all(step.get("signature") != "opaque-signature" for step in new_history)
    assert all(step.get("type") != "function_call" for step in new_history)


def test_gemini_rejects_server_storage_and_sanitises_remote_errors() -> None:
    with pytest.raises(ModelConfigurationError, match="storage is disabled"):
        GeminiModelProvider(
            _settings(gemini_store_interactions=True),
            client=FakeGeminiClient([]),
        )

    client = FakeGeminiClient([RuntimeError("secret prompt and token")])
    provider = GeminiModelProvider(_settings(), client=client)
    with pytest.raises(ModelProviderError) as error:
        provider.generate(ModelRequest(messages=("hello",)))
    assert str(error.value) == "Gemini model request failed"
    assert "secret" not in str(error.value)


def test_gemini_http_budget_retries_only_model_requests() -> None:
    provider = GeminiModelProvider(
        _settings(timeout_seconds=12.5, max_retries=3),
        client=FakeGeminiClient([]),
    )
    options = provider._http_options(  # noqa: SLF001 - adapter contract testi
        _fake_http_types()
    )

    assert options.api_version == "v1"
    assert options.timeout == 12_500
    assert options.retry_options.attempts == 4
    assert 429 in options.retry_options.http_status_codes


def test_gemini_builds_developer_client_with_stable_v1_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = _install_fake_google_sdk(monkeypatch)

    GeminiModelProvider(_settings(api_key="developer-key"))

    assert len(constructor_calls) == 1
    kwargs = constructor_calls[0]
    assert kwargs["api_key"] == "developer-key"
    assert "enterprise" not in kwargs
    assert kwargs["http_options"].api_version == "v1"


def test_gemini_builds_enterprise_client_with_project_and_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = _install_fake_google_sdk(monkeypatch)

    GeminiModelProvider(
        _settings(
            gemini_backend="vertex",
            api_key="",
            google_cloud_project="enterprise-project",
            google_cloud_location="europe-west4",
        )
    )

    assert len(constructor_calls) == 1
    kwargs = constructor_calls[0]
    assert kwargs["enterprise"] is True
    assert kwargs["project"] == "enterprise-project"
    assert kwargs["location"] == "europe-west4"
    assert "api_key" not in kwargs
    assert kwargs["http_options"].api_version == "v1"


@pytest.mark.parametrize(
    "results",
    [
        (),
        (FunctionResult("unknown", "read_po", {"status": "OPEN"}),),
        (FunctionResult("call-1", "wrong_tool", {"status": "OPEN"}),),
        (
            FunctionResult("call-1", "read_po", {"status": "OPEN"}),
            FunctionResult("call-1", "read_po", {"status": "OPEN"}),
        ),
    ],
)
def test_gemini_rejects_function_results_not_matching_retained_calls(
    results: tuple[FunctionResult, ...],
) -> None:
    client = FakeGeminiClient(
        [
            _gemini_response(
                FakeStep(
                    {
                        "type": "function_call",
                        "id": "call-1",
                        "name": "read_po",
                        "arguments": {"po": "9"},
                    }
                ),
                status="requires_action",
            )
        ]
    )
    provider = GeminiModelProvider(_settings(), client=client)
    provider.generate(ModelRequest(messages=("PO 9",)))

    with pytest.raises(ModelProtocolError, match="Function result"):
        provider.generate(ModelRequest(new_turn=False, function_results=results))
    assert len(client.interactions.calls) == 1


def test_gemini_streams_text_without_storing_interaction_state() -> None:
    final_interaction = SimpleNamespace(
        status="completed",
        model="gemini-3.7-flash",
        usage=SimpleNamespace(total_input_tokens=2, total_output_tokens=1),
    )
    events = [
        SimpleNamespace(
            event_type="step.start",
            index=0,
            step=FakeStep({"type": "model_output", "content": []}),
        ),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="text", text="Merhaba"),
        ),
        SimpleNamespace(
            event_type="step.stop",
            index=0,
            step=FakeStep(
                {
                    "type": "model_output",
                    "content": [{"type": "text", "text": "Merhaba"}],
                }
            ),
        ),
        SimpleNamespace(event_type="interaction.completed", interaction=final_interaction),
    ]
    client = FakeGeminiClient([events])
    provider = GeminiModelProvider(_settings(), client=client)
    fragments: list[str] = []

    response = provider.generate(
        ModelRequest(messages=("Selam",), stream=True),
        on_text=fragments.append,
    )

    assert response.text == "Merhaba"
    assert fragments == ["Merhaba"]
    assert client.interactions.calls[0]["stream"] is True
    assert client.interactions.calls[0]["store"] is False


def test_gemini_stream_accepts_arguments_delta_compatibility_shape() -> None:
    final_interaction = SimpleNamespace(
        status="requires_action",
        model="gemini-3.7-flash",
        usage=SimpleNamespace(total_input_tokens=2, total_output_tokens=1),
    )
    events = [
        SimpleNamespace(
            event_type="step.start",
            index=0,
            step=FakeStep(
                {
                    "type": "function_call",
                    "id": "call-stream-1",
                    "name": "read_po",
                    "arguments": {},
                }
            ),
        ),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="arguments_delta", arguments='{"po":"9"}'),
        ),
        SimpleNamespace(event_type="step.stop", index=0),
        SimpleNamespace(event_type="interaction.completed", interaction=final_interaction),
    ]
    provider = GeminiModelProvider(_settings(), client=FakeGeminiClient([events]))

    response = provider.generate(ModelRequest(messages=("PO 9",), stream=True))

    assert len(response.function_calls) == 1
    assert response.function_calls[0].call_id == "call-stream-1"
    assert response.function_calls[0].name == "read_po"
    assert response.function_calls[0].arguments == {"po": "9"}


def test_gemini_stream_ending_without_completion_fails_closed() -> None:
    events = [
        SimpleNamespace(
            event_type="step.start",
            index=0,
            step=FakeStep({"type": "model_output", "content": []}),
        ),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="text", text="Kismi yanit"),
        ),
    ]
    provider = GeminiModelProvider(_settings(), client=FakeGeminiClient([events]))

    with pytest.raises(ModelProtocolError, match="before interaction completion"):
        provider.generate(ModelRequest(messages=("Selam",), stream=True))


class FakeAnthropicMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def test_anthropic_adapter_is_lazy_and_keeps_manual_tool_contract() -> None:
    first_raw = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Kontrol ediyorum."),
            SimpleNamespace(
                type="tool_use",
                id="tool-1",
                name="read_po",
                input={"po": "9"},
            ),
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=2,
            cache_creation_input_tokens=1,
        ),
        stop_reason="tool_use",
        model="claude-test",
    )
    final_raw = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="PO acik.")],
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
        stop_reason="end_turn",
        model="claude-test",
    )
    messages = FakeAnthropicMessages([first_raw, final_raw])
    provider = AnthropicModelProvider(
        _settings("anthropic"),
        client=SimpleNamespace(messages=messages),
    )
    tool = FunctionDeclaration("read_po", "Read PO", {"type": "object"})

    first = provider.generate(ModelRequest(messages=("PO 9",), tools=(tool,)))
    assert first.function_calls[0].arguments == {"po": "9"}
    assert messages.calls[0]["tools"] == [
        {"name": "read_po", "description": "Read PO", "input_schema": {"type": "object"}}
    ]
    assert messages.calls[0]["temperature"] == 0.2

    final = provider.generate(
        ModelRequest(
            new_turn=False,
            function_results=(FunctionResult("tool-1", "read_po", {"status": "OPEN"}),),
            tools=(tool,),
        )
    )
    assert final.text == "PO acik."
    assert messages.calls[1]["messages"][-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": '{"status":"OPEN"}',
                "is_error": False,
            }
        ],
    }
