"""Gemini adaptorunun sozlesme testleri (canli anahtar gerektirmez).

SDK gercek, ag sahte: ``client`` disaridan verilir. Test edilenler
saglayiciya ozgu ve kritik olan seyler:

  * otomatik fonksiyon yurutme HER ZAMAN kapali,
  * Gemini 3'te kaldirilan orneklem parametreleri GONDERILMEZ,
  * function call/response esleme (call_id + isim) dogru,
  * thought signature korunur ama loglanmaz/audit'e yazilmaz,
  * Developer ve Vertex istemci yapilandirmalari,
  * hata siniflandirmasi (tekrar denenebilir mi).
"""

from __future__ import annotations

import pytest

pytest.importorskip("google.genai")

from certaops.providers.contracts import (  # noqa: E402
    FunctionCall,
    FunctionDeclaration,
    FunctionResult,
    ModelAuthError,
    ModelMessage,
    ModelRateLimitError,
    ModelRequest,
    ModelTimeoutError,
    ModelUnavailableError,
    redact_provider_state,
)
from certaops.providers.gemini import (  # noqa: E402
    DEPRECATED_SAMPLING_FIELDS,
    GeminiProvider,
    classify_gemini_error,
)


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)


def _response(parts, *, finish="STOP", usage=None):
    from google.genai import types

    candidate = types.Candidate(
        content=types.Content(role="model", parts=parts), finish_reason=finish
    )
    return types.GenerateContentResponse(
        candidates=[candidate],
        usage_metadata=usage
        or types.GenerateContentResponseUsageMetadata(
            prompt_token_count=120, candidates_token_count=40, thoughts_token_count=15
        ),
    )


def _provider(responses):
    client = FakeClient(responses)
    return GeminiProvider(model="gemini-3.7-flash", client=client, sleep=lambda _: None), client


REQUEST = ModelRequest(
    system="sistem talimati",
    messages=[ModelMessage(role="user", text="stok durumu")],
    functions=[
        FunctionDeclaration(
            name="sap_stock_overview",
            description="Stok",
            parameters={"type": "object", "properties": {"material_ids": {"type": "array"}}},
        )
    ],
)


# --- 5. Otomatik fonksiyon yurutme kapali -----------------------------------
def test_otomatik_fonksiyon_yurutme_her_zaman_kapali():
    """SDK bir SAP handler'ini dogrudan calistiramaz."""
    from google.genai import types

    provider, client = _provider([_response([types.Part(text="tamam")])])
    provider.generate(REQUEST)
    config = client.models.calls[0]["config"]
    assert config.automatic_function_calling.disable is True
    assert config.automatic_function_calling.maximum_remote_calls == 0


def test_sdk_ye_cagrilabilir_nesne_gonderilmez():
    """Tool'lar saf sema olarak gider; Python fonksiyonu ASLA verilmez."""
    from google.genai import types

    provider, client = _provider([_response([types.Part(text="ok")])])
    provider.generate(REQUEST)
    config = client.models.calls[0]["config"]
    for tool in config.tools or []:
        for declaration in tool.function_declarations or []:
            assert not callable(declaration)
            assert isinstance(declaration.name, str)


# --- 14. Deprecated orneklem parametreleri gonderilmez ----------------------
def test_gemini3_deprecated_sampling_parametreleri_gonderilmez():
    from google.genai import types

    provider, client = _provider([_response([types.Part(text="ok")])])
    provider.generate(REQUEST)
    config = client.models.calls[0]["config"]
    for field_name in DEPRECATED_SAMPLING_FIELDS:
        assert getattr(config, field_name, None) is None, f"{field_name} gonderilmemeli"


def test_thinking_level_gonderilir():
    from google.genai import types

    provider, client = _provider([_response([types.Part(text="ok")])])
    provider.generate(
        ModelRequest(system="s", messages=[ModelMessage(role="user", text="x")],
                     thinking_level="medium")
    )
    config = client.models.calls[0]["config"]
    assert str(config.thinking_config.thinking_level).endswith("MEDIUM")


