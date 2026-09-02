"""Modeli atlayan dogrudan yanit yolunun davranis testleri.

Test edilen sey "metin guzel mi" degil, **sinirlar**:
  - kisayol tetiklendiginde LLM'e HIC istek gitmemeli,
  - allowlist disindaki hicbir tool modeli atlayamamali,
  - belirsiz/karmasik sorularda normal LLM akisina dusulmeli,
  - policy reddi veya hata dogrudan yanit olarak sunulmamali.
"""

from __future__ import annotations

import pytest

from robotics_agent.core.direct import (
    DIRECT_ANSWER_TOOLS,
    direct_answer_for,
    match_shortcut,
    shortcut_catalogue,
)


class ModelForbidden:
    """Cagrilirsa testi dusuren saglayici.

    Kisayol yolunun **hicbir** saglayici istegi uretmedigini kanitlar:
    SAP verisi surecin disina cikmaz.
    """

    name = "forbidden"
    model = "none"

    def generate(self, _request):
        raise AssertionError(
            "Model cagrildi: dogrudan yanit yolu calismadi, veri saglayiciya cikti."
        )

    def close(self):
        return None


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DIRECT_ANSWERS", "true")

    from certaops.runtime import SAPAgentRuntime
    from robotics_agent.config import get_settings

    return SAPAgentRuntime(get_settings(reload=True), provider=ModelForbidden())


# --- Kisayol: model hic cagrilmaz -------------------------------------------
@pytest.mark.parametrize(
    "message",
    [
        "HD-GEAR-CSF25-100 stok durumu",
        "stok: HD-GEAR-CSF25-100",
        "malzeme bilgisi HD-GEAR-CSF25-100",
        "4500019014 numarali siparisin faturasi kesildi mi?",
        "baglanti durumu",
        "sap yetenekler",
        "SAP bağlantısı sağlıklı mı?",
        "SAP sisteminin desteklediği servisleri ve yetenekleri listele.",
        "HD-GEAR-CSF25-100 numaralı malzemenin detaylarını göster.",
        "4500019014 numaralı siparişin durumu",
        "TG ile başlayan malzemeleri ara.",
        "1. HD-GEAR-CSF25-100 numaralı malzemenin stok durumunu göster.",
        "5105600118 numaralı tedarikçi faturasının durumunu göster.",
    ],
)
def test_kisayol_modeli_hic_cagirmaz(agent, message):
    turn = agent.chat(message)
    assert turn.direct_answer is True
    assert turn.direct_answer_reason == "shortcut"
    assert turn.model_calls == 0, "SAP verisi LLM API'sine gitmemeliydi"
    assert turn.text.strip()
    assert turn.input_tokens == 0 and turn.output_tokens == 0


def test_kisayol_tool_cagrisini_audit_eder(agent):
    turn = agent.chat("baglanti durumu")
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "sap_connection_health"
    assert turn.tool_calls[0].is_error is False


@pytest.mark.parametrize(
    ("message", "tool", "arguments"),
    [
        ("SAP bağlantısı sağlıklı mı?", "sap_connection_health", {}),
        (
            "SAP sisteminin desteklediği servisleri ve yetenekleri listele.",
            "sap_discover_capabilities",
            {},
        ),
        (
            "21 numaralı malzemenin detaylarını göster.",
            "sap_material_360",
            {"material_id": "21"},
        ),
        (
            "4500000012 numaralı siparişin durumu",
            "sap_purchase_order_360",
            {"po_id": "4500000012"},
        ),
        (
            "TG ile başlayan malzemeleri ara.",
            "sap_search_materials",
            {"query": "TG"},
        ),
        (
            "1. 21 numaralı malzemenin stok durumunu göster.",
            "sap_stock_overview",
            {"material_ids": ["21"]},
        ),
        (
            "5100000001 numaralı tedarikçi faturasının durumunu göster.",
            "sap_supplier_invoice_status",
            {"invoice_id": "5100000001"},
        ),
    ],
)
def test_canli_test_sorulari_modele_dusmeden_eslenir(message, tool, arguments):
    match = match_shortcut(message)

    assert match is not None
    assert match.tool == tool
    assert match.arguments == arguments


def test_fatura_kisayolu_dogrudan_evet_hayir_cevabi_verir(agent):
    turn = agent.chat("4500019014 numaralı siparişin faturası kesildi mi?")

    assert turn.direct_answer is True
    assert turn.model_calls == 0
    assert turn.tool_calls[0].name == "sap_supplier_invoice_status"
    assert "Evet" in turn.text


def test_genel_siparis_listesi_model_sentezini_erken_kesmez():
    payload = {
        "order_count": 1,
        "orders": [{"po_id": "4500001", "vendor": "V1"}],
        "open_value_by_currency": {"EUR": 100},
    }

    assert (
        direct_answer_for(
            "sap_track_purchase_orders", payload, reason="self_contained"
        )
        is None
    )
    assert (
        direct_answer_for("sap_track_purchase_orders", payload, reason="shortcut")
        is not None
    )


def test_kisayol_gecmise_yazilir(agent):
    """Sonraki turda model baglami gormeli; kisayol gecmisi bosluga birakmaz."""
    agent.chat("baglanti durumu")
    assert len(agent.messages) == 2
    assert agent.messages[0].role == "user"
    assert agent.messages[1].role == "assistant"


