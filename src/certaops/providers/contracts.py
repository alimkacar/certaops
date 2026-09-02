"""Saglayici-bagimsiz model sozlesmeleri.

Core runtime hicbir LLM saglayicisinin SDK tipini gormez. Anthropic'in
``tool_use`` blogu, Gemini'nin ``FunctionCall`` parcasi, OpenAI'nin
``tool_calls`` dizisi - hepsi bu moduldeki notr tiplere cevrilir ve runtime
yalnizca bunlari bilir.

Neden onemli
------------
Saglayici tipleri runtime'a sizarsa iki sey olur: (1) saglayici degistirmek
tum donguyu yeniden yazmak demektir, (2) daha kotusu, guvenlik mantigi
saglayiciya ozgu dallanmalarla dolar ve hangi yolun policy'den gectigini
kimse takip edemez. Tek bir notr sozlesme, guvenlik katmanini tek yerde
tutar.

Gizlilik notu
-------------
``FunctionCall.provider_state`` ve ``ModelMessage.provider_state`` saglayiciya
ait opak devam bilgisini tasir (Gemini "thought signature" gibi). Bu alanlar:

  * denetim defterine YAZILMAZ,
  * loglara BASILMAZ,
  * tenant'lar arasinda PAYLASILMAZ,
  * normal konusma metnine CEVRILMEZ.

``__repr__`` bilerek bu alanlari gizler; yanlislikla bir log satirina
dusmesini engeller.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
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
]

#: Modelin turu neden bitirdigi. Saglayici kodlari buraya normallestirilir.
StopReason = Literal["end_turn", "tool_use", "max_tokens", "safety", "other"]

#: Muhakeme butcesi. Saglayici destegi yoksa yok sayilir.
ThinkingLevel = Literal["minimal", "low", "medium", "high"]

_REDACTED = "<gizli>"


# --- Hatalar ----------------------------------------------------------------
class ModelProviderError(RuntimeError):
    """Saglayici hatalarinin ortak taban sinifi.

    ``retryable`` **yalnizca saglayici cagrisinin kendisi** icin gecerlidir.
    Runtime'in bir SAP tool'unu tekrar calistirmasi ANLAMINA GELMEZ; tool
    yurutmesi call_id bazinda tekilllenir (bkz. runtime).
    """

    kind: str = "provider_error"
    retryable: bool = False

    def __init__(self, message: str, *, provider: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "kind": self.kind,
            "provider": self.provider,
            "retryable": self.retryable,
            "status": self.status,
        }


class ModelAuthError(ModelProviderError):
    """API anahtari gecersiz veya yetki yok. Tekrar denemek anlamsizdir."""

    kind = "auth"
    retryable = False


class ModelRateLimitError(ModelProviderError):
    kind = "rate_limit"
    retryable = True


class ModelTimeoutError(ModelProviderError):
    kind = "timeout"
    retryable = True


class ModelUnavailableError(ModelProviderError):
    """5xx / gecici kesinti."""

    kind = "unavailable"
    retryable = True


class ModelBadRequestError(ModelProviderError):
    """Istek gecersiz (sema, model adi, token siniri). Tekrar denemek duzeltmez."""

    kind = "bad_request"
    retryable = False


# --- Veri tipleri -----------------------------------------------------------
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def merge(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class FunctionDeclaration:
    """Modele gosterilen tool sozlesmesi (JSON Schema)."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


#: `provider_state` opak bir saglayici degeridir; Gemini'de `bytes` gelir.
#: JSON bytes tasiyamadigi icin base64'e sarilir ve geri yuklenirken acilir.
#: Sarmalayici bir isaretci sozluktur; boylece "base64 gibi gorunen bir string"
#: ile gercek bytes birbirine karismaz.
_BYTES_TAG = "__b64__"


def _state_to_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bytes):
        return {_BYTES_TAG: base64.b64encode(value).decode("ascii")}
    if isinstance(value, (str, int, float, bool)):
        return value
    # Taninmayan bir tip sessizce string'e cevrilmez: geri yuklendiginde
    # saglayiciya bozuk bir devam bilgisi gitmesindense hic gitmemesi iyidir.
    return None


def _state_from_json(value: Any) -> Any:
    if isinstance(value, Mapping) and _BYTES_TAG in value:
        try:
            return base64.b64decode(str(value[_BYTES_TAG]).encode("ascii"))
        except (ValueError, TypeError):
            return None
    return value


