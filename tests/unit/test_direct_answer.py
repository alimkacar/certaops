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
        "baglanti durumu",
        "sap yetenekler",
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


def test_eksik_stok_modele_birakilir():
    """Eksik varsa aksiyon onerisi gerekir; bicimlendirme yetmez."""
    payload = {
        "materials": [{"material_id": "M1"}],
        "shortage_count": 1,
        "shortages": [{"material_id": "M1", "missing": 5}],
    }
    assert direct_answer_for("sap_stock_overview", payload, reason="x") is None


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
