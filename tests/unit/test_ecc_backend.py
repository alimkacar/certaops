"""ECC backend'inin GERCEK davranis testleri.

Bu testler `httpx.MockTransport` kullanir: HTTP cekirdegi, CSRF akisi, V2
`d`/`results` acilimi, $filter uretimi ve tum mapping kodu **gercek koddur**.
Sahte olan tek sey agin kendisidir. Yani "import oluyor" degil, "dogru sorguyu
uretiyor ve donen veriyi dogru yorumluyor" dogrulanir.

Kritik olanlar:
  - N+1 regresyon koruma: coklu malzeme TEK cagride okunmali.
  - Filtre literalleri: Edm.Decimal `m` soneki, Edm.DateTime `datetime'...'`.
  - prepare_* HICBIR kosulda yazmamali.
  - Bilinmeyen deger 0.0'a dusmemeli; `estimated_fields` ile isaretlenmeli.
  - Belge akisinda her bag gercek SAP alanindan gelmeli.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from robotics_agent.adapters.sap import SAPError
from robotics_agent.sap.ecc import ECCSAPBackend, reference_token
from robotics_agent.sap.models import PurchaseRequisitionItem

BASE = "https://ecc.test"


# ---------------------------------------------------------------------------
# Sahte ECC
# ---------------------------------------------------------------------------
class FakeECC:
    """Kayit tutan sahte Gateway. Rota anahtari: entity set adi."""

    def __init__(self, routes: dict[str, list[dict]] | None = None) -> None:
        self.routes: dict[str, list[dict]] = routes or {}
        self.requests: list[httpx.Request] = []
        self.created: list[dict] = []
        self.create_response: dict = {}

    # --- yardimcilar ---
    def calls_to(self, entity_set: str) -> list[httpx.Request]:
        return [r for r in self.requests if entity_set in r.url.path]

    def filter_of(self, entity_set: str, index: int = 0) -> str:
        return self.calls_to(entity_set)[index].url.params.get("$filter", "")

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method in {"POST", "PUT", "PATCH", "DELETE"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if request.method == "HEAD" or "$metadata" in path:
            return httpx.Response(200, headers={"x-csrf-token": "tok"}, text="<edmx/>")

        if request.method == "POST":
            self.created.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                201,
                json={"d": self.create_response},
                headers={"Content-Type": "application/json"},
            )

        entity_set = path.rstrip("/").rsplit("/", 1)[-1]
        rows = self.routes.get(entity_set, [])
        return httpx.Response(
            200,
            json={"d": {"results": rows}},
            headers={"Content-Type": "application/json"},
        )


def build_backend(settings_factory, tmp_path, routes=None, **create) -> tuple[ECCSAPBackend, FakeECC]:
    settings = settings_factory(
        tmp_path,
        **{
            "sap.backend": "ecc",
            "sap.base_url": BASE,
            "sap.auth_mode": "basic",
            "sap.username": "svc",
            "sap.password": "pw",
            "sap.odata_version": "v2",
            "sap.plant": "1100",
            "sap.purch_org": "1000",
            "sap.purch_group": "R01",
            "sap.company_code": "1000",
            "sap.currency": "EUR",
            "sap.read_only": False,
            "security.allowed_sap_hosts": ("ecc.test",),
        },
    )
    fake = FakeECC(routes)
    if create:
        fake.create_response = create
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(fake.handler))

    import robotics_agent.sap.ecc as ecc_mod

    original = ecc_mod.build_http_client
    ecc_mod.build_http_client = lambda connection, cfg: client  # noqa: ARG005
    try:
        backend = ECCSAPBackend(settings)
    finally:
        ecc_mod.build_http_client = original
    return backend, fake


@pytest.fixture
def material_row() -> dict:
    return {
        "Material": "R-1000",
        "Plant": "1100",
        "MaterialDescription": "Robot kolu 6 eksen",
        "MaterialType": "HALB",
        "MaterialGroup": "ROB01",
        "BaseUnit": "ST",
        "GrossWeight": "128.5",
        "ProcurementType": "F",
        "PlannedDeliveryDays": "21",
        "MinimumLotSize": "2",
        "MRPController": "101",
        "ABCIndicator": "A",
        "MovingAveragePrice": "18500.00",
        "PriceUnit": "1",
        "Currency": "EUR",
    }


# ---------------------------------------------------------------------------
# Malzeme ana verisi
# ---------------------------------------------------------------------------
def test_search_materials_tek_cagri_yapar_ve_aciklamada_arar(
    settings_factory, tmp_path, material_row
):
    """N+1 regresyon korumasi: S/4 adapteri 3 cagri yapiyor, ECC 1 yapmali."""
    backend, fake = build_backend(settings_factory, tmp_path, {"MaterialSet": [material_row]})

    results = backend.search_materials("robot kolu", limit=10)

    assert len(fake.calls_to("MaterialSet")) == 1, "coklu cagri = N+1 regresyonu"
    flt = fake.filter_of("MaterialSet")
    assert "substringof('robot',MaterialDescription)" in flt
    assert "substringof('robot',Material)" in flt, "yalniz aciklamada aramak yetmez"
    assert "Plant eq '1100'" in flt

    material = results[0]
    assert material.material_id == "R-1000"
    assert material.description == "Robot kolu 6 eksen"
    assert material.material_type == "HALB"
    assert material.gross_weight_kg == 128.5
    assert material.planned_delivery_days == 21
    # MBEW join'i sayesinde ayri degerleme cagrisi gerekmez.
    assert material.moving_avg_price == 18500.0
    assert not fake.calls_to("MaterialValuationSet")


def test_search_materials_tirnak_iceren_sorguyu_kacisla_gonderir(
    settings_factory, tmp_path, material_row
):
    """OData injection korumasi: tek tirnak ikilenmeli."""
    backend, fake = build_backend(settings_factory, tmp_path, {"MaterialSet": [material_row]})
    backend.search_materials("O'Brien")
    assert "O''Brien" in fake.filter_of("MaterialSet")


def test_karakteristik_filtresi_tek_cagride_okur_ve_sinifsizi_eler(
    settings_factory, tmp_path, material_row
):
    """Siniflandirmasi okunamayan malzeme ELENMELI: bilinmiyor != uyuyor."""
    ikinci = dict(material_row, Material="R-2000", MaterialDescription="Robot kolu 4 eksen")
    backend, fake = build_backend(
        settings_factory,
        tmp_path,
        {
            "MaterialSet": [material_row, ikinci],
            # Yalniz R-1000'in karakteristigi var.
            "MaterialCharcValueSet": [
                {
                    "Material": "R-1000",
                    "ClassType": "001",
                    "Characteristic": "PAYLOAD_KG",
                    "CharcValue": "12",
                },
                {
                    "Material": "R-1000",
                    "ClassType": "001",
                    "Characteristic": "REACH_MM",
                    "CharcValue": "1800",
                },
            ],
        },
    )

    results = backend.search_materials("robot", attribute_filters={"payload_kg": (10.0, 20.0)})

    assert len(fake.calls_to("MaterialCharcValueSet")) == 1, "malzeme basina cagri = N+1"
    assert [m.material_id for m in results] == ["R-1000"]
    assert results[0].attributes["payload_kg"] == 12
    # Aralik disi kalsa da sinifsiz kalan elenmis olmali.
    disarida = backend.search_materials("robot", attribute_filters={"payload_kg": (50.0, 90.0)})
    assert disarida == []


# ---------------------------------------------------------------------------
# Stok
# ---------------------------------------------------------------------------
def test_stok_coklu_malzemeyi_tek_cagride_okur_ve_dogru_toplar(settings_factory, tmp_path):
    """Depo yerleri toplanir; emniyet stogu tesis bazli oldugu icin TOPLANMAZ."""
    backend, fake = build_backend(
        settings_factory,
        tmp_path,
        {
            "StockSet": [
                {
                    "Material": "R-1000", "Plant": "1100", "StorageLocation": "0001",
                    "UnrestrictedQuantity": "10", "QualityInspectionQuantity": "2",
                    "BlockedQuantity": "1", "SafetyStock": "5", "BaseUnit": "ST",
                },
                {
                    "Material": "R-1000", "Plant": "1100", "StorageLocation": "0002",
                    "UnrestrictedQuantity": "4", "QualityInspectionQuantity": "0",
                    "BlockedQuantity": "0", "SafetyStock": "5", "BaseUnit": "ST",
                },
            ],
            "PurchaseOrderItemSet": [
                {"Material": "R-1000", "Quantity": "20", "DeliveredQuantity": "8"},
            ],
            "SupplyDemandSet": [
                {"Material": "R-1000", "MRPElement": "AR", "Quantity": "-3"},
                {"Material": "R-1000", "MRPElement": "BE", "Quantity": "20"},
            ],
        },
    )

    levels = backend.get_stock(["R-1000", "R-2000"], plant="1100")

    assert len(fake.calls_to("StockSet")) == 1, "malzeme basina dongu = N+1 regresyonu"
    assert "R-1000" in fake.filter_of("StockSet") and "R-2000" in fake.filter_of("StockSet")

    r1000 = next(level for level in levels if level.material_id == "R-1000")
    assert r1000.unrestricted_qty == 14.0        # 10 + 4
    assert r1000.quality_inspection_qty == 2.0
    assert r1000.safety_stock == 5.0             # max, toplam (10) DEGIL
    assert r1000.on_order_qty == 12.0            # 20 siparis - 8 teslim
    assert r1000.reserved_qty == 3.0             # yalniz talep elementi (AR)
    assert r1000.unreserved_qty == 11.0

    # Verisi olmayan malzeme yine de sonucta olmali (sessizce dusmemeli).
    assert any(level.material_id == "R-2000" for level in levels)


def test_rezervasyon_okunamazsa_sifira_zorlanmaz(settings_factory, tmp_path):
    """MD04 yoksa reserved_qty 0.0 kalir ama bu 'rezervasyon yok' iddiasi degildir.

    Onemli olan: kod patlamaz ve stok fotografini yine dondurur.
    """
    backend, _ = build_backend(
        settings_factory,
        tmp_path,
        {
            "StockSet": [
                {"Material": "R-1000", "Plant": "1100", "StorageLocation": "0001",
                 "UnrestrictedQuantity": "9", "SafetyStock": "0", "BaseUnit": "ST"}
            ],
        },
    )
    levels = backend.get_stock(["R-1000"])
    assert levels[0].unrestricted_qty == 9.0
    assert levels[0].reserved_qty == 0.0


# ---------------------------------------------------------------------------
# ATP
# ---------------------------------------------------------------------------
def test_eksik_skor_alanlari_sifir_degil_estimated_isaretlenir(settings_factory, tmp_path):
    """ECC'de standart olmayan alanlar 0 dondurulurse yanlis karar uretir."""
    backend, _ = build_backend(
        settings_factory,
        tmp_path,
        {
            "SupplierScoreSet": [
                {"Supplier": "V-100", "PurchasingOrganization": "1000",
                 "OverallScore": "82", "PriceScore": "75", "QualityScore": "90",
                 "DeliveryScore": "80", "ServiceScore": "85",
                 "EvaluationPeriod": "2026-Q2"}
            ]
        },
    )
    score = backend.get_supplier_score("V-100")

    assert score.overall_score == 82.0
    assert score.on_time_delivery_pct is None, "bilinmeyen deger None olmali, 0.0 degil"
    assert score.quality_ppm is None
    assert set(score.estimated_fields) == {
        "on_time_delivery_pct", "quality_ppm", "quantity_score"
    }
    assert score.has_real_data is False


