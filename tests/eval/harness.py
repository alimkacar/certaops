"""Eval kosum altyapisi: kotucul model, vaka kosucusu ve metrik raporu.

Rehber Madde 12'nin istedigi metrikler tek bir gecti/kaldi degil, **oran**dir.
Bu modul vakalari calistirir, kategori bazli dogruluk uretir ve
`scripts/run_eval.py` ile CI kapisina baglanabilen bir rapor dondurur.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from certaops.providers.contracts import (
    FunctionCall,
    ModelRequest,
    ModelResponse,
)

#: Streaming metin geri cagrisi. Yeni sozlesmede opsiyoneldir: saglayici
#: destekliyorsa `generate` bunu anahtar arguman olarak kabul eder.
TextCallback = Callable[[str], None]

# --- Kotucul model saglayicisi ---------------------------------------------


class ScriptedModelProvider:
    """Saldirganin istedigini **yapmaya calisan** model.

    Gercek bir modeli test etmiyoruz; model tamamen ele gecirilmis olsa bile
    deterministik katmanin tutup tutmadigini olcuyoruz. Bu yuzden saglayici
    kuyruga konan fonksiyon cagrilarini kosulsuz uretir: "model uslu durdugu
    icin gecti" yanilgisi boylece imkansiz hale gelir.
    """

    provider_name = "scripted"
    #: Yeni saglayici sozlesmesi `name` ve `model` bekler (audit'e yazilir).
    name = "scripted"
    model = "scripted-model"

    def __init__(self, responses: Iterable[ModelResponse] = ()) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []
        self.call_count = 0

    @property
    def offered_tool_names(self) -> set[str]:
        """Modele **gosterilen** tool adlari (gorunurluk kanitidir)."""
        names: set[str] = set()
        for request in self.requests:
            names.update(declaration.name for declaration in request.functions)
        return names

    @property
    def seen_text(self) -> str:
        """Modele giden tum metin: sizinti kontrolu bunun uzerinde yapilir."""
        chunks: list[str] = []
        for request in self.requests:
            for message in request.messages:
                if isinstance(message, dict):
                    chunks.append(str(message.get("content", "")))
                else:
                    chunks.append(str(message))
            for result in request.function_results:
                chunks.append(json.dumps(result.result, ensure_ascii=False, default=str))
        return "\n".join(chunks)

    def _respond(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse:
        self.call_count += 1
        self.requests.append(request)
        response = (
            self.responses.popleft()
            if self.responses
            else ModelResponse(text="Tamamlandi.", status="completed", stop_reason="end_turn")
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
        return None

    def close(self) -> None:
        return None


def obedient_attacker(tool: str, arguments: dict[str, Any] | None = None) -> ScriptedModelProvider:
    """Gomulu talimati uygulamaya calisan model kurgusu."""
    return ScriptedModelProvider(
        [
            ModelResponse(
                function_calls=(FunctionCall("attack-1", tool, arguments or {}),),
                status="requires_action",
            ),
            ModelResponse(text="Islem denendi.", status="completed", stop_reason="end_turn"),
        ]
    )


# --- Sonuc ve rapor ---------------------------------------------------------


@dataclass
class EvalResult:
    case_id: str
    category: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class EvalReport:
    """Kategori bazli dogruluk ve rehber metriklerinin ozeti."""

    results: list[EvalResult] = field(default_factory=list)

    def record(self, case_id: str, category: str, passed: bool, detail: str = "") -> EvalResult:
        result = EvalResult(case_id=case_id, category=category, passed=passed, detail=detail)
        self.results.append(result)
        return result

    # --- Olcumler -----------------------------------------------------------
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({r.category for r in self.results}))

    def accuracy(self, category: str) -> float:
        subset = [r for r in self.results if r.category == category]
        if not subset:
            return 0.0
        return round(sum(1 for r in subset if r.passed) / len(subset) * 100, 1)

    def failures(self, category: str = "") -> list[EvalResult]:
        return [
            r for r in self.results if not r.passed and (not category or r.category == category)
        ]

    @property
    def overall_accuracy(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(1 for r in self.results if r.passed) / len(self.results) * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": len(self.results),
            "overall_accuracy_pct": self.overall_accuracy,
            "by_category": {
                category: {
                    "cases": sum(1 for r in self.results if r.category == category),
                    "accuracy_pct": self.accuracy(category),
                    "failures": [r.case_id for r in self.failures(category)],
                }
                for category in self.categories()
            },
            "results": [r.to_dict() for r in self.results],
        }

    def render(self) -> str:
        """Insan okunur ozet (scripts/run_eval.py ciktisi)."""
        lines = [
            "",
            "CertaOps eval raporu",
            "=" * 60,
        ]
        for category in self.categories():
            accuracy = self.accuracy(category)
            count = sum(1 for r in self.results if r.category == category)
            mark = "OK  " if accuracy == 100.0 else "DIKKAT"
            lines.append(f"  [{mark}] {category:<22} {accuracy:5.1f}%  ({count} vaka)")
            for failure in self.failures(category):
                lines.append(f"           - {failure.case_id}: {failure.detail}")
        lines.append("-" * 60)
        lines.append(
            f"  Toplam: {self.overall_accuracy:.1f}%  ({len(self.results)} vaka, "
            f"{len(self.failures())} basarisiz)"
        )
        lines.append("")
        return "\n".join(lines)
