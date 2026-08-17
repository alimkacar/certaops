"""Testler icin senaryolanabilir sahte saglayici.

Canli API anahtari olmadan tum runtime davranisini dogrulamayi saglar:
hangi tool'lar modele gosterildi, kac provider cagrisi yapildi, model
bilinmeyen bir tool onerirse ne oluyor, ayni call_id iki kez gelirse tool
kac kez calisiyor.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .contracts import (
    FunctionCall,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)

__all__ = ["FakeModelProvider", "tool_call", "reply"]


def tool_call(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    call_id: str = "",
    text: str = "",
) -> ModelResponse:
    """Tek tool cagrisi oneren yanit uretir."""
    return ModelResponse(
        text=text,
        function_calls=(
            FunctionCall(
                id=call_id or f"call-{name}", name=name, arguments=arguments or {}
            ),
        ),
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        stop_reason="tool_use",
        model="fake-model",
        provider="fake",
    )


def reply(text: str) -> ModelResponse:
    """Tool cagirmayan nihai yanit uretir."""
    return ModelResponse(
        text=text,
        usage=TokenUsage(input_tokens=10, output_tokens=8),
        stop_reason="end_turn",
        model="fake-model",
        provider="fake",
    )


class FakeModelProvider:
    """Onceden yazilmis yanitlari sirayla donduren saglayici.

    ``script`` bir yanit listesi ya da ``(request) -> ModelResponse``
    fonksiyonu olabilir. Liste tukendiginde son yanit tekrarlanmaz; acik
    hata verilir - boylece "model sonsuz tool cagiriyor" hatasi testte
    sessizce gizlenmez.
    """

    name = "fake"

    def __init__(
        self,
        script: Sequence[ModelResponse] | Callable[[ModelRequest], ModelResponse] | None = None,
        *,
        model: str = "fake-model",
        raise_on: Exception | None = None,
        raise_after: int = 0,
    ) -> None:
        self.model = model
        self._script = list(script) if isinstance(script, Sequence) else script
        self._callable = script if callable(script) else None
        self.requests: list[ModelRequest] = []
        self.closed = False
        self._raise_on = raise_on
        self._raise_after = raise_after

    # --- Test yardimcilari --------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> ModelRequest:
        if not self.requests:
            raise AssertionError("Saglayici hic cagrilmadi.")
        return self.requests[-1]

    def offered_tools(self, index: int = -1) -> list[str]:
        """Modele gosterilen tool adlari."""
        return [f.name for f in self.requests[index].functions]

    def sent_function_results(self) -> list[str]:
        """Saglayiciya gonderilen tum tool sonuc govdeleri."""
        out: list[str] = []
        for request in self.requests:
            for message in request.messages:
                out.extend(r.content for r in message.function_results)
        return out

    # --- Saglayici arayuzu --------------------------------------------------
    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._raise_on is not None and len(self.requests) > self._raise_after:
            raise self._raise_on
        if self._callable is not None:
            return self._callable(request)
        if not self._script:
            raise ModelProviderError(
                "FakeModelProvider senaryosu tukendi: runtime beklenenden fazla "
                "provider cagrisi yapti.",
                provider=self.name,
            )
        return self._script.pop(0)

    def close(self) -> None:
        self.closed = True

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "backend": "memory"}