@dataclass(frozen=True)
class FunctionCall:
    """Modelin ONERDIGI tool cagrisi.

    Bu bir **oneri**dir, yurutme emri degil. Runtime her cagriyi
    ``execute_tool`` uzerinden policy/DLP/audit katmanlarindan gecirir.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: Saglayiciya ait opak devam bilgisi (thought signature vb.).
    #: ASLA loglanmaz, audit'e yazilmaz, tenant'lar arasi paylasilmaz.
    provider_state: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:  # pragma: no cover - teshis kolayligi
        state = _REDACTED if self.provider_state is not None else "None"
        return f"FunctionCall(id={self.id!r}, name={self.name!r}, provider_state={state})"

    def to_audit_dict(self) -> dict[str, Any]:
        """Denetim defterine yazilabilir hal: provider_state HARIC."""
        return {"call_id": self.id, "name": self.name}

    def to_dict(self) -> dict[str, Any]:
        """Oturum kaydi icin JSON'a yazilabilir hal.

        `to_audit_dict`ten farki: bu hal turu SURDURMEK icindir, denetim
        icin degil. `provider_state` (thought signature) korunur, cunku
        Gemini cok adimli function calling'de onu geri bekler. Deger opaktir
        ve yalniz ayni oturumun saglayici cagrisina geri doner.
        """
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
            "provider_state": _state_to_json(self.provider_state),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FunctionCall:
        return cls(
            id=str(data.get("id", "") or ""),
            name=str(data.get("name", "") or ""),
            arguments=dict(data.get("arguments") or {}),
            provider_state=_state_from_json(data.get("provider_state")),
        )


@dataclass(frozen=True)
class FunctionResult:
    """Tool yurutmesinin modele donen sonucu."""

    call_id: str
    name: str
    content: str
    is_error: bool = False

    def payload(self) -> dict[str, Any]:
        """Saglayicilarin bekledigi sozluk govdesi."""
        try:
            parsed = json.loads(self.content)
        except (TypeError, ValueError):
            return {"result": self.content}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "content": self.content,
            "is_error": self.is_error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FunctionResult:
        return cls(
            call_id=str(data.get("call_id", "") or ""),
            name=str(data.get("name", "") or ""),
            content=str(data.get("content", "") or ""),
            is_error=bool(data.get("is_error", False)),
        )


@dataclass(frozen=True)
class ModelMessage:
    """Konusma gecmisindeki tek adim."""

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    function_calls: tuple[FunctionCall, ...] = ()
    function_results: tuple[FunctionResult, ...] = ()
    #: Saglayiciya ait opak devam bilgisi. Gizlilik kurallari FunctionCall ile ayni.
    provider_state: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ModelMessage(role={self.role!r}, text_len={len(self.text)}, "
            f"calls={len(self.function_calls)}, results={len(self.function_results)})"
        )

    def with_text(self, text: str) -> ModelMessage:
        return replace(self, text=text)

    def to_dict(self) -> dict[str, Any]:
        """Oturum kaydina yazilabilir hal.

        Bu metot OLMADAN `json.dumps(..., default=str)` dataclass'i
        `__repr__` string'ine cevirir; geri yuklendiginde gecmis duz
        string listesi olur ve saglayici siniri `message.role` uzerinde
        AttributeError verir.
        """
        return {
            "role": self.role,
            "text": self.text,
            "function_calls": [c.to_dict() for c in self.function_calls],
            "function_results": [r.to_dict() for r in self.function_results],
            "provider_state": _state_to_json(self.provider_state),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModelMessage:
        role = str(data.get("role", "") or "")
        if role not in ("user", "assistant", "tool"):
            raise ValueError(f"gecersiz mesaj rolu: {role!r}")
        return cls(
            role=role,  # type: ignore[arg-type]
            text=str(data.get("text", "") or ""),
            function_calls=tuple(
                FunctionCall.from_dict(c) for c in (data.get("function_calls") or [])
            ),
            function_results=tuple(
                FunctionResult.from_dict(r) for r in (data.get("function_results") or [])
            ),
            provider_state=_state_from_json(data.get("provider_state")),
        )



def messages_to_dicts(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Oturum kaydina yazilacak hal. `ModelMessage` olmayanlar atlanir."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelMessage):
            out.append(message.to_dict())
        elif isinstance(message, Mapping) and "role" in message:
            out.append(dict(message))
    return out


def messages_from_dicts(rows: Iterable[Any]) -> list[ModelMessage]:
    """Oturum kaydindan geri yukleme.

    Bozuk veya eski bicimdeki kayitlar **atlanir**, hata firlatilmaz. Eski
    surumler gecmisi `__repr__` string'i olarak yazmisti; o kayitlarla bir
    turu bastan bozmaktansa gecmisi kisaltmak dogru davranistir.
    """
    out: list[ModelMessage] = []
    for row in rows:
        if isinstance(row, ModelMessage):
            out.append(row)
            continue
        if not isinstance(row, Mapping):
            continue
        try:
            out.append(ModelMessage.from_dict(row))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


@dataclass(frozen=True)
class ModelRequest:
    """Saglayiciya gonderilecek tek istek."""

    system: str
    messages: Sequence[ModelMessage]
    functions: Sequence[FunctionDeclaration] = ()
    max_output_tokens: int = 4096
    thinking_level: ThinkingLevel = "low"
    stream: bool = False
    timeout_s: float = 90.0
    #: Saglayicinin istegi/yaniti saklamasina izin verilsin mi.
    #: Varsayilan False: SAP verisi saglayici tarafinda kalici olmamalidir.
    store: bool = False

    def describe(self) -> dict[str, Any]:
        """Audit icin guvenli ozet. **Icerik yok**, yalnizca sekil."""
        return {
            "message_count": len(self.messages),
            "function_count": len(self.functions),
            "function_names": [f.name for f in self.functions],
            "max_output_tokens": self.max_output_tokens,
            "thinking_level": self.thinking_level,
            "store": self.store,
        }


@dataclass(frozen=True)
class ModelResponse:
    """Saglayicidan donen normallestirilmis yanit."""

    text: str = ""
    function_calls: tuple[FunctionCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    #: Saglayici cagrisinin SONUCU. `stop_reason`dan farklidir: o modelin turu
    #: neden bitirdigini soyler, bu cagrinin saglikli tamamlanip tamamlanmadigini.
    #:
    #:   SAGLIKLI : "completed" (tur bitti), "requires_action" (model tool
    #:              cagirmak istiyor - NORMAL akis, hata degil)
    #:   BOZUK    : "failed", "cancelled", "incomplete"
    #:
    #: Bozuk bir yanittaki function call YARIM olabilir; runtime bunlari
    #: calistirmaz. Taninmayan bir deger de guvenli tarafta kalinarak bozuk
    #: sayilir, ama sessizce degil: ERROR seviyesinde loglanir.
    status: str = "completed"
    stop_reason: StopReason = "end_turn"
    model: str = ""
    provider: str = ""
    #: Saglayiciya ait opak devam bilgisi (asistan turunu geri gonderirken gerekir).
    provider_state: Any = field(default=None, repr=False, compare=False)

    @property
    def wants_tools(self) -> bool:
        return bool(self.function_calls)

    def to_assistant_message(self) -> ModelMessage:
        return ModelMessage(
            role="assistant",
            text=self.text,
            function_calls=self.function_calls,
            provider_state=self.provider_state,
        )

    def to_audit_dict(self) -> dict[str, Any]:
        """provider_state ve metin ICERMEZ; yalnizca olculebilir ust veri."""
        return {
            "provider": self.provider,
            "model": self.model,
            "stop_reason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "function_calls": [c.to_audit_dict() for c in self.function_calls],
        }


# --- Saglayici arayuzu ------------------------------------------------------
@runtime_checkable
class ModelProvider(Protocol):
    """Tum LLM saglayicilarinin uydugu sozlesme.

    Uygulayanlar:
      * ``name``   -> audit'e yazilan saglayici kimligi ("gemini", "anthropic")
      * ``model``  -> audit'e yazilan model adi
      * ``generate`` -> tek istek/yanit; function calling dahil
      * ``close``  -> kaynaklari birak

    ``generate`` **tool yurutmezi**. Modelin onerdigi cagrilari dondurur;
    yurutme karari ve policy denetimi runtime'a aittir.
    """

    name: str
    model: str

    def generate(self, request: ModelRequest) -> ModelResponse: ...

    def close(self) -> None: ...

    # Opsiyonel: saglayici streaming destekliyorsa `generate` ek bir
    # `on_text` anahtar argumani kabul eder ve `request.stream=True` iken
    # metin parcalarini uretildikce iletir. Function call'lar HER ZAMAN
    # akis bittikten sonra dondurulur - yarim argumanla SAP yazmasi
    # calistirilamaz. Runtime bu ozelligi opsiyonel kabul eder.


def redact_provider_state(value: Any) -> Any:
    """Bir yapinin icindeki opak saglayici durumunu gizler.

    Log/audit yollarinda savunma amaclidir: bir dict icinde yanlislikla
    tasinan `provider_state`/`thought_signature` anahtarlari maskelenir.
    """
    secret_keys = {"provider_state", "thought_signature", "thoughtsignature", "signature"}
    if isinstance(value, Mapping):
        return {
            k: (_REDACTED if str(k).lower() in secret_keys else redact_provider_state(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_provider_state(v) for v in value]
    return value