# --- 13. call_id ve function name eslemesi ---------------------------------
def test_function_call_id_ve_ad_dogru_eslenir():
    from google.genai import types

    provider, client = _provider(
        [
            _response(
                [
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc-42", name="sap_stock_overview", args={"material_ids": ["M1"]}
                        )
                    )
                ],
                finish="STOP",
            )
        ]
    )
    response = provider.generate(REQUEST)
    assert response.stop_reason == "tool_use"
    assert len(response.function_calls) == 1
    call = response.function_calls[0]
    assert call.id == "fc-42"
    assert call.name == "sap_stock_overview"
    assert call.arguments == {"material_ids": ["M1"]}


def test_function_response_ayni_id_ve_adla_geri_gonderilir():
    from google.genai import types

    provider, client = _provider([_response([types.Part(text="ozet")])])
    provider.generate(
        ModelRequest(
            system="s",
            messages=[
                ModelMessage(role="user", text="stok"),
                ModelMessage(
                    role="assistant",
                    function_calls=(
                        FunctionCall(id="fc-42", name="sap_stock_overview", arguments={}),
                    ),
                ),
                ModelMessage(
                    role="tool",
                    function_results=(
                        FunctionResult(
                            call_id="fc-42", name="sap_stock_overview", content='{"ok":true}'
                        ),
                    ),
                ),
            ],
        )
    )
    contents = client.models.calls[0]["contents"]
    responses = [
        part.function_response
        for content in contents
        for part in (content.parts or [])
        if part.function_response is not None
    ]
    assert len(responses) == 1
    assert responses[0].id == "fc-42"
    assert responses[0].name == "sap_stock_overview"
    assert responses[0].response == {"ok": True}


# --- Thought signature: korunur, sizmaz -------------------------------------
def test_thought_signature_korunur_ama_audit_e_yazilmaz():
    from google.genai import types

    part = types.Part(
        function_call=types.FunctionCall(id="fc-1", name="sap_stock_overview", args={})
    )
    part.thought_signature = b"gizli-imza"
    provider, _ = _provider([_response([part])])
    response = provider.generate(REQUEST)
    call = response.function_calls[0]
    assert call.provider_state == b"gizli-imza"
    # Audit ve repr sizdirmaz.
    assert "gizli-imza" not in repr(call)
    assert "provider_state" not in call.to_audit_dict()
    assert "gizli-imza" not in str(response.to_audit_dict())


def test_thought_signature_sonraki_istege_geri_konur():
    from google.genai import types

    provider, client = _provider([_response([types.Part(text="ok")])])
    provider.generate(
        ModelRequest(
            system="s",
            messages=[
                ModelMessage(
                    role="assistant",
                    function_calls=(
                        FunctionCall(
                            id="fc-1", name="t", arguments={}, provider_state=b"imza"
                        ),
                    ),
                )
            ],
        )
    )
    contents = client.models.calls[0]["contents"]
    signatures = [
        part.thought_signature
        for content in contents
        for part in (content.parts or [])
        if getattr(part, "thought_signature", None)
    ]
    assert signatures == [b"imza"]


def test_thought_parcalari_metne_donusturulmez():
    from google.genai import types

    thought = types.Part(text="ic muhakeme")
    thought.thought = True
    provider, _ = _provider([_response([thought, types.Part(text="gorunur cevap")])])
    response = provider.generate(REQUEST)
    assert response.text == "gorunur cevap"
    assert "ic muhakeme" not in response.text


def test_redact_provider_state_ic_ice_maskeler():
    payload = {"a": 1, "thought_signature": "x", "n": [{"provider_state": "y"}]}
    cleaned = redact_provider_state(payload)
    assert cleaned["thought_signature"] == "<gizli>"
    assert cleaned["n"][0]["provider_state"] == "<gizli>"
    assert cleaned["a"] == 1


# --- Kullanim bilgisi --------------------------------------------------------
def test_token_kullanimi_eslenir():
    from google.genai import types

    provider, _ = _provider([_response([types.Part(text="ok")])])
    response = provider.generate(REQUEST)
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 40
    assert response.usage.reasoning_tokens == 15
    assert response.provider == "gemini"
    assert response.model == "gemini-3.7-flash"


# --- 15. Developer ve Vertex yapilandirmasi ---------------------------------
def test_developer_backend_api_key_ister():
    with pytest.raises(ModelAuthError):
        GeminiProvider(backend="developer", api_key="")