# --- Fail-open: belirsizlikte model devreye girer ---------------------------
@pytest.mark.parametrize(
    "message",
    [
        "stok durumunu getir ve alternatif tedarikci oner",
        "HD-GEAR-CSF25-100 icin talep olustur",
        "merhaba",
        "bu malzemenin stogu nedir? ayrica projeye etkisini de yaz",
    ],
)
def test_belirsiz_soru_modele_duser(agent, message):
    with pytest.raises(AssertionError, match="Model cagrildi"):
        agent.chat(message)


def test_kisayol_kapatilinca_modele_duser(agent, monkeypatch):
    from robotics_agent.config import get_settings

    monkeypatch.setenv("AGENT_DIRECT_ANSWERS", "false")
    agent.settings = get_settings(reload=True)
    with pytest.raises(AssertionError, match="Model cagrildi"):
        agent.chat("baglanti durumu")


def test_cok_uzun_mesaj_kisayol_saymaz():
    assert match_shortcut("stok: " + "A" * 200) is None


def test_kismi_eslesme_kisayol_saymaz():
    """`fullmatch` sarti: mesajin bir parcasi eslesirse kisayol kullanilmaz."""
    assert match_shortcut("once stok: MAT-1 sonra siparis ver") is None


# --- Allowlist bir guvenlik kontroludur -------------------------------------
def test_allowlist_disindaki_tool_modeli_atlayamaz():
    assert direct_answer_for("sap_pr_submit", {"requisition_id": "1"}, reason="x") is None
    assert direct_answer_for("bilinmeyen_tool", {"a": 1}, reason="x") is None


def test_mutating_tool_allowliste_eklenemez():
    from robotics_agent.contracts import RiskTier
    from robotics_agent.tools.registry import tool

    DIRECT_ANSWER_TOOLS["gecici_yazma_toolu"] = DIRECT_ANSWER_TOOLS["sap_stock_overview"]
    try:
        with pytest.raises(ValueError, match="DIRECT_ANSWER_TOOLS"):

            @tool(
                "gecici_yazma_toolu",
                "test",
                {"type": "object", "properties": {}},
                risk_tier=RiskTier.R3,
                required_scopes=("sap.pr.write",),
                approval_policy="threshold",
                idempotent=True,
            )
            def _handler(ctx):  # pragma: no cover - kayit basarisiz olmali
                return {}
    finally:
        DIRECT_ANSWER_TOOLS.pop("gecici_yazma_toolu", None)


def test_hatali_sonuc_dogrudan_donmez():
    assert direct_answer_for("sap_stock_overview", {"error": "yok"}, reason="x") is None
    assert (
        direct_answer_for("sap_stock_overview", {"needs_review": True}, reason="x") is None
    )
    assert (
        direct_answer_for(
            "sap_stock_overview", {"denial_code": "MISSING_SCOPE"}, reason="x"
        )
        is None
    )


def test_eksik_stok_kesin_kisayolda_yerel_cevaplanir():
    """Kesin durum sorusu alternatif istemiyorsa model gerekmez."""
    payload = {
        "materials": [
            {
                "material_id": "M1",
                "description": "Test",
                "plant": "1000",
                "unrestricted": 0,
                "reserved": 0,
                "unreserved": 0,
            }
        ],
        "shortage_count": 1,
        "shortages": [{"material_id": "M1", "missing": 5}],
    }
    assert direct_answer_for("sap_stock_overview", payload, reason="shortcut") is not None
    assert direct_answer_for("sap_stock_overview", payload, reason="self_contained") is None


def test_kisayol_tool_hatasi_modele_dusmez(agent, monkeypatch):
    import certaops.runtime.agent as runtime_agent

    monkeypatch.setattr(
        runtime_agent,
        "execute_tool",
        lambda _name, _arguments, _ctx: (
            '{"error":"SAP gecici olarak yanit vermedi",'
            '"denial_code":"SAP_UNAVAILABLE","remediation":"Daha sonra deneyin."}',
            True,
        ),
    )

    turn = agent.chat("SAP baglantisi saglikli mi?")

    assert turn.direct_answer is True
    assert turn.direct_answer_reason == "shortcut_error"
    assert turn.model_calls == 0
    assert turn.tool_calls[0].is_error is True
    assert "SAP_UNAVAILABLE" in turn.text


def test_bos_sonuc_dogrudan_donmez():
    assert direct_answer_for("sap_stock_overview", {"materials": []}, reason="x") is None


def test_renderer_patlarsa_modele_duser(monkeypatch):
    """Renderer istisnasi kullaniciya sizmaz, sessizce LLM akisina duser."""
    from robotics_agent.core.direct import DirectAnswerSpec

    def boom(_payload):
        raise RuntimeError("render patladi")

    monkeypatch.setitem(
        DIRECT_ANSWER_TOOLS, "sap_connection_health", DirectAnswerSpec(render=boom)
    )
    assert direct_answer_for("sap_connection_health", {"sap": {}}, reason="x") is None


def test_kisayol_katalogu_tum_tool_lari_allowlistte():
    for entry in shortcut_catalogue():
        assert entry["tool"] in DIRECT_ANSWER_TOOLS
