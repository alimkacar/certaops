"""Kanal ciktisindaki tool onizlemesi: yapisal alan ve maskeleme sozlesmesi.

`result_preview` bir operatore GOSTERILMEK uzere 600 karaktere kirpilmis
metindir. Kirpma JSON'u bozar, dolayisiyla arayuz ondan kart/tablo cizemez.
`result_json` ayni icerigi kirpmadan, yapisal olarak verir.

Buradaki testlerin isi, bu ek alanin **yeni bir veri yolu acmadigini**
dogrulamak: `result_preview` neyi gizliyorsa `result_json` de gizlemeli.
Aksi halde arayuze veri tasimak icin DLP'nin etrafindan dolasilmis olurdu.
"""

from __future__ import annotations

import importlib
import json

import pytest

from robotics_agent.channels.auth import hash_token

TOKEN = "tok-preview-secret"

# Gercek bir SAP kaydinda bulunabilecek, maskelenmesi gereken degerler.
IBAN = "TR330006100519786457841326"
EPOSTA = "muhasebe@acme-robotik.com"


@pytest.fixture
def principals_file(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "token_sha256": hash_token(TOKEN),
                        "subject": "operator@firma.test",
                        "tenant": "100",
                        "roles": ["AUDITOR"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _env(monkeypatch, tmp_path, principals_file, *, masking: bool = True):
    monkeypatch.setenv("AGENT_AUTH_MODE", "static_token")
    monkeypatch.setenv("AGENT_PRINCIPALS_FILE", str(principals_file))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("SAP_ALLOWED_HOSTS", "s4.firma.test")
    monkeypatch.setenv("AGENT_SESSION_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_DLP_MODE", "enforce")
    monkeypatch.setenv("AGENT_RISK_SCORING_MODE", "enforce")
    monkeypatch.setenv("AGENT_PSEUDONYMIZATION_KEY_ID", "kms://test-pseudonym-v1")
    monkeypatch.setenv("AGENT_KMS_KEY_ID", "kms://test-data-key-v1")
    monkeypatch.setenv("AGENT_D3_CACHE_ENABLED", "false")
    monkeypatch.setenv("AGENT_MASK_PREVIEWS", "true" if masking else "false")


def _api(monkeypatch, tmp_path, principals_file, *, masking: bool = True):
    _env(monkeypatch, tmp_path, principals_file, masking=masking)
    import robotics_agent.config as config_module

    config_module._settings = None  # noqa: SLF001 - test icin singleton sifirlama
    return importlib.reload(importlib.import_module("robotics_agent.channels.api"))


# --- Yapisal onizleme -------------------------------------------------------
def test_structured_preview_parses_a_json_result(monkeypatch, tmp_path, principals_file):
    """Tool sonucu sozluk olarak donmeli; arayuz kart cizebilmeli."""
    api = _api(monkeypatch, tmp_path, principals_file)
    body = api._structured_preview(  # noqa: SLF001
        json.dumps(
            {
                "material_id": "MAT-1001",
                "chain_complete": False,
                "stages": [{"stage": "Talep", "count": 1}],
            }
        )
    )
    assert body["material_id"] == "MAT-1001"
    assert body["chain_complete"] is False
    # Liste ve ic ice sozluk yapisi korunur: arayuz tabloyu bundan cizer.
    assert body["stages"] == [{"stage": "Talep", "count": 1}]


@pytest.mark.xfail(
    reason=(
        "BILINEN KUSUR: mask_text icindeki telefon deseni "
        "`(?<!\\d)(\\+?\\d[\\d\\s()-]{8,}\\d)(?!\\d)` ayraci olmayan 10 haneli "
        "sayilari da yakaliyor. SAP siparis (45xxxxxxxx) ve fatura (51xxxxxxxx) "
        "numaralari bu yuzden '***' oluyor; 8 haneli talep numaralari etkilenmiyor. "
        "Bu kusur `result_json`dan ONCE de vardi: `truncate_preview` ayni "
        "`mask_text`i cagirdigi icin mevcut /chat onizlemeleri de belge "
        "numaralarini maskeliyor. Duzeltme: telefon desenini en az bir ayrac "
        "(+, bosluk, parantez, tire) isteyecek sekilde daraltmak.",
    ),
    strict=True,
)
def test_sap_document_numbers_should_survive_masking(monkeypatch, tmp_path, principals_file):
    """Belge numarasi is anahtaridir; maskelenirse karar verilemez.

    Maskeleme modulunun kendi sozu: "Is verisi (fiyat, malzeme numarasi)
    maskelenmez; maskelenirse karar verilemez hale gelir." Belge numarasi da
    bu tanimin icindedir.
    """
    api = _api(monkeypatch, tmp_path, principals_file)
    body = api._structured_preview(json.dumps({"po_id": "4500000012"}))  # noqa: SLF001
    assert body["po_id"] == "4500000012"


def test_structured_preview_returns_none_for_non_json(monkeypatch, tmp_path, principals_file):
    """JSON olmayan sonucta arayuz `result_preview` metnine dusmeli."""
    api = _api(monkeypatch, tmp_path, principals_file)
    assert api._structured_preview("bu bir JSON degil") is None  # noqa: SLF001


def test_structured_preview_returns_none_for_a_json_list(monkeypatch, tmp_path, principals_file):
    """Sozluk olmayan gecerli JSON de yapisal sayilmaz."""
    api = _api(monkeypatch, tmp_path, principals_file)
    assert api._structured_preview('["a", "b"]') is None  # noqa: SLF001


# --- Maskeleme sozlesmesi ---------------------------------------------------
def test_structured_preview_masks_sensitive_values(monkeypatch, tmp_path, principals_file):
    """`result_preview` neyi maskeliyorsa `result_json` de maskelemeli.

    Bu testin duserse anlami sudur: arayuze yapisal veri tasimak icin
    maskeleme katmani atlanmis olur.
    """
    api = _api(monkeypatch, tmp_path, principals_file, masking=True)
    raw = json.dumps({"vendor_id": "V-100", "supplier_iban": IBAN, "contact": EPOSTA})

    body = api._structured_preview(raw)  # noqa: SLF001
    serialized = json.dumps(body, ensure_ascii=False)

    assert IBAN not in serialized
    assert EPOSTA not in serialized
    # Is anahtari maskelenmez: kart onsuz okunamaz.
    assert body["vendor_id"] == "V-100"


def test_secret_keys_are_hidden_entirely(monkeypatch, tmp_path, principals_file):
    """`token`/`password` gibi anahtarlar deger olarak hic gecmemeli."""
    api = _api(monkeypatch, tmp_path, principals_file, masking=True)
    body = api._structured_preview(  # noqa: SLF001
        json.dumps({"api_token": "sk-live-4471", "material_id": "MAT-1001"})
    )
    assert "sk-live-4471" not in json.dumps(body)
    assert body["material_id"] == "MAT-1001"


def test_masking_can_be_disabled_for_local_debugging(monkeypatch, tmp_path, principals_file):
    """AGENT_MASK_PREVIEWS=false ile ham deger gecer.

    Bu ayar `result_preview` icin de aynidir; iki alan ayni kapiyi kullanir.
    """
    api = _api(monkeypatch, tmp_path, principals_file, masking=False)
    body = api._structured_preview(json.dumps({"supplier_iban": IBAN}))  # noqa: SLF001
    assert body["supplier_iban"] == IBAN


# --- Kanal sozlesmesi -------------------------------------------------------
def test_tool_call_out_carries_both_shapes(monkeypatch, tmp_path, principals_file):
    """Kanal ciktisi hem metni hem yapiyi tasimali.

    Metin geriye donuk uyumluluk icin durur: mevcut istemciler onu okuyor.
    """
    api = _api(monkeypatch, tmp_path, principals_file)

    class _Call:
        name = "sap_document_flow"
        arguments = {"document_id": "4500000012"}
        result = json.dumps({"document_id": "4500000012", "chain_complete": False})
        is_error = False

    out = api._tool_call_out(_Call())  # noqa: SLF001
    assert out.name == "sap_document_flow"
    assert out.result_preview  # kirpilmis metin
    assert out.result_json["chain_complete"] is False  # yapisal


def test_tool_call_out_survives_a_non_json_result(monkeypatch, tmp_path, principals_file):
    """Yapisal alan cozulemedigi zaman istek 500 vermemeli."""
    api = _api(monkeypatch, tmp_path, principals_file)

    class _Call:
        name = "sap_generate_report"
        arguments = {}
        result = "yapisal olmayan cikti"
        is_error = False

    out = api._tool_call_out(_Call())  # noqa: SLF001
    assert out.result_json is None
    assert "yapisal olmayan cikti" in out.result_preview
