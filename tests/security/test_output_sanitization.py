"""Cikti temizleme sozlesmesi (OWASP LLM02 / LLM05).

Basari yolunda DLP zaten calisiyordu. Bu testler **hata yolunu** ve **model
cevabini** kapsar; ikisi de eskiden filtresizdi:

  - LLM02 Sensitive Information Disclosure: hassas veri prompt'tan VE ciktidan
    temizlenmeli. Hata govdesi de bir ciktidir.
  - LLM05 Improper Output Handling: model bir kullanici gibi gorulmeli
    (zero-trust); cevabi dogrulanmadan asagi akise verilmemeli.

Bu dosyadaki her test, duzeltmeden ONCE kirmizi idi.
"""

from __future__ import annotations

import json
import logging

import pytest

from robotics_agent.adapters.sap import SAPError
from robotics_agent.contracts import ActorContext
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools
from robotics_agent.tools.registry import REGISTRY

# Gercek bir SAP hata govdesinde bulunabilecek degerler.
IBAN = "TR330006100519786457841326"
EPOSTA = "muhasebe@acme-robotik.com"
VKN = "1234567890"
HASSAS = (
    f"Tedarikci V-100 (iletisim: {EPOSTA}, IBAN {IBAN}) icin odeme blokaji var. "
    f"Vergi no: {VKN} (VKN). Fatura 5105551 tutari 1.190,00 EUR."
)
GIZLI = ("IBAN", IBAN, EPOSTA, VKN)


@pytest.fixture(autouse=True)
def _tools():
    load_all_tools()


@pytest.fixture
def ctx(settings, purchaser) -> ToolContext:
    return ToolContext(settings=settings, actor=purchaser)


def _patch_handler(name: str, handler):
    """Bir tool'un handler'ini gecici olarak degistirir."""
    import dataclasses

    base = REGISTRY[name]
    REGISTRY[name] = dataclasses.replace(base, handler=handler)
    return base


def _restore(name: str, base) -> None:
    REGISTRY[name] = base


def _run_failing(ctx: ToolContext, exc: Exception) -> str:
    def handler(ctx, **kw):
        raise exc

    base = _patch_handler("sap_supplier_score_360", handler)
    try:
        payload, is_error = execute_tool("sap_supplier_score_360", {"vendor_ids": ["V-100"]}, ctx)
    finally:
        _restore("sap_supplier_score_360", base)
    assert is_error
    return payload


def _leaks(text: str) -> list[str]:
    return [needle for needle in (IBAN, EPOSTA, VKN) if needle in text]


# ---------------------------------------------------------------------------
# LLM02 - hata govdesi modele ham gitmemeli
# ---------------------------------------------------------------------------
def test_sap_hatasi_modele_ham_gitmez(ctx):
    payload = _run_failing(ctx, SAPError(HASSAS, code="ME_SUPPLIER_BLOCKED"))
    assert _leaks(payload) == [], f"hassas deger DLP'siz modele ulasti: {_leaks(payload)}"


def test_sap_hatasi_yine_de_kullanilabilir_kalir(ctx):
    """Temizleme, hatayi ise yaramaz hale getirmemeli.

    Model neyin yanlis gittigini anlayabilmeli: hata kodu ve is baglami
    korunur, yalniz hassas degerler maskelenir.
    """
    payload = _run_failing(ctx, SAPError(HASSAS, code="ME_SUPPLIER_BLOCKED"))
    body = json.loads(payload)
    assert body["sap_code"] == "ME_SUPPLIER_BLOCKED"
    assert "odeme blokaji" in body["error"], "is baglami korunmali"
    assert "5105551" in body["error"], "belge numarasi (D1) maskelenmemeli"


def test_beklenmeyen_istisna_de_temizlenir(ctx):
    """`except Exception` yolu da ayni sozlesmeye tabi."""
    payload = _run_failing(ctx, ValueError(HASSAS))
    assert _leaks(payload) == []


def test_hata_merkezi_loga_ham_yazilmaz(ctx, caplog):
    """field_policy: 'Merkezi log hicbir kosulda ticari/kisisel veri almaz'."""
    with caplog.at_level(logging.WARNING, logger="robotics_agent.tools.registry"):
        _run_failing(ctx, SAPError(HASSAS, code="ME_SUPPLIER_BLOCKED"))
    kayit = "\n".join(r.getMessage() for r in caplog.records)
    assert _leaks(kayit) == [], f"loga ham sizinti: {_leaks(kayit)}"


def test_hata_denetim_defterine_ham_yazilmaz(ctx, settings):
    """Denetim defteri uzun saklamalidir; KVKK silme talebini imkansiz kilmamali."""
    _run_failing(ctx, SAPError(HASSAS, code="ME_SUPPLIER_BLOCKED"))
    entries = ctx.audit.recent(limit=20)
    dump = json.dumps(
        [e.to_dict() if hasattr(e, "to_dict") else e for e in entries],
        ensure_ascii=False,
        default=str,
    )
    assert _leaks(dump) == [], f"audit'e ham sizinti: {_leaks(dump)}"


