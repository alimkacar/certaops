"""Bagimsiz SAP okumalarinin paralel calistirilmasi.

Olcum: tur suresi neredeyse tamamen ardisik ag gidis-donusu. Bu modul
`gather_reads`in davranis sozlesmesini sabitler - hiz kazanci ancak
siranin, istisnanin ve tekil cagri yolunun bozulmamasiyla degerlidir.
"""

from __future__ import annotations

import threading
import time

import pytest

from robotics_agent.adapters.sap.concurrency import gather_named, gather_reads


def test_sonuc_sirasi_girdi_sirasindadir():
    """En yavas okuma ilk sirada olsa bile sonuc indeksi kaymaz."""
    gecikmeler = [0.05, 0.03, 0.01, 0.0]

    def oku(i: int):
        return lambda: (time.sleep(gecikmeler[i]), i)[1]

    assert gather_reads([oku(i) for i in range(4)], max_workers=4) == [0, 1, 2, 3]


def test_paralel_calisma_gercekten_es_zamanli():
    bariyer = threading.Barrier(3, timeout=5)

    def bekle():
        # Ucu de ayni anda burada bulusmazsa Barrier zaman asimina ugrar;
        # yani bu test sirali bir uygulamada GECMEZ.
        bariyer.wait()
        return True

    assert gather_reads([bekle, bekle, bekle], max_workers=3) == [True, True, True]


def test_tek_okuma_havuz_kurmaz():
    """Tek cagri icin thread havuzu kurmak kazanctan pahaliya gelir."""
    gorulen: set[str] = set()

    def kaydet():
        gorulen.add(threading.current_thread().name)
        return 1

    assert gather_reads([kaydet], max_workers=4) == [1]
    assert gorulen == {threading.current_thread().name}


def test_max_workers_bir_ise_sirayla_calisir():
    gorulen: set[str] = set()

    def kaydet():
        gorulen.add(threading.current_thread().name)
        return 1

    gather_reads([kaydet, kaydet, kaydet], max_workers=1)
    assert gorulen == {threading.current_thread().name}


def test_istisna_yutulmaz():
    """Yarim bir okuma sessizce bos sonuc gibi gorunmemeli."""

    def patla():
        raise ValueError("servis kapali")

    with pytest.raises(ValueError, match="servis kapali"):
        gather_reads([lambda: 1, patla, lambda: 2], max_workers=3)


def test_bos_liste_havuz_kurmadan_doner():
    assert gather_reads([]) == []


def test_gather_named_anahtarlari_korur():
    out = gather_named(
        {"stok": lambda: "s", "fiyat": lambda: "f", "kaynak": lambda: "k"},
        max_workers=3,
    )
    assert out == {"stok": "s", "fiyat": "f", "kaynak": "k"}


def test_sap_cagri_sayaci_paralel_okumada_eksik_saymaz():
    """`call_count` artisi kilitli olmali.

    Thread'ler HTTP beklerken GIL'i birakir; kilitsiz bir `+= 1` orada
    araya girebilir ve audit'teki `sap_calls` gercegin altinda kalir -
    yani butce ve telemetri sessizce yanlis olur.
    """
    import dataclasses

    from robotics_agent.adapters.sap.http import ODataHttpCore

    alanlar = {f.name for f in dataclasses.fields(ODataHttpCore)}
    assert "_count_lock" in alanlar, "call_count kilitsiz artiriliyor"


# --- Süreç genelinde $metadata önbelleği -----------------------------------
def test_metadata_onbellegi_ornekler_arasinda_paylasilir():
    """`$metadata` sistemin şemasıdır; her yeni oturum bunu baştan indirmemeli.

    `SAPAgentRuntime.__init__` her örnek için `build_backend()` çağırır ve
    runtime'lar oturum başına önbelleklenir. Örnek başına tutulan önbellek,
    her yeni oturumun ilk cevabına `$metadata` indirme + XML ayrıştırma
    maliyetini ekliyordu.
    """
    from robotics_agent.sap import schema_cache

    schema_cache.clear()
    cagri_sayisi = {"n": 0}

    def _uret():
        cagri_sayisi["n"] += 1
        return {"entity_sets": ("A_Product",)}

    anahtar = ("https://s4.test", "100", "metadata", "/sap/opu/odata/x")
    ilk = schema_cache.cached(anahtar, _uret)
    ikinci = schema_cache.cached(anahtar, _uret)

    assert ilk is ikinci, "ikinci okuma onbellekten gelmedi"
    assert cagri_sayisi["n"] == 1, "sema iki kez indirildi"


def test_farkli_sistemler_semayi_paylasmaz():
    """Host/client anahtarın parçasıdır: iki ayrı SAP sistemi karışmamalı."""
    from robotics_agent.sap import schema_cache

    schema_cache.clear()
    a = schema_cache.cached(("https://a.test", "100", "metadata", "/x"), lambda: "A")
    b = schema_cache.cached(("https://b.test", "100", "metadata", "/x"), lambda: "B")
    assert (a, b) == ("A", "B")


def test_reset_backend_semayi_dusurur():
    """Bağlantı değiştiğinde eski sistemin şeması taşınmamalı."""
    from robotics_agent.sap import reset_backend, schema_cache

    schema_cache.clear()
    schema_cache.cached(("https://a.test", "100", "metadata", "/x"), lambda: "A")
    assert schema_cache.size() == 1
    reset_backend()
    assert schema_cache.size() == 0


def test_sema_onbellegi_es_zamanli_erisimde_tek_deger_dondurur():
    """Yarışta kaybeden thread de aynı nesneyi almalı."""
    from robotics_agent.sap import schema_cache

    schema_cache.clear()
    anahtar = ("https://s4.test", "100", "metadata", "/yaris")
    sonuclar = gather_reads(
        [lambda: schema_cache.cached(anahtar, lambda: object()) for _ in range(8)],
        max_workers=8,
    )
    assert len({id(r) for r in sonuclar}) == 1, "farkli thread'ler farkli nesne aldi"