# ---------------------------------------------------------------------------
# Satinalma talebi
# ---------------------------------------------------------------------------
def _pr_routes(material_row: dict) -> dict:
    return {
        "MaterialSet": [material_row],
        "InfoRecordSet": [
            {
                "InfoRecord": "530001", "Material": "R-1000", "Supplier": "V-100",
                "SupplierName": "Acme Robotik", "PurchasingOrganization": "1000",
                "NetPrice": "17500", "Currency": "EUR", "PriceUnit": "1",
                "MinimumQuantity": "5", "PlannedDeliveryDays": "30",
                "Incoterms": "DAP", "PaymentTerms": "NT30",
            }
        ],
    }


def test_prepare_asla_yazmaz_ve_bulgulari_uretir(settings_factory, tmp_path, material_row):
    backend, fake = build_backend(settings_factory, tmp_path, _pr_routes(material_row))

    draft = backend.prepare_purchase_requisition(
        [
            PurchaseRequisitionItem(
                material_id="R-1000",
                quantity=2,                       # MOQ 5'in altinda
                delivery_date=date.today() + timedelta(days=3),  # 30 gunluk temin altinda
            )
        ],
        header_text="Hat 3 yenileme",
    )

    assert fake.writes == [], "prepare_* HICBIR kosulda yazmamali"
    alanlar = {f.field for f in draft.findings}
    assert "quantity" in alanlar            # MOQ ihlali
    assert "delivery_date" in alanlar       # temin suresi ihlali
    assert "account_assignment" in alanlar  # WBS/masraf merkezi yok
    assert draft.total_value == 35000.0     # 2 x 17500
    assert draft.items[0]["vendor_id"] == "V-100"
    assert draft.is_submittable, "uyarilar engelleyici degildir"


