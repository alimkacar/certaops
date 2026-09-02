"""Bulgu defteri: sorgunun SAP'a doğru gitmesiyle ilgili regresyonlar.

Ortak sınıf: **filtre SAP'a gönderilmiyor, sonuç Python'da eleniyor.** Bu,
performans sorunu gibi görünüp aslında bir doğruluk sorunudur — sayfa
penceresinin dışında kalan kayıtlar sessizce yok sayılır ve çağıran taraf
bu listeden sayı/ortalama hesaplar.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robotics_agent.adapters.sap.errors import SAPError, SAPFault
from robotics_agent.sap.models import MaterialClassification


# --- 1. Tedarikçi filtresi SAP'a gönderilmeli ------------------------------
class _FakeV4:
    """`read_collection` çağrılarını kaydeden minimal V4 istemcisi."""

    def __init__(self, rows=None, *, reject_filter: str = "") -> None:
        self.rows = rows or []
        self.reject_filter = reject_filter
        self.calls: list[dict] = []

    def read_collection(self, service, entity_set, **kwargs):
        self.calls.append({"entity_set": entity_set, **kwargs})
        expr = kwargs.get("filter_expr", "")
        if self.reject_filter and self.reject_filter in expr:
            raise SAPError(
                "Property 'Supplier' is not filterable",
                code="400",
                fault=SAPFault(
                    http_status=400, code="400", message="not filterable", target_api="po"
                ),
            )

        class _Page:
            pass

        page = _Page()
        page.rows = self.rows
        return page


def _po_row(po_id: str, supplier: str) -> dict:
    return {
        "PurchaseOrder": po_id,
        "PurchaseOrderItem": "00010",
        "Material": "M1",
        "OrderQuantity": 10,
        "NetPriceAmount": 100,
        "DocumentCurrency": "EUR",
        "IsCompletelyDelivered": False,
        "_PurchaseOrder": {"Supplier": supplier, "SupplierName": supplier},
        "_PurchaseOrderScheduleLineTP": [],
        "_PurOrdAccountAssignment": [],
    }


@pytest.fixture
def odata_backend(settings):
    """Ağ kurmadan `ODataBackend` örneği (yalnız saf metotlar sınanır)."""
    from robotics_agent.sap.odata import ODataSAPBackend

    return ODataSAPBackend.__new__(ODataSAPBackend)


def test_tedarikci_filtresi_odata_sorgusuna_giriyor(odata_backend, settings):
    """Eskiden `$top=limit` çekilip Python'da eleniyordu: pencerenin dışındaki
    siparişler "yok" sayılıyor, `open_order_count` yanlış çıkıyordu."""
    fake = _FakeV4([_po_row("4500001", "0010002")])
    odata_backend.v4 = fake
    odata_backend.settings = settings

    with patch.object(type(odata_backend), "_alias_for", lambda self, a: "purchase_order"):
        odata_backend.get_purchase_orders(vendor_id="0010002", limit=50)

    expr = fake.calls[0]["filter_expr"]
    assert "_PurchaseOrder/Supplier eq '0010002'" in expr, (
        f"tedarikçi filtresi SAP'a gitmedi: {expr!r}"
    )


def test_sunucu_filtreyi_reddederse_pencere_genisletilir(odata_backend, settings):
    """Nav-yolu filtresi kapalıysa geri düşülür ama kırpma riski azaltılır."""
    fake = _FakeV4([_po_row("4500001", "0010002")], reject_filter="Supplier")
    odata_backend.v4 = fake
    odata_backend.settings = settings

    with patch.object(type(odata_backend), "_alias_for", lambda self, a: "purchase_order"):
        odata_backend.get_purchase_orders(vendor_id="0010002", limit=50)

    assert len(fake.calls) == 2, "geri düşme denenmedi"
    assert "Supplier" not in fake.calls[1]["filter_expr"]
    assert fake.calls[1]["max_pages"] > 1, "eleme istemciye düştüyse pencere büyümeli"


def test_yetki_hatasi_filtresiz_tekrar_denenmez(odata_backend, settings):
    """403'ü "filtre desteklenmiyor" sayıp filtresiz okumak, kullanıcıyı
    görmemesi gereken satırlara yaklaştırırdı."""
    from robotics_agent.sap.odata import _is_filter_rejection

    yetki = SAPError(
        "Not authorized",
        code="403",
        fault=SAPFault(http_status=403, code="403", message="no authorization", target_api="po"),
    )
    assert _is_filter_rejection(yetki) is False


# --- 2. Karakteristik adı büyük/küçük harfe takılmamalı --------------------
def test_karakteristik_adi_buyuk_kucuk_harf_duyarsiz():
    """SAP `REACH_MM` yazar, model `reach_mm` gönderir. Tam eşleşme arayan
    eski hâli `None` dönüyor, sonuç "hiçbir malzeme uymadı" gibi görünüyordu."""
    c = MaterialClassification(
        material_id="M1", characteristics={"REACH_MM": "1800", "PAYLOAD_KG": 12}
    )
    assert c.numeric("reach_mm") == 1800.0
    assert c.numeric("Reach_MM") == 1800.0
    assert c.numeric("reach mm") == 1800.0
    assert c.numeric("payload-kg") == 12.0


def test_tam_eslesme_hala_oncelikli():
    """Normalleştirme yalnız bir yedek yoldur; birebir eşleşme bozulmaz."""
    c = MaterialClassification(material_id="M1", characteristics={"REACH_MM": 1800})
    assert c.numeric("REACH_MM") == 1800.0


def test_olmayan_karakteristik_none_doner():
    c = MaterialClassification(material_id="M1", characteristics={"REACH_MM": 1800})
    assert c.numeric("agirlik") is None


def test_sayisal_olmayan_deger_none_doner():
    c = MaterialClassification(material_id="M1", characteristics={"RENK": "mavi"})
    assert c.numeric("renk") is None


# --- 3. WBS hiyerarşisi: eşitlik değil, ata/alt eleman kuralı --------------
def test_proje_kodu_alt_elemanlari_kapsar():
    """Sipariş hesap ataması YAPRAK elemana yapılır; kullanıcı proje kodunu
    sorar. Eşitlik kullanan eski hâli "bu projede açık sipariş yok" diyordu."""
    from robotics_agent.sap.base import wbs_matches

    assert wbs_matches("R-2026-021", "R-2026-021-1")
    assert wbs_matches("R-2026-021", "R-2026-021-2")
    assert wbs_matches("R-2026-021", "R-2026-021")


def test_benzer_proje_kodu_ata_sayilmaz():
    """Düz `startswith` yanlış olurdu: `R-2026-02` ayrı bir proje kodudur."""
    from robotics_agent.sap.base import wbs_matches

    assert not wbs_matches("R-2026-02", "R-2026-021-1")
    assert not wbs_matches("R-2026-0", "R-2026-021")


def test_wbs_eslesmesi_ayrac_ve_buyuk_harf_toleransli():
    from robotics_agent.sap.base import wbs_matches

    assert wbs_matches("r-2026-021", "R-2026-021-1")
    assert wbs_matches("P.100", "P.100.1")
    assert wbs_matches("P/100", "P/100/1")
    assert not wbs_matches("", "R-2026-021")
    assert not wbs_matches("R-2026-021", "")


def test_mock_backend_proje_kodunu_hiyerarsik_eslestirir(buyer_ctx):
    """Uçtan uca: mock veride yalnız yaprak WBS'ler var."""
    parent = buyer_ctx.sap.get_purchase_orders(wbs_element="R-2026-014")
    leaf = buyer_ctx.sap.get_purchase_orders(wbs_element="R-2026-014-1")

    assert parent, "proje kodu hiçbir siparişi getirmedi (hiyerarşi çalışmıyor)"
    assert len(parent) >= len(leaf)
    assert all(po.wbs_element and po.wbs_element.startswith("R-2026-014") for po in parent)


