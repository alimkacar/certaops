"""Google Gemini saglayici adaptoru (resmi ``google-genai`` SDK'si).

Kapsam
------
* Gemini Developer API ve Vertex AI backend'i ayni arayuzden secilir.
* Function calling: modelin **onerdigi** cagrilar dondurulur.
* Gemini 3 muhakeme butcesi ``thinking_level`` ile verilir.
* ``temperature`` / ``top_p`` / ``top_k`` / ``candidate_count`` GONDERILMEZ.

Kritik guvenlik karari: otomatik fonksiyon yurutme KAPALI
---------------------------------------------------------
SDK, Python fonksiyonlarini tool olarak alip modelin cagrisini **kendisi**
calistirabilir (automatic function calling). Bu proje icin bu ozellik bir
guvenlik acigidir: SAP tool handler'i dogrudan cagrilirsa RBAC, ABAC, risk
skoru, insan onayi, idempotency, DLP, audit ve butce katmanlarinin tamami
atlanir.

Bu yuzden:
  * SDK'ya **hicbir zaman cagrilabilir Python nesnesi verilmez**; yalnizca
    ``types.FunctionDeclaration`` (saf sema) gonderilir,
  * ``AutomaticFunctionCallingConfig(disable=True)`` her istekte set edilir,
  * ``maximum_remote_calls=0`` ile ikinci bir emniyet kemeri takilir.

Ikisi birden, SDK surumu degisse bile otomatik yurutmeyi kapali tutar.

Thought signature (Gemini 3)
----------------------------
Model, cok adimli function calling'de kendi muhakeme baglamini opak bir
``thought_signature`` ile tasir ve sonraki turda geri gonderilmesini bekler.
Bu deger:
  * ``FunctionCall.provider_state`` icinde tasinir,
  * denetim defterine ve loglara YAZILMAZ,
  * konusma metnine cevrilmez,
  * yalnizca ayni oturumun sonraki istegine geri konur.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
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

__all__ = ["GeminiProvider", "classify_gemini_error"]

#: Gemini 3'te kaldirilan orneklem parametreleri. Gonderilirse istek reddedilir
#: ya da sessizce yok sayilir; ikisi de istenmez. Test bu kumeyi dogrular.
DEPRECATED_SAMPLING_FIELDS = frozenset(
    {"temperature", "top_p", "top_k", "candidate_count"}
)

# Gemini 3.7 Flash `MINIMAL` seviyesini KALDIRDI; istenirse API hata doner.
# Notr sozlesme `minimal`i tanimaya devam eder (baska saglayicilar destekleyebilir),
# bu yuzden kelepceleme burada, adaptorde yapilir: yapilandirma gecerli kalir,
# istek gecersiz gitmez.
_THINKING_LEVELS = {"minimal": "LOW", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}

_STOP_REASONS: dict[str, StopReason] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "safety",
    "RECITATION": "safety",
    "PROHIBITED_CONTENT": "safety",
    "BLOCKLIST": "safety",
}


def classify_gemini_error(exc: Exception, *, provider: str = "gemini") -> ModelProviderError:
    """SDK istisnasini saglayici-bagimsiz hata sinifina cevirir.

    Siniflandirmanin amaci tek bir soruyu cevaplamak: **tekrar denemek
    mantikli mi?** Yanlis siniflandirma ya gereksiz gecikme (kalici hatayi
    tekrarlamak) ya da gereksiz basarisizlik (gecici hatayi kalici saymak)
    uretir.
    """
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None
    text = str(exc)
    lowered = text.lower()

    if isinstance(exc, TimeoutError) or "timeout" in lowered or "deadline" in lowered:
        return ModelTimeoutError(text, provider=provider, status=status)
    if status in (401, 403) or "permission" in lowered or "api key" in lowered:
        return ModelAuthError(text, provider=provider, status=status)
    if status == 429 or "quota" in lowered or "rate limit" in lowered or "resource_exhausted" in lowered:
        return ModelRateLimitError(text, provider=provider, status=status)
    if status is not None and 500 <= status < 600:
        return ModelUnavailableError(text, provider=provider, status=status)
    if "unavailable" in lowered or "overloaded" in lowered:
        return ModelUnavailableError(text, provider=provider, status=status)
    if status is not None and 400 <= status < 500:
        return ModelBadRequestError(text, provider=provider, status=status)
    return ModelProviderError(text, provider=provider, status=status)


class GeminiProvider:
    """``google-genai`` uzerinden Gemini erisimi.

    ``client`` disaridan verilebilir; testler gercek SDK ve ag olmadan tam
    davranisi dogrulayabilir.
    """

    name = "gemini"

    def __init__(
        self,
        *,
        model: str = "gemini-3.7-flash",
        api_key: str = "",
        backend: str = "developer",
        project: str = "",
        location: str = "",
        timeout_s: float = 90.0,
        max_retries: int = 2,
        store_interactions: bool = False,
        client: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.model = model
        self.backend = (backend or "developer").lower()
        self.timeout_s = timeout_s
        self.max_retries = max(1, max_retries)
        self.store_interactions = store_interactions
        self._sleep = sleep
        self._client = client if client is not None else self._build_client(
            api_key=api_key, project=project, location=location
        )

    # --- Istemci kurulumu ---------------------------------------------------
    def _build_client(self, *, api_key: str, project: str, location: str) -> Any:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - bagimlilik yoksa
            raise ModelProviderError(
                "google-genai paketi kurulu degil. `pip install google-genai` "
                "veya `pip install .[gemini]` calistirin.",
                provider=self.name,
            ) from exc

        if self.backend == "vertex":
            if not (project and location):
                raise ModelBadRequestError(
                    "GEMINI_BACKEND=vertex icin GOOGLE_CLOUD_PROJECT ve "
                    "GOOGLE_CLOUD_LOCATION zorunludur.",
                    provider=self.name,
                )
            log.info("Gemini backend: vertex (project=%s, location=%s)", project, location)
            return genai.Client(vertexai=True, project=project, location=location)
        if not api_key:
            raise ModelAuthError(
                "GEMINI_API_KEY tanimli degil (GEMINI_BACKEND=developer).",
                provider=self.name,
            )
        log.info("Gemini backend: developer API")
        return genai.Client(api_key=api_key)

    # --- Istek donusumu -----------------------------------------------------
    def _config(self, request: ModelRequest) -> Any:
        from google.genai import types

        tools = []
        if request.functions:
            tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=f.name,
                            description=f.description,
                            parameters_json_schema=dict(f.parameters),
                        )
                        for f in request.functions
                    ]
                )
            ]

        kwargs: dict[str, Any] = {
            "system_instruction": request.system or None,
            "max_output_tokens": request.max_output_tokens,
            # Iki katmanli emniyet: SDK asla bir handler calistirmaz.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True, maximum_remote_calls=0
            ),
            "thinking_config": types.ThinkingConfig(
                thinking_level=_THINKING_LEVELS.get(request.thinking_level, "LOW")
            ),
        }
        if tools:
            kwargs["tools"] = tools
        # DIKKAT: temperature/top_p/top_k/candidate_count BILEREK yok.
        # Gemini 3 bunlari kaldirdi; gondermek istegi bozar.

        http_options = self._http_options(request)
        if http_options is not None:
            kwargs["http_options"] = http_options
        return types.GenerateContentConfig(**kwargs)

    def _http_options(self, request: ModelRequest) -> Any:
        from google.genai import types

        options: dict[str, Any] = {"timeout": int(request.timeout_s * 1000)}
        if not request.store:
            # Saglayici tarafinda kalici saklamayi kapatma istegi. Developer
            # API'de veri saklama hesap ayarlarina da baglidir; uretimde SAP
            # verisi icin Vertex backend'i onerilir (bkz. README).
            options["extra_body"] = {"store": False}
        return types.HttpOptions(**options)

    def _contents(self, messages: Sequence[ModelMessage]) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for message in messages:
            if message.role == "user":
                if message.text:
                    contents.append(
                        types.Content(role="user", parts=[types.Part(text=message.text)])
                    )
                continue
            if message.role == "assistant":
                parts: list[Any] = []
                if message.text:
                    parts.append(types.Part(text=message.text))
                for call in message.function_calls:
                    part = types.Part(
                        function_call=types.FunctionCall(
                            id=call.id or None, name=call.name, args=dict(call.arguments)
                        )
                    )
                    # Thought signature geri konur: Gemini 3 cok adimli
                    # function calling'de bunu bekler. Deger opaktir ve
                    # baska hicbir yere gitmez.
                    if call.provider_state:
                        part.thought_signature = call.provider_state
                    parts.append(part)
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue
            # role == "tool"
            parts = [
                types.Part(
                    function_response=types.FunctionResponse(
                        id=result.call_id or None,
                        name=result.name,
                        response=result.payload(),
                    )
                )
                for result in message.function_results
            ]
            if parts:
                contents.append(types.Content(role="user", parts=parts))
        return contents

    # --- Yanit donusumu -----------------------------------------------------
    def _parse(self, raw: Any) -> ModelResponse:
        candidates = getattr(raw, "candidates", None) or []
        text_parts: list[str] = []
        calls: list[FunctionCall] = []
        stop_raw = ""

        if candidates:
            candidate = candidates[0]
            stop_raw = str(getattr(candidate, "finish_reason", "") or "")
            content = getattr(candidate, "content", None)
            for part in (getattr(content, "parts", None) or []):
                # `thought=True` parcalari modelin ic muhakemesidir; kullaniciya
                # ve audit'e gitmez.
                if getattr(part, "thought", False):
                    continue
                call = getattr(part, "function_call", None)
                if call is not None:
                    calls.append(
                        FunctionCall(
                            id=str(getattr(call, "id", "") or "") or f"call-{len(calls) + 1}",
                            name=str(getattr(call, "name", "") or ""),
                            arguments=dict(getattr(call, "args", None) or {}),
                            provider_state=getattr(part, "thought_signature", None),
                        )
                    )
                    continue
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(str(text))

        usage_raw = getattr(raw, "usage_metadata", None)
        usage = TokenUsage(
            input_tokens=int(getattr(usage_raw, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage_raw, "candidates_token_count", 0) or 0),
            cached_input_tokens=int(getattr(usage_raw, "cached_content_token_count", 0) or 0),
            reasoning_tokens=int(getattr(usage_raw, "thoughts_token_count", 0) or 0),
        )

        stop: StopReason = "tool_use" if calls else _STOP_REASONS.get(
            stop_raw.upper().rsplit(".", 1)[-1], "end_turn"
        )
        return ModelResponse(
            text="\n".join(text_parts).strip(),
            function_calls=tuple(calls),
            usage=usage,
            stop_reason=stop,
            model=self.model,
            provider=self.name,
        )

    # --- Genel API ----------------------------------------------------------
    def generate(
        self, request: ModelRequest, *, on_text: Callable[[str], None] | None = None
    ) -> ModelResponse:
        """Tek istek/yanit. Tool YURUTMEZ; yalnizca oneri dondurur.

        Yeniden deneme yalnizca **saglayici cagrisini** kapsar. Bu noktada
        hicbir SAP tool'u calistirilmamistir; dolayisiyla retry bir SAP
        yazmasini tekrarlayamaz.

        ``request.stream=True`` ve ``on_text`` verildiginde metin parcalari
        uretildikce iletilir. Function call'lar **akis bittikten sonra**
        dondurulur: yarim bir cagriyi calistirmak, argumanlari eksik bir SAP
        yazmasi anlamina gelirdi.
        """
        contents = self._contents(request.messages)
        config = self._config(request)
        last: ModelProviderError | None = None
        streaming = request.stream and on_text is not None

        for attempt in range(1, self.max_retries + 1):
            try:
                if streaming:
                    raw = self._collect_stream(contents, config, on_text)
                else:
                    raw = self._client.models.generate_content(
                        model=self.model, contents=contents, config=config
                    )
            except Exception as exc:  # noqa: BLE001 - SDK hata tipleri degisken
                error = classify_gemini_error(exc, provider=self.name)
                last = error
                if not error.retryable or attempt >= self.max_retries:
                    raise error from exc
                delay = min(8.0, 2.0**attempt)
                log.warning(
                    "Gemini %s (deneme %d/%d); %.1f sn sonra tekrar",
                    error.kind, attempt, self.max_retries, delay,
                )
                self._sleep(delay)
                continue
            return raw if isinstance(raw, ModelResponse) else self._parse(raw)

        raise last or ModelProviderError("Gemini cagrisi tamamlanamadi.", provider=self.name)

    def _collect_stream(
        self, contents: list[Any], config: Any, on_text: Callable[[str], None]
    ) -> ModelResponse:
        """Akisi tuketip tek bir normallestirilmis yanit uretir.

        Metin parcalari uretildikce cagiriciya verilir; function call'lar
        biriktirilir ve **akis tamamlandiktan sonra** dondurulur.
        """
        texts: list[str] = []
        calls: list[FunctionCall] = []
        usage = TokenUsage()
        stop_raw = ""
        for chunk in self._client.models.generate_content_stream(
            model=self.model, contents=contents, config=config
        ):
            for candidate in getattr(chunk, "candidates", None) or []:
                stop_raw = str(getattr(candidate, "finish_reason", "") or "") or stop_raw
                content = getattr(candidate, "content", None)
                for part in (getattr(content, "parts", None) or []):
                    if getattr(part, "thought", False):
                        continue
                    call = getattr(part, "function_call", None)
                    if call is not None:
                        calls.append(
                            FunctionCall(
                                id=str(getattr(call, "id", "") or "")
                                or f"call-{len(calls) + 1}",
                                name=str(getattr(call, "name", "") or ""),
                                arguments=dict(getattr(call, "args", None) or {}),
                                provider_state=getattr(part, "thought_signature", None),
                            )
                        )
                        continue
                    text = getattr(part, "text", None)
                    if text:
                        texts.append(str(text))
                        on_text(str(text))
            usage_raw = getattr(chunk, "usage_metadata", None)
            if usage_raw is not None:
                usage = TokenUsage(
                    input_tokens=int(getattr(usage_raw, "prompt_token_count", 0) or 0),
                    output_tokens=int(getattr(usage_raw, "candidates_token_count", 0) or 0),
                    cached_input_tokens=int(
                        getattr(usage_raw, "cached_content_token_count", 0) or 0
                    ),
                    reasoning_tokens=int(getattr(usage_raw, "thoughts_token_count", 0) or 0),
                )
        stop: StopReason = "tool_use" if calls else _STOP_REASONS.get(
            stop_raw.upper().rsplit(".", 1)[-1], "end_turn"
        )
        return ModelResponse(
            text="".join(texts).strip(),
            function_calls=tuple(calls),
            usage=usage,
            stop_reason=stop,
            model=self.model,
            provider=self.name,
        )

    def describe(self) -> dict[str, Any]:
        """Health ciktisi icin **anahtar icermeyen** ozet."""
        return {
            "provider": self.name,
            "model": self.model,
            "backend": self.backend,
            "store_interactions": self.store_interactions,
            "max_retries": self.max_retries,
            "timeout_s": self.timeout_s,
        }

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):  # pragma: no cover - SDK surumune bagli
            closer()