def test_submit_idempotency_anahtarini_govdeye_koyar(settings_factory, tmp_path, material_row):
    backend, fake = build_backend(
        settings_factory, tmp_path, _pr_routes(material_row),
        PurchaseRequisition="0010004711",
    )
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="R-1000", quantity=10, wbs_element="P-1.1")]
    )
    result = backend.submit_purchase_requisition(draft, external_reference="turn-42")

    assert len(fake.writes) == 1
    body = fake.created[0]
    assert body["IdempotencyKey"] == "turn-42"
    assert body["ToItems"][0]["Material"] == "R-1000"
    assert body["ToItems"][0]["AccountAssignmentCategory"] == "P", "WBS varsa proje atamasi"
    assert result.requisition_id == "0010004711"
    assert result.created is True
    assert result.etag.startswith("idempotency:"), "ECC'de ETag yok, mutabakat anahtari tasinir"


def test_submit_cakismada_yeni_belge_yaratmaz(settings_factory, tmp_path, material_row):
    """Ayni anahtar tekrar gonderilirse mevcut PR donmeli, created=False olmali."""
    backend, _ = build_backend(
        settings_factory, tmp_path, _pr_routes(material_row),
        PurchaseRequisition="0010004711", AlreadyExisted="X",
    )
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="R-1000", quantity=10, cost_center="CC10")]
    )
    result = backend.submit_purchase_requisition(draft, external_reference="turn-42")

    assert result.requisition_id == "0010004711"
    assert result.created is False
    assert "daha once" in result.messages[0]


