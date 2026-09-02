"""Bulgu defterindeki doğruluk regresyonları.

Üçü de aynı sınıfta hata: SAP'ta OLMAYAN bir veri, varmış gibi üretilip
sonuca yazılıyordu. Bu projenin satış argümanı "uydurmaz" olduğu için bu
testler sözleşmeyi sabitler.
"""

from __future__ import annotations

from robotics_agent.sap.models import GoodsReceipt, Vendor


# --- 1. İptal edilen mal kabulü iki kez düşülmemeli ------------------------
def _gr(movement: str, qty: float, *, cancelled: bool = False, reverses: str = "") -> GoodsReceipt:
    return GoodsReceipt(
        material_document="49" + movement,
        movement_type=movement,
        quantity=qty,
        po_id="4500001",
        po_item="00010",
        reversed=bool(reverses) or movement in {"102", "122", "162"},
        cancelled=cancelled,
        reverses_document=reverses,
    )


def test_iptal_edilen_mal_kabulu_iki_kez_dusulmez():
    """SAP iptalde iki satır bırakır: işaretlenmiş asıl + ters kayıt.

    Önceki sürüm ikisini de `reversed` sayıp eksi yazıyordu; kısmen teslim
    edilmiş bir sipariş "hiç teslim edilmedi" görünüyordu.
    """
    rows = [
        _gr("101", 10),                              # canlı mal kabulü
        _gr("101", 5, cancelled=True),               # iptal EDİLMİŞ asıl
        _gr("102", 5, reverses="49101"),             # onu iptal eden ters kayıt
    ]
    assert sum(r.signed_quantity for r in rows) == 10.0


def test_ters_kayit_eksi_asil_arti_isaret_alir():
    assert _gr("101", 7).signed_quantity == 7.0
    assert _gr("102", 7).signed_quantity == -7.0
    assert _gr("122", 7).signed_quantity == -7.0
    # `ReversedMaterialDocument` doluysa hareket tipi ne olursa olsun ters kayıt.
    assert _gr("101", 7, reverses="4900001").signed_quantity == -7.0


def test_iptal_bayragi_ters_kayit_demek_degildir():
    """İptal edilmiş asıl satır hâlâ artı katkı verir; onu ters kayıt götürür."""
    asil = _gr("101", 5, cancelled=True)
    assert asil.cancelled is True
    assert asil.is_reversal is False
    assert asil.signed_quantity == 5.0


# --- 2. Ölçülmemiş tedarikçi performansı uydurulmamalı --------------------
def test_olculmemis_tedarikci_skoru_uretilmez():
    """Eksik kriteri sıfır sayıp skor üretmek, ölçülmemişi kötü gösterirdi."""
    v = Vendor(vendor_id="V1", name="Ölçülmemiş")
    assert v.score() is None
    assert "on_time_delivery_pct" in v.unmeasured_fields
    assert "quality_ppm" in v.unmeasured_fields


def test_tam_olculmus_tedarikci_skoru_uretilir():
    v = Vendor(
        vendor_id="V2",
        name="Ölçülmüş",
        on_time_delivery_pct=95.0,
        quality_ppm=200,
        price_competitiveness=80.0,
        responsiveness=70.0,
    )
    assert v.unmeasured_fields == []
    assert v.score() == 88.5


def test_bloke_tedarikci_olcumsuz_da_olsa_sifir_doner():
    """Bloke, ölçüm boşluğu değil bir SAP gerçeğidir; skoru sıfırdır."""
    assert Vendor(vendor_id="V3", name="Bloke", blocked=True).score() == 0.0


def test_sifir_ppm_ile_olcumsuz_ppm_ayni_sey_degildir():
    olculmus = Vendor(vendor_id="A", name="A", on_time_delivery_pct=100.0, quality_ppm=0,
                      price_competitiveness=50.0, responsiveness=50.0)
    olculmemis = Vendor(vendor_id="B", name="B")
    assert olculmus.quality_ppm == 0
    assert olculmemis.quality_ppm is None
    assert olculmus.score() is not None
    assert olculmemis.score() is None
