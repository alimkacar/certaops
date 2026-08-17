"""Deterministic in-memory implementation of the model-provider contract."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from robotics_agent.providers.base import ModelRequest, ModelResponse, TextCallback


class FakeModelProvider:
    """Return queued responses and retain requests for contract assertions."""

    provider_name = "fake"

    def __init__(self, responses: Iterable[ModelResponse] = ()) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []
        self.call_count = 0
        self.reset_count = 0
        self.closed = False

    def enqueue(self, response: ModelResponse) -> None:
        self.responses.append(response)

    def _respond(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        self.call_count += 1
        self.requests.append(request)
        response = self.responses.popleft() if self.responses else ModelResponse(
            text="Sahte provider yaniti.",
            status="completed",
            stop_reason="end_turn",
            provider=self.provider_name,
            model=request.model,
            backend="memory",
        )
        if on_text is not None and response.text:
            on_text(response.text)
        return response

    def generate(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        return self._respond(request, on_text=on_text)

    def complete(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        return self._respond(request, on_text=on_text)

    def reset(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        self.closed = True


class FailIfCalledProvider(FakeModelProvider):
    """Privacy assertion double: any model invocation fails the test."""

    def _respond(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        del request, on_text
        raise AssertionError(
            "Model cagrildi: dogrudan yanit yolu calismadi, veri provider'a cikti."
        )