def test_referans_aramasi_tam_esitlik_kullanir(settings_factory, tmp_path):
    """S/4 substring tarar; ECC'de bu imkansiz - tam esitlik olmali."""
    backend, fake = build_backend(
        settings_factory,
        tmp_path,
        {
            "IdempotencySet": [
                {"IdempotencyKey": "turn-42", "ObjectType": "PURCHASE_REQUISITION",
                 "ObjectId": "0010004711"}
            ],
            "PurchaseRequisitionSet": [
                {"PurchaseRequisition": "0010004711", "HeaderText": "Hat 3",
                 "ToItems": {"results": [{"Price": "100", "Quantity": "3"}]}}
            ],
        },
    )
    found = backend.find_purchase_requisition_by_reference("turn-42")

    assert found is not None
    pr_id, record = found
    assert pr_id == "0010004711"
    assert record["total_value"] == 300.0
    flt = fake.filter_of("IdempotencySet")
    assert "IdempotencyKey eq 'turn-42'" in flt
    assert "substringof" not in flt, "ECC'de baslik metni filtrelenemez"


def test_uzun_anahtar_64_karaktere_hashlenir():
    kisa = reference_token("turn-42")
    uzun = reference_token("x" * 200)
    assert kisa == "turn-42"
    assert len(uzun) == 64 and uzun != "x" * 64