# --- 4. Boş malzeme listesi olumlu teminata dönüşmemeli --------------------
def test_bos_malzeme_listesi_stokta_var_demez(buyer_ctx, run_tool):
    """Sıfır veri, olumlu cevap değildir: "tüm kalemler stoktan karşılanabilir"
    hiç kontrol edilmemiş bir kapsam için verilen bir teminattı."""
    result = run_tool("sap_stock_overview", buyer_ctx, expect_error=True, material_ids=[])
    assert result["denial_code"] == "INPUT_EMPTY"
    assert "karsilanabilir" not in str(result).lower()


def test_bulunamayan_malzemeler_degerlendirme_disinda_isaretlenir(buyer_ctx, run_tool):
    result = run_tool("sap_stock_overview", buyer_ctx, material_ids=["YOK-1", "YOK-2"])
    assert result["evaluated_count"] == 0
    assert sorted(result["not_found"]) == ["YOK-1", "YOK-2"]
    assert "cikarim yapilamaz" in result["recommendation"]


# --- 5. Karışık para birimi tek toplama indirilmemeli ---------------------
def test_karisik_para_birimi_tek_toplam_uretmez(buyer_ctx, run_tool, monkeypatch):
    """EUR + USD siparişleri toplayıp sonuca sistem varsayılanını etiket
    olarak basmak, kur dönüşümü olmadan üretilmiş yanlış bir rakamdı."""
    from robotics_agent.sap.models import PurchaseOrder

    def fake_orders(**kwargs):
        return [
            PurchaseOrder(
                po_id="45001",
                vendor_id="V1",
                currency="EUR",
                net_value=1000.0,
                material_id="M1",
                quantity=10,
                delivered_qty=0,
                status="open",
            ),
            PurchaseOrder(
                po_id="45002",
                vendor_id="V2",
                currency="USD",
                net_value=500.0,
                material_id="M2",
                quantity=5,
                delivered_qty=0,
                status="open",
            ),
        ]

    monkeypatch.setattr(buyer_ctx.sap, "get_purchase_orders", fake_orders)
    result = run_tool("sap_track_purchase_orders", buyer_ctx, vendor_id="KARISIK")

    assert result["mixed_currency"] is True
    # Serileştirme `None` alanları düşürdüğü için tek toplam hiç yayınlanmaz;
    # korunan şey "yanlış bir tek rakam verilmemesi".
    assert "total_open_value" not in result, "kur dönüşümü olmadan tek toplam verilmemeli"
    assert "currency" not in result, "karışık para biriminde tek etiket basılmamalı"
    assert result["open_value_by_currency"] == {"EUR": 1000.0, "USD": 500.0}
    assert "kur donusumu yapilmadi" in result["note"].lower()


def test_tek_para_biriminde_duz_alanlar_korunur(buyer_ctx, run_tool, monkeypatch):
    """Düzeltme yaygın hâli bozmamalı; etiket artık GERÇEK para birimi."""
    from robotics_agent.sap.models import PurchaseOrder

    monkeypatch.setattr(
        buyer_ctx.sap,
        "get_purchase_orders",
        lambda **kw: [
            PurchaseOrder(
                po_id="45001",
                vendor_id="V1",
                currency="SEK",
                net_value=800.0,
                material_id="M1",
                quantity=8,
                delivered_qty=0,
                status="open",
            )
        ],
    )
    result = run_tool("sap_track_purchase_orders", buyer_ctx, vendor_id="TEKPARA")

    assert result["currency"] == "SEK", "etiket sistem varsayılanı değil gerçek para birimi olmalı"
    assert result["total_open_value"] == 800.0
    assert result.get("mixed_currency") is None
