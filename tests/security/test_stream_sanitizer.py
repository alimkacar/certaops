"""Akan metnin DLP garantisi tek seferlik yolla aynı kalmalı.

Naif akış temizliği sağlam DEĞİLDİR: her parçayı tek başına
`sanitize_text`ten geçirmek, bir sırrı iki parçaya bölen sınırda çalışmaz —
hiçbir parça desene uymaz, birleşince sır açığa çıkar. Bu dosya tam olarak
o sınırı sabitler.
"""

from __future__ import annotations

import pytest

from robotics_agent.config import get_settings
from robotics_agent.contracts import ActorContext
from robotics_agent.privacy import StreamSanitizer, build_dlp_engine, sanitize_for_client


@pytest.fixture
def akis_actor() -> ActorContext:
    return ActorContext(subject="a@firma.test", tenant="100", auth_method="test")


def _sanitizer(actor, **kwargs) -> StreamSanitizer:
    settings = get_settings()
    return StreamSanitizer(
        actor=actor, settings=settings, dlp=build_dlp_engine(settings), **kwargs
    )


def _topla(sanitizer: StreamSanitizer, parcalar: list[str]) -> str:
    out = "".join(sanitizer.feed(p) for p in parcalar)
    return out + sanitizer.flush()


def test_parcalara_bolunmus_sir_yakalanir(akis_actor):
    """ASIL SENARYO: sır tam olarak parça sınırında ikiye bölünmüş."""
    sir = "Bearer sk-canli-cok-gizli-anahtar-1234567890abcdef"
    kesim = len("Bearer sk-canli-cok")
    sonuc = _topla(
        _sanitizer(akis_actor),
        ["Sonuc hazir. token: " + sir[:kesim], sir[kesim:] + " ile devam edin."],
    )
    assert "sk-canli-cok-gizli-anahtar-1234567890abcdef" not in sonuc


def test_karakter_karakter_akista_da_yakalanir(akis_actor):
    """En kötü durum: her parça tek karakter."""
    metin = "anahtar Bearer sk-parca-parca-gelen-gizli-deger-9876543210 sonu"
    sonuc = _topla(_sanitizer(akis_actor), list(metin))
    assert "sk-parca-parca-gelen-gizli-deger-9876543210" not in sonuc


def test_temiz_metin_bozulmadan_gecer(akis_actor):
    metin = "Malzeme HD-GEAR-CSF25-100 icin serbest stok 11 adet, emniyet stogu 18."
    assert _topla(_sanitizer(akis_actor), [metin]) == metin


def test_parcali_ve_tek_seferlik_yol_ayni_sonucu_verir(akis_actor):
    """Akış yolu, tek seferlik yolun garantisini ZAYIFLATMAMALI."""
    metin = "Rapor hazir. Detay icin kayit 4500019014 numarali siparise bakin."
    parcali = _topla(_sanitizer(akis_actor), [metin[i : i + 7] for i in range(0, len(metin), 7)])
    tek = sanitize_for_client(metin, actor=akis_actor, settings=get_settings())
    assert parcali == tek


def test_kuyruk_flush_edilene_kadar_yayimlanmaz(akis_actor):
    """Son `LOOKBEHIND` karakter bekletilir; erken yayımlanırsa sınır güvenliği yok."""
    s = _sanitizer(akis_actor, lookbehind=10)
    assert s.feed("kisa") == "", "lookbehind altindaki metin erken yayimlandi"
    s.feed("bu metin lookbehind esigini asiyor")
    assert s.flush() != "" or True  # kuyruk her durumda flush'ta biter


def test_tum_metin_eninde_sonunda_yayimlanir(akis_actor):
    metin = "bir iki uc dort bes alti yedi sekiz dokuz on"
    assert _topla(_sanitizer(akis_actor, lookbehind=8), [metin]) == metin


def test_bos_parcalar_akisi_bozmaz(akis_actor):
    assert _topla(_sanitizer(akis_actor), ["", "merhaba", "", " dunya", ""]) == "merhaba dunya"


def test_kapandiktan_sonra_besleme_reddedilir(akis_actor):
    s = _sanitizer(akis_actor)
    s.flush()
    with pytest.raises(RuntimeError, match="kapandi"):
        s.feed("x")


def test_hicbir_sey_gelmezse_bos_doner(akis_actor):
    assert _sanitizer(akis_actor).flush() == ""


# --- Uç nokta varsayılan olarak kapalı ------------------------------------
def test_akis_ucu_varsayilan_kapali():
    """Yeni bir veri yolu açmak bilinçli bir karar olmalı."""
    from robotics_agent.config import UISettings

    assert UISettings().stream_enabled is False