# ---------------------------------------------------------------------------
# Satinalma siparisi ve belge akisi
# ---------------------------------------------------------------------------
def test_po_miktar_agirlikli_termin_ve_statu(settings_factory, tmp_path):
    erken, gec = date(2026, 9, 1), date(2026, 11, 1)
    backend, _ = build_backend(
        settings_factory,
        tmp_path,
        {
            "PurchaseOrderItemSet": [
                {
                    "PurchaseOrder": "4500001", "PurchaseOrderItem": "00010",
                    "Material": "R-1000", "Quantity": "10", "DeliveredQuantity": "4",
                    "InvoicedQuantity": "0", "NetPrice": "100", "NetValue": "1000",
                    "Currency": "EUR", "ItemText": "Robot kolu",
                    "ToHeader": {"results": [{"Supplier": "V-100", "SupplierName": "Acme",
                                              "CreationDate": "2026-08-01",
                                              "DocumentCurrency": "EUR"}]},
                    "ToScheduleLines": {"results": [
                        {"DeliveryDate": erken.isoformat(), "ScheduleQuantity": "9"},
                        {"DeliveryDate": gec.isoformat(), "ScheduleQuantity": "1"},
                    ]},
                }
            ]
        },
    )
    orders = backend.get_purchase_orders(material_id="R-1000")
    po = orders[0]

    assert po.status == "partially_delivered"
    assert po.open_qty == 6.0
    assert po.vendor_name == "Acme"
    # 9 birim erken + 1 birim gec -> agirlikli tarih erkene cok yakin olmali
    assert erken <= po.requested_delivery_date <= erken + timedelta(days=10)


def test_belge_akisi_ekbe_zincirini_gercek_alanlarla_kurar(settings_factory, tmp_path):
    backend, _ = build_backend(
        settings_factory,
        tmp_path,
        {
            "PurchaseOrderItemSet": [
                {
                    "PurchaseOrder": "4500001", "PurchaseOrderItem": "00010",
                    "PurchaseRequisition": "0010004711", "Quantity": "10",
                    "DeliveredQuantity": "10", "NetValue": "1000", "Currency": "EUR",
                    "ToHeader": {"results": [{"CreationDate": "2026-08-01"}]},
                }
            ],
            "PurchaseOrderHistorySet": [
                {"PurchaseOrder": "4500001", "HistoryCategory": "E",
                 "MaterialDocument": "5000001", "PostingDate": "2026-09-05",
                 "Quantity": "10", "MovementType": "101", "Amount": "1000",
                 "Currency": "EUR"},
                {"PurchaseOrder": "4500001", "HistoryCategory": "Q",
                 "MaterialDocument": "5105551", "PostingDate": "2026-09-20",
                 "Quantity": "10", "Amount": "1000", "Currency": "EUR"},
            ],
            "SupplierInvoiceSet": [
                {"SupplierInvoice": "5105551", "ClearingDate": "2026-10-15",
                 "GrossAmount": "1190", "Currency": "EUR",
                 "AccountingDocument": "1900001234"}
            ],
        },
    )
    nodes = backend.get_document_flow("4500001", document_type="purchase_order")
    by_type = {n.document_type: n for n in nodes}

    assert set(by_type) == {
        "purchase_requisition", "purchase_order", "goods_receipt",
        "supplier_invoice", "payment",
    }
    # Her bag gercek bir SAP alanindan gelmeli - tahmin yok.
    assert by_type["purchase_requisition"].linked_by == "EKPO-BANFN"
    assert by_type["goods_receipt"].linked_by == "EKBE-BELNR (BEWTP=E)"
    assert by_type["supplier_invoice"].linked_by == "EKBE-BELNR (BEWTP=Q)"
    assert by_type["payment"].linked_by == "BSEG-AUGBL (mahsup belgesi)"
    assert by_type["payment"].predecessor_id == "5105551"
    assert all(n.source_api for n in nodes), "her dugum kaynagini bildirmeli"