def test_basari_yolu_bozulmadi(ctx):
    """Regresyon: mevcut DLP davranisi aynen korunmali."""

    def handler(ctx, **kw):
        return {
            "vendor_id": "V-100",
            "supplier_email": EPOSTA,
            "supplier_iban": IBAN,
            "overall_score": 82,
        }

    base = _patch_handler("sap_supplier_score_360", handler)
    try:
        payload, is_error = execute_tool("sap_supplier_score_360", {"vendor_ids": ["V-100"]}, ctx)
    finally:
        _restore("sap_supplier_score_360", base)

    assert not is_error
    assert _leaks(payload) == []
    body = json.loads(payload)
    assert body["vendor_id"] == "V-100", "is verisi korunmali"
    assert body["overall_score"] == 82


# ---------------------------------------------------------------------------
# LLM05 - model cevabi zero-trust ile ele alinmali
# ---------------------------------------------------------------------------
def test_model_cevabi_istemciye_verilmeden_once_temizlenir(settings, purchaser):
    """Model ciktisi bir kullanici girdisi gibi gorulmeli (zero-trust).

    Model normalde D3 gormez ama prompt injection veya model hatasi sonucu
    cevabina hassas veri yazabilir. Son kapi burasidir.

    Dikkat: `client` sink'i `model` sink'inden **bilerek daha gevsektir**.
    Veri minimizasyonu modele uygulanir (fiyati hesaplamak icin e-postaya
    ihtiyaci yok); yetkili insan operatore uygulanmaz. Bu yuzden burada
    beklenen sey "her sey maskelensin" degil, "D3 gecmesin"dir.
    """
    from robotics_agent.privacy import sanitize_for_client

    temiz = sanitize_for_client(
        f"Tedarikci IBAN'i {IBAN}, vergi no {VKN} (VKN).",
        actor=purchaser,
        settings=settings,
    )
    assert IBAN not in temiz, "D3 banka bilgisi istemciye ham gitmemeli"
    assert VKN not in temiz, "D3 vergi kimligi istemciye ham gitmemeli"
    assert "Tedarikci" in temiz, "is metni korunmali"


def test_yetkisiz_actor_icin_d3_tokenlestirilir(settings, purchaser):
    """PURCHASER `sap.data.restricted` tasimaz -> D3 tokenlestirilir."""
    from robotics_agent.contracts import SCOPE_DATA_RESTRICTED
    from robotics_agent.privacy import sanitize_for_client

    assert not purchaser.has_scope(SCOPE_DATA_RESTRICTED), "test on kosulu"
    temiz = sanitize_for_client(f"IBAN {IBAN}", actor=purchaser, settings=settings)
    assert IBAN not in temiz


def test_kapsamsiz_rol_d2_kisisel_veriyi_goremez(settings):
    """VIEWER `sap.data.confidential` tasimaz -> e-posta maskelenir.

    D2 kapisinin gercekten actor kapsamina bakip bakmadigini kanitlar.
    """
    from robotics_agent.contracts import SCOPE_DATA_CONFIDENTIAL
    from robotics_agent.privacy import sanitize_for_client

    viewer = ActorContext(
        subject="gozlemci@firma.test",
        tenant="100",
        roles=("VIEWER",),
        auth_method="test",
    )
    assert not viewer.has_scope(SCOPE_DATA_CONFIDENTIAL), "test on kosulu"
    temiz = sanitize_for_client(f"iletisim {EPOSTA}", actor=viewer, settings=settings)
    assert EPOSTA not in temiz


def test_yetkili_rol_d2_kisisel_veriyi_gorebilir(settings, purchaser):
    """Ters yon: temizleme, yetkili kullaniciyi korlestirmemeli.

    Asiri kisitlama da bir hatadir - alici tedarikciyle iletisim kuramaz.
    """
    from robotics_agent.privacy import sanitize_for_client

    temiz = sanitize_for_client(f"iletisim {EPOSTA}", actor=purchaser, settings=settings)
    assert EPOSTA in temiz


def test_temizleme_normal_metni_bozmaz(settings, purchaser):
    """Yanlis pozitif kontrolu: is metni degismeden gecmeli."""
    from robotics_agent.privacy import sanitize_for_client

    metin = (
        "Satinalma talebi 0010004711 olusturuldu. Malzeme R-1000, 10 adet, "
        "tesis 1100, teslim 2026-09-01. Toplam 175.000,00 EUR."
    )
    assert sanitize_for_client(metin, actor=purchaser, settings=settings) == metin


def test_api_yaniti_temizlenmis_cevabi_dondurur():
    """api.py gercekten bu kapiyi kullaniyor mu (kod yolu bagli mi)?"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/robotics_agent/channels/api.py"
    metin = src.read_text(encoding="utf-8")
    assert "sanitize_for_client" in metin, "api.py cevabi temizlemeden donduruyor"
    assert "reply=turn.text," not in metin, "ham model metni hala dondurulUYOR"