def test_vertex_backend_proje_ve_lokasyon_ister():
    from certaops.providers.contracts import ModelBadRequestError

    with pytest.raises(ModelBadRequestError):
        GeminiProvider(backend="vertex", project="", location="")


def test_vertex_backend_client_kurar(monkeypatch):
    created: dict = {}

    class StubGenai:
        @staticmethod
        def Client(**kwargs):  # noqa: N802 - SDK adini taklit eder
            created.update(kwargs)
            return FakeClient([])

    import sys

    module = type(sys)("google.genai_stub")
    module.Client = StubGenai.Client
    monkeypatch.setitem(sys.modules, "google.genai", module)
    monkeypatch.setattr("google.genai.Client", StubGenai.Client, raising=False)

    provider = GeminiProvider(backend="vertex", project="p1", location="eu-west4")
    assert provider.backend == "vertex"
    assert created.get("vertexai") is True
    assert created.get("project") == "p1"
    assert created.get("location") == "eu-west4"


def test_describe_api_anahtarini_gostermez():
    provider, _ = _provider([])
    described = provider.describe()
    assert "api_key" not in described
    assert set(described) >= {"provider", "model", "backend"}


# --- Hata siniflandirmasi ---------------------------------------------------
@pytest.mark.parametrize(
    ("exc", "expected", "retryable"),
    [
        (TimeoutError("deadline exceeded"), ModelTimeoutError, True),
        (RuntimeError("429 rate limit exceeded"), ModelRateLimitError, True),
        (RuntimeError("PERMISSION_DENIED: invalid api key"), ModelAuthError, False),
        (RuntimeError("503 service unavailable"), ModelUnavailableError, True),
    ],
)
def test_hata_siniflandirmasi(exc, expected, retryable):
    error = classify_gemini_error(exc)
    assert isinstance(error, expected)
    assert error.retryable is retryable


def test_tekrar_denenebilir_hata_yeniden_denenir():
    from google.genai import types

    provider, client = _provider(
        [RuntimeError("503 service unavailable"), _response([types.Part(text="ok")])]
    )
    response = provider.generate(REQUEST)
    assert response.text == "ok"
    assert len(client.models.calls) == 2


def test_kalici_hata_tekrar_denenmez():
    provider, client = _provider([RuntimeError("PERMISSION_DENIED: bad key")])
    with pytest.raises(ModelAuthError):
        provider.generate(REQUEST)
    assert len(client.models.calls) == 1


# --- Streaming (opsiyonel) ---------------------------------------------------
class StreamingModels(FakeModels):
    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.responses.pop(0))


def test_streaming_metni_parca_parca_iletir_function_call_i_sonda_verir():
    """Yarim argumanla SAP yazmasi calistirilamaz: call'lar akis bitince gelir."""
    from google.genai import types

    client = FakeClient([])
    client.models = StreamingModels(
        [
            [
                _response([types.Part(text="ilk ")]),
                _response([types.Part(text="parca")]),
                _response(
                    [
                        types.Part(
                            function_call=types.FunctionCall(
                                id="fc-9", name="sap_stock_overview", args={}
                            )
                        )
                    ]
                ),
            ]
        ]
    )
    provider = GeminiProvider(model="gemini-3.7-flash", client=client, sleep=lambda _: None)
    chunks: list[str] = []
    response = provider.generate(
        ModelRequest(
            system="s",
            messages=[ModelMessage(role="user", text="stok")],
            stream=True,
        ),
        on_text=chunks.append,
    )
    assert chunks == ["ilk ", "parca"]
    assert response.text == "ilk parca"
    assert [c.name for c in response.function_calls] == ["sap_stock_overview"]
    assert response.stop_reason == "tool_use"


def test_minimal_thinking_level_is_clamped_to_low():
    """Gemini 3.7 Flash `MINIMAL` seviyesini kaldirdi; istenirse API hata doner.

    Notr sozlesme `minimal`i tanimaya devam eder (baska saglayicilar
    destekleyebilir). Adaptor bunu sessizce `LOW`a kelepceler: yapilandirma
    gecerli kalir, istek gecersiz gitmez.
    """
    from certaops.providers.gemini import _THINKING_LEVELS

    assert _THINKING_LEVELS["minimal"] == "LOW"
    assert "MINIMAL" not in set(_THINKING_LEVELS.values())