def test_mahsup_edilmemis_fatura_icin_odeme_dugumu_uretilmez(settings_factory, tmp_path):
    backend, _ = build_backend(
        settings_factory,
        tmp_path,
        {
            "PurchaseOrderItemSet": [
                {"PurchaseOrder": "4500001", "Quantity": "1", "NetValue": "10",
                 "ToHeader": {"results": [{}]}}
            ],
            "PurchaseOrderHistorySet": [
                {"PurchaseOrder": "4500001", "HistoryCategory": "Q",
                 "MaterialDocument": "5105551", "Quantity": "1"}
            ],
            # ClearingDate YOK -> henuz odenmemis
            "SupplierInvoiceSet": [{"SupplierInvoice": "5105551", "GrossAmount": "12"}],
        },
    )
    nodes = backend.get_document_flow("4500001", document_type="purchase_order")
    assert not any(n.document_type == "payment" for n in nodes)


def test_po_bagi_yoksa_akis_tahmin_uretmez(settings_factory, tmp_path):
    backend, _ = build_backend(settings_factory, tmp_path, {"PurchaseOrderItemSet": []})
    with pytest.raises(SAPError) as exc:
        backend.get_document_flow("0010004711", document_type="purchase_requisition")
    assert exc.value.code == "DOCFLOW_NO_PO_LINK"


def test_gecersiz_belge_turu_reddedilir(settings_factory, tmp_path):
    backend, _ = build_backend(settings_factory, tmp_path, {})
    with pytest.raises(SAPError) as exc:
        backend.get_document_flow("X", document_type="fatura")
    assert exc.value.code == "DOCFLOW_BAD_TYPE"


# ---------------------------------------------------------------------------
# Fatura ve is akisi
# ---------------------------------------------------------------------------
def test_fatura_blokaji_ve_sapma_hesaplanir(settings_factory, tmp_path):
    backend, _ = build_backend(
        settings_factory,
        tmp_path,
        {
            "SupplierInvoiceSet": [
                {
                    "SupplierInvoice": "5105551", "FiscalYear": "2026",
                    "Supplier": "V-100", "GrossAmount": "1190", "Currency": "EUR",
                    "PaymentBlock": "R", "PostingDate": "2026-09-20",
                    "ToItems": {"results": [{"PurchaseOrder": "4500001"}]},
                    "ToBlocks": {"results": [
                        {"InvoiceItem": "1", "BlockReason": "price", "ToleranceKey": "PP",
                         "ExpectedValue": "1000", "ActualValue": "1100",
                         "ToleranceLimitPercent": "5", "Currency": "EUR",
                         "PurchaseOrder": "4500001"}
                    ]},
                }
            ]
        },
    )
    invoices = backend.get_supplier_invoices(invoice_id="5105551")
    inv = invoices[0]

    assert inv.status == "blocked"
    assert inv.is_blocked
    assert inv.po_ids == ["4500001"]
    block = inv.blocks[0]
    assert block.block_reason == "price"
    assert block.tolerance_key == "PP"
    assert block.variance_abs == 100.0
    assert block.variance_pct == 10.0


def test_izinsiz_host_engellenir(settings_factory, tmp_path):
    """SAP_ALLOWED_HOSTS disina cikis engellenmeli (egress korumasi)."""
    from robotics_agent.adapters.sap import HostNotAllowed

    backend, _ = build_backend(settings_factory, tmp_path, {"MaterialSet": []})
    backend._core.allowed_hosts = ("baska.host",)
    with pytest.raises(HostNotAllowed):
        backend.search_materials("robot")


def test_capabilities_ecc_gercegini_raporlar(settings_factory, tmp_path):
    backend, _ = build_backend(settings_factory, tmp_path, {})
    caps = backend.capabilities()

    assert caps["backend"] == "ecc"
    assert caps["odata_preference"] == "v2"
    assert all(caps["supported"].values()), "ECC tum portlari uygulamali"
    assert len(caps["services"]) == 6
    assert "V4" in " ".join(caps["notes"])
    assert "V4" in backend.preferred_service_order()
