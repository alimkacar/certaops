"""S/4HANA OData backend'inin davranis testleri.

`sap/odata.py` 470 ifade ve 164 dal tasiyordu; kapsam **%0**'di. ECC backend'i
`httpx.MockTransport` ile dogrulanirken S/4 backend'i hic ayni muameleyi
gormemisti - filtre uretimi, V4 `$expand` ayristirma, agirlikli termin hesabi
ve PR gonderimi calisma zamaninda ilk kez sinaniyordu.

Sahte olan tek sey ag: HTTP cekirdegi, CSRF akisi, V2 `d`/`results` ve V4
`value` acilimi gercek koddur.

Dosyanin dogruladigi invariantlar `odata.py` docstring'inde bildirilenlerdir:
  A. Malzeme aramasi aciklamada da arar.
  B. Stok fotografi bir ATP teyidi olarak sunulmaz.
  C. PO okumasi V4 `$expand` ile yapilir; baslik basina ek GET yoktur.
  D. Tedarikci skorlari okunamazsa `estimated_fields` ile isaretlenir.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from robotics_agent.adapters.sap import SAPError
from robotics_agent.sap.models import PurchaseRequisitionItem
from robotics_agent.sap.odata import ODataSAPBackend, reference_token

BASE = "https://s4.test"


class FakeS4:
    """V2 ve V4'u ayni anda konusan sahte S/4 sistemi.

    Rota anahtari entity set adidir. V4 servisleri yol icinde `/odata4/`
    tasidigi icin sarmalayici ona gore secilir - gercek sistemde de fark
    budur.
    """

    def __init__(self, routes: dict[str, list[dict]] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.created: list[dict] = []
        self.create_response: dict = {}
        self.fail: dict[str, int] = {}

    def calls_to(self, entity_set: str) -> list[httpx.Request]:
        return [r for r in self.requests if entity_set in r.url.path]

    def filter_of(self, entity_set: str, index: int = 0) -> str:
        return self.calls_to(entity_set)[index].url.params.get("$filter", "")

    @property
    def writes(self) -> list[httpx.Request]:
        """Gercek yazma istekleri.

        `$batch` HARIC tutulur: OData V4'te toplu **okuma** da POST ile yapilir
        (`_fill_vendor_names` boyle calisir). Her POST'u yazma saymak, dogru
        calisan bir okuma optimizasyonunu yazma sanardi.
        """
        return [
            r for r in self.requests
            if r.method in {"POST", "PUT", "PATCH", "DELETE"} and "$batch" not in r.url.path
        ]

    @property
    def batch_reads_only(self) -> bool:
        """Gonderilen her `$batch` govdesi yalniz GET iceriyor mu?"""
        for request in self.requests:
            if "$batch" not in request.url.path:
                continue
            body = (request.content or b"").decode("utf-8", "replace").upper()
            if any(verb in body for verb in ("POST ", "PATCH ", "PUT ", "DELETE ")):
                return False
        return True

    def created_containing(self, key: str) -> dict:
        """Belirtilen alani tasiyan ilk POST govdesi."""
        for body in self.created:
            if isinstance(body, dict) and key in body:
                return body
        raise AssertionError(f"'{key}' iceren POST govdesi yok: {self.created}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        is_v4 = "/odata4/" in path

        if request.method == "HEAD" or "$metadata" in path:
            return httpx.Response(200, headers={"x-csrf-token": "tok"}, text="<edmx/>")

        if request.method == "POST":
            self.created.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                201, json=self.create_response,
                headers={"Content-Type": "application/json", "ETag": 'W/"1"'},
            )

        entity_set = path.rstrip("/").rsplit("/", 1)[-1].split("(")[0]
        for needle, status in self.fail.items():
            if needle in path:
                return httpx.Response(status, json={"error": {"message": {"value": "yok"}}})
        rows = self.routes.get(entity_set, [])

        if is_v4:
            body: dict = {"value": rows}
        else:
            body = {"d": {"results": rows}}
        return httpx.Response(200, json=body, headers={"Content-Type": "application/json"})


def build(settings_factory, tmp_path, routes=None, create=None, fail=None):
    settings = settings_factory(
        tmp_path,
        **{
            "sap.backend": "odata", "sap.base_url": BASE, "sap.auth_mode": "basic",
            "sap.username": "svc", "sap.password": "pw", "sap.plant": "1100",
            "sap.purch_org": "1000", "sap.purch_group": "R01",
            "sap.company_code": "1000", "sap.currency": "EUR",
            "sap.read_only": False,
            "security.allowed_sap_hosts": ("s4.test",),
        },
    )
    fake = FakeS4(routes)
    fake.create_response = create or {}
    fake.fail = fail or {}
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(fake.handler))

    import robotics_agent.sap.odata as mod

    original = mod.build_http_client
    mod.build_http_client = lambda connection, cfg: client  # noqa: ARG005
    try:
        backend = ODataSAPBackend(settings)
    finally:
        mod.build_http_client = original
    return backend, fake


# ---------------------------------------------------------------------------
# Invariant A - malzeme aramasi aciklamada da arar
# ---------------------------------------------------------------------------
def test_arama_aciklamada_da_arar(settings_factory, tmp_path):
    """Yalniz malzeme numarasinda aramak gercek sistemde sonucu bosaltir."""
    backend, fake = build(
        settings_factory, tmp_path,
        {
            "A_ProductDescription": [{"Product": "R-1000", "ProductDescription": "Robot kolu"}],
            "A_Product": [{
                "Product": "R-1000", "ProductType": "HALB", "ProductGroup": "ROB01",
                "BaseUnit": "ST", "GrossWeight": "128.5",
                "to_Description": {"results": [
                    {"ProductDescription": "Robot kolu", "Language": "TR"}]},
                "to_Plant": {"results": [
                    {"Plant": "1100", "ProcurementType": "F", "PlndDelryDurnInDays": "21"}]},
            }],
        },
    )
    materials = backend.search_materials("robot")

    assert fake.calls_to("A_ProductDescription"), "aciklama araması yapilmadi"
    assert "substringof('robot',ProductDescription)" in fake.filter_of("A_ProductDescription")
    assert materials[0].material_id == "R-1000"
    assert materials[0].description == "Robot kolu"
    assert materials[0].planned_delivery_days == 21


def test_arama_tirnagi_kacisla_gonderir(settings_factory, tmp_path):
    backend, fake = build(settings_factory, tmp_path, {"A_Product": [], "A_ProductDescription": []})
    backend.search_materials("O'Brien")
    assert "O''Brien" in fake.filter_of("A_ProductDescription")


def test_guncel_product_api_mrp_area_alanlarini_esler(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory,
        tmp_path,
        {"A_Product": [{
            "Product": "R-1000",
            "ProductType": "HALB",
            "to_Plant": {"results": [{
                "Plant": "1100",
                "ProcurementType": "F",
                "MRPResponsible": "007",
                "to_PlantMRPArea": {"results": [{
                    "MRPArea": "1100",
                    "PlannedDeliveryDurationInDays": "18",
                }]},
            }]},
        }]},
    )

    material = backend.get_material("R-1000")

    assert material is not None
    assert material.mrp_controller == "007"
    assert material.planned_delivery_days == 18
    request = fake.calls_to("A_Product")[0]
    assert "to_Plant/to_PlantMRPArea" in request.url.params.get("$expand", "")


# ---------------------------------------------------------------------------
# Stok - acik siparisten teslim edilen dusulur
# ---------------------------------------------------------------------------
def test_acik_siparis_teslim_edileni_duser(settings_factory, tmp_path):
    backend, _ = build(
        settings_factory, tmp_path,
        {
            "A_MatlStkInAcctMod": [
                {"Material": "R-1000", "Plant": "1100", "InventoryStockType": "01",
                 "MatlWrhsStkQtyInMatlBaseUnit": "14", "StorageLocation": "0001"},
                {"Material": "R-1000", "Plant": "1100", "InventoryStockType": "02",
                 "MatlWrhsStkQtyInMatlBaseUnit": "2"},
            ],
            "PurchaseOrderItem": [
                {"PurchaseOrder": "4500001", "OrderQuantity": "20",
                 "_PurchaseOrderScheduleLineTP": [{"OpenPurchaseOrderQuantity": "12"}]},
            ],
            "SupplyDemandItems": [],
        },
    )
    level = backend.get_stock(["R-1000"], plant="1100")[0]
    assert level.unrestricted_qty == 14.0
    assert level.quality_inspection_qty == 2.0
    assert level.on_order_qty == 12.0, "20 siparis - 8 teslim"


# ---------------------------------------------------------------------------
# Invariant B - gercek ATP
# ---------------------------------------------------------------------------
def test_guncel_mrp_tarihi_eslenir(settings_factory, tmp_path):
    backend, _ = build(
        settings_factory,
        tmp_path,
        {"SupplyDemandItems": [{
            "Material": "R-1000",
            "MRPElement": "AR",
            "MRPElementOpenQuantity": "4",
            "MRPElementAvailyOrRqmtDate": "2026-09-10",
        }]},
    )

    items = backend.get_supply_demand("R-1000")

    assert items and items[0].availability_date == date(2026, 9, 10)


def test_mrp_malzeme_yok_kodu_bos_sonuc_doner(settings_factory, tmp_path, monkeypatch):
    backend, _ = build(settings_factory, tmp_path)

    def no_material(*_args, **_kwargs):
        raise SAPError("No material found for the specified selection criteria", code="PP_MRP_RSC/010")

    monkeypatch.setattr(backend.v2, "read", no_material)

    assert backend.get_supply_demand("R-1000") == []


# ---------------------------------------------------------------------------
# Invariant C - PO okumasinda N+1 yok, agirlikli termin
# ---------------------------------------------------------------------------
def test_po_expand_ile_tek_cagri_ve_agirlikli_termin(settings_factory, tmp_path):
    erken, gec = date(2026, 9, 1), date(2026, 12, 1)
    backend, fake = build(
        settings_factory, tmp_path,
        {"PurchaseOrderItem": [{
            "PurchaseOrder": "4500001", "Material": "R-1000", "OrderQuantity": "10",
            "NetPriceAmount": "100", "DocumentCurrency": "EUR",
            "PurchaseOrderItemText": "Robot kolu",
            "_PurchaseOrder": [{"Supplier": "V-100", "SupplierName": "Acme",
                                "CreationDate": "2026-08-01"}],
            "_PurchaseOrderScheduleLineTP": [
                {"ScheduleLineDeliveryDate": erken.isoformat(),
                 "ScheduleLineOrderQuantity": "9", "OpenPurchaseOrderQuantity": "5"},
                {"ScheduleLineDeliveryDate": gec.isoformat(),
                 "ScheduleLineOrderQuantity": "1", "OpenPurchaseOrderQuantity": "1"},
            ],
            "_PurOrdAccountAssignment": [{"WBSElementExternalID": "P-100.1"}],
        }]},
    )
    orders = backend.get_purchase_orders(material_id="R-1000")

    assert len(fake.calls_to("PurchaseOrder")) == 1, "baslik basina ek GET = N+1 regresyonu"
    po = orders[0]
    assert po.vendor_name == "Acme"
    assert po.status == "partially_delivered"
    assert po.delivered_qty == 4.0
    assert po.wbs_element == "P-100.1"
    # 9 birim erken, 1 birim gec -> agirlikli tarih erkene yakin olmali
    assert erken <= po.requested_delivery_date <= erken + timedelta(days=15)


# ---------------------------------------------------------------------------
# Invariant D - okunamayan skor uydurulmaz
# ---------------------------------------------------------------------------
def test_skor_okunamazsa_estimated_isaretlenir(settings_factory, tmp_path):
    backend, _ = build(
        settings_factory, tmp_path, {}, fail={"A_SUPPLIEROPLSCORESAV_CDS": 404}
    )
    score = backend.get_supplier_score("V-100")
    assert score is not None
    assert score.overall_score is None, "okunamayan skor 0.0 degil None olmali"
    assert "overall_score" in score.estimated_fields
    assert score.has_real_data is False


def test_guncel_supplier_score_sutunlarini_ortalar(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory,
        tmp_path,
        {"Results": [
            {
                "Supplier": "V-100", "PurchasingOrganization": "1000",
                "SupplierOperationalScore": "80", "PriceVarianceScore": "70",
                "TimeVarianceScore": "90", "QuantityVarianceScore": "60",
                "InspectionLotQualityScore": "100", "QualityNotificationScore": "80",
            },
            {
                "Supplier": "V-100", "PurchasingOrganization": "1000",
                "SupplierOperationalScore": "100", "PriceVarianceScore": "90",
                "TimeVarianceScore": "70", "QuantityVarianceScore": "80",
                "InspectionLotQualityScore": "80", "QualityNotificationScore": "60",
            },
        ]},
    )

    score = backend.get_supplier_score("V-100")

    assert score is not None
    assert score.overall_score == 90
    assert score.delivery_score == 80
    assert score.quality_score == 80
    calls = fake.calls_to("Results")
    assert calls and "P_DateFunction='YEARTODATE'" in calls[0].url.path


def test_guncel_tedarikci_kok_ve_adres_alanlari_eslenir(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory,
        tmp_path,
        {
            "A_Supplier": [{
                "Supplier": "V-100", "SupplierName": "Acme", "PurchasingIsBlocked": True,
            }],
            "A_BusinessPartnerAddress": [{
                "BusinessPartner": "V-100", "Country": "DE", "CityName": "Berlin",
            }],
            "Results": [],
        },
    )

    vendor = backend.get_vendor("V-100")

    assert vendor is not None
    assert vendor.country == "DE" and vendor.city == "Berlin" and vendor.blocked is True
    assert "Country" not in fake.calls_to("A_Supplier")[0].url.params.get("$select", "")


def test_tedarikciler_ana_veri_adres_ve_skoru_toplu_okur(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory,
        tmp_path,
        {
            "A_Supplier": [
                {"Supplier": "V-100", "SupplierName": "Acme"},
                {"Supplier": "V-200", "SupplierName": "Beta"},
            ],
            "A_BusinessPartnerAddress": [
                {"BusinessPartner": "V-100", "Country": "DE", "CityName": "Berlin"},
                {"BusinessPartner": "V-200", "Country": "TR", "CityName": "Istanbul"},
            ],
            "Results": [
                {"Supplier": "V-100", "PurchasingOrganization": "1000",
                 "SupplierOperationalScore": "90", "TimeVarianceScore": "80"},
                {"Supplier": "V-200", "PurchasingOrganization": "1000",
                 "SupplierOperationalScore": "70", "TimeVarianceScore": "60"},
            ],
        },
    )

    vendors = backend.get_vendors(["V-100", "V-200"])

    assert set(vendors) == {"V-100", "V-200"}
    assert vendors["V-100"].country == "DE"
    assert vendors["V-200"].on_time_delivery_pct == 60
    supplier_calls = [
        request for request in fake.requests
        if request.url.path.rstrip("/").rsplit("/", 1)[-1] == "A_Supplier"
    ]
    assert len(supplier_calls) == 1
    assert len(fake.calls_to("A_BusinessPartnerAddress")) == 1
    assert len(fake.calls_to("Results")) == 1


def test_v4_po_wbs_hesap_atamasindan_filtrelenir(settings_factory, tmp_path):
    backend, fake = build(settings_factory, tmp_path, {"PurchaseOrderItem": []})

    backend.get_purchase_orders(wbs_element="P-100.1")

    request = fake.calls_to("PurchaseOrderItem")[0]
    assert "_PurOrdAccountAssignment/any" in request.url.params.get("$filter", "")
    assert "_PurOrdAccountAssignment" in request.url.params.get("$expand", "")


# ---------------------------------------------------------------------------
# PR - hazirlik asla yazmaz, gonderim referans tasir
# ---------------------------------------------------------------------------
def _pr_routes() -> dict:
    return {
        "A_Product": [{
            "Product": "R-1000", "ProductType": "HALB", "BaseUnit": "ST",
            "to_Description": {"results": [{"ProductDescription": "Robot kolu",
                                            "Language": "TR"}]},
            "to_Plant": {"results": [{"Plant": "1100", "PlndDelryDurnInDays": "30",
                                      "MinimumLotSizeQuantity": "5"}]},
        }],
        "A_PurchasingInfoRecord": [{
            "Supplier": "V-100", "Material": "R-1000",
            "to_PurgInfoRecdOrgPlantData": {"results": [{
                "PurchasingOrganization": "1000", "NetPriceAmount": "17500",
                "Currency": "EUR", "MinimumPurchaseOrderQuantity": "5",
                "MaterialPlannedDeliveryDurn": "30"}]},
        }],
        "A_Supplier": [{"Supplier": "V-100", "SupplierName": "Acme"}],
        "A_ProductValuation": [],
    }


def test_prepare_asla_yazmaz(settings_factory, tmp_path):
    backend, fake = build(settings_factory, tmp_path, _pr_routes())
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="R-1000", quantity=2,
                                 delivery_date=date.today() + timedelta(days=3))],
        header_text="Hat 3",
    )
    assert fake.writes == [], "prepare_* HICBIR kosulda yazmamali"
    assert fake.batch_reads_only, "$batch icinde degisiklik istegi var"
    alanlar = {f.field for f in draft.findings}
    assert "quantity" in alanlar, "MOQ ihlali bildirilmedi"
    assert "delivery_date" in alanlar, "temin suresi ihlali bildirilmedi"
    assert "account_assignment" in alanlar


def test_submit_referans_token_basliga_gomer(settings_factory, tmp_path):
    """S/4'te PR baslik metni 40 karakterle sinirli: hash gomulur."""
    backend, fake = build(
        settings_factory, tmp_path, _pr_routes(),
        create={"PurchaseRequisition": "0010004711"},
    )
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="R-1000", quantity=10, wbs_element="P-1.1")]
    )
    result = backend.submit_purchase_requisition(draft, external_reference="turn-42")

    # `prepare` sirasinda tedarikci adlari icin bir `$batch` POST'u da atilir;
    # PR govdesi icerige gore secilir.
    body = fake.created_containing("PurchaseRequisitionHeaderText")
    token = reference_token("turn-42")
    assert token.startswith("REF#")
    assert token in body["PurchaseRequisitionHeaderText"]
    assert len(body["PurchaseRequisitionHeaderText"]) <= 40, "SAP alan siniri asildi"
    assert result.requisition_id == "0010004711"
    assert result.created is True


def test_engelleyici_bulgu_varsa_gonderilmez(settings_factory, tmp_path):
    from robotics_agent.sap.models import PurchaseRequisitionDraft, ValidationFinding

    backend, fake = build(settings_factory, tmp_path, _pr_routes())
    draft = PurchaseRequisitionDraft(
        items=[{"material_id": "R-1000"}],
        findings=[ValidationFinding(severity="error", field="x", message="engelleyici")],
    )
    with pytest.raises(SAPError) as exc:
        backend.submit_purchase_requisition(draft, external_reference="k")
    assert exc.value.code == "EBAN_VALIDATION_FAILED"
    assert fake.writes == [], "engelleyici bulguya ragmen SAP'a yazildi"


def test_referans_aramasi_contains_kullanir(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory, tmp_path,
        {"PurchaseRequisition": [{"PurchaseRequisition": "0010004711",
                                  "PurchaseRequisitionHeaderText": "REF#abc Hat 3",
                                  "_PurchaseRequisitionItem": []}]},
    )
    found = backend.find_purchase_requisition_by_reference("turn-42")
    assert found is not None
    assert "contains(PurchaseRequisitionHeaderText" in fake.filter_of("PurchaseRequisition")


# ---------------------------------------------------------------------------
# Clean core - egress allowlist
# ---------------------------------------------------------------------------
def test_izinsiz_host_engellenir(settings_factory, tmp_path):
    from robotics_agent.adapters.sap import HostNotAllowed

    backend, _ = build(settings_factory, tmp_path, {"A_Product": []})
    backend._core_v2.allowed_hosts = ("baska.host",)
    with pytest.raises(HostNotAllowed):
        backend.search_materials("robot")


# ---------------------------------------------------------------------------
# Released P2P API'leri - PO / malzeme belgesi / tedarikci faturasi
# ---------------------------------------------------------------------------
def test_p2p_released_servisleri_referanslardan_eslenir(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory,
        tmp_path,
        {
            "A_PurchaseOrderItem": [{
                "PurchaseOrder": "4500001",
                "PurchaseOrderItem": "00010",
                "PurchaseOrderItemText": "Robot kolu",
                "Material": "R-1000",
                "Plant": "1100",
                "OrderQuantity": "10",
                "PurchaseOrderQuantityUnit": "ST",
                "DocumentCurrency": "EUR",
                "NetPriceAmount": "2500",
                "NetPriceQuantity": "1",
                "GoodsReceiptIsExpected": True,
                "InvoiceIsExpected": True,
                "PurchaseRequisition": "100001",
                "PurchaseRequisitionItem": "00010",
                "to_AccountAssignment": {"results": [{
                    "WBSElementExternalID": "P-100.1",
                }]},
            }],
            "A_PurchaseOrderScheduleLine": [{
                "PurchasingDocument": "4500001",
                "PurchasingDocumentItem": "00010",
                "ScheduleLine": "0001",
                "ScheduleLineDeliveryDate": "2026-09-15",
                "ScheduleLineOrderQuantity": "10",
                "PurchaseOrderQuantityUnit": "ST",
            }],
            "A_MaterialDocumentItem": [{
                "MaterialDocument": "5000001",
                "MaterialDocumentYear": "2026",
                "MaterialDocumentItem": "0001",
                "Material": "R-1000",
                "Plant": "1100",
                "GoodsMovementType": "101",
                "PurchaseOrder": "4500001",
                "PurchaseOrderItem": "00010",
                "QuantityInEntryUnit": "6",
                "EntryUnit": "ST",
                "GoodsMovementIsCancelled": False,
                "to_MaterialDocumentHeader": {"results": [{"PostingDate": "2026-08-18"}]},
            }],
            "A_SuplrInvcItemPurOrdRef": [{
                "SupplierInvoice": "5100001",
                "FiscalYear": "2026",
                "SupplierInvoiceItem": "0001",
                "PurchaseOrder": "4500001",
                "PurchaseOrderItem": "00010",
                "QuantityInPurchaseOrderUnit": "4",
                "SupplierInvoiceItemAmount": "10000",
            }],
            "A_SupplierInvoice": [{
                "SupplierInvoice": "5100001",
                "FiscalYear": "2026",
                "CompanyCode": "1000",
                "DocumentDate": "2026-08-19",
                "PostingDate": "2026-08-20",
                "InvoicingParty": "V-100",
                "DocumentCurrency": "EUR",
                "InvoiceGrossAmount": "11900",
                "PaymentTerms": "NT30",
                "DueCalculationBaseDate": "2026-08-20",
                "NetPaymentDays": "30",
                "PaymentBlockingReason": "PP",
                "SupplierInvoiceStatus": "POSTED",
                "to_SuplrInvcItemPurOrdRef": {"results": []},
                "to_SupplierInvoiceTax": {"results": [{"TaxAmount": "1900"}]},
            }],
            "A_Supplier": [{"Supplier": "V-100", "SupplierName": "Acme"}],
        },
    )

    items = backend.get_purchase_order_items("4500001")
    schedules = backend.get_schedule_lines("4500001")
    receipts = backend.get_goods_receipts(po_id="4500001")
    invoices = backend.get_supplier_invoices(po_id="4500001")

    assert items[0].net_value == 25_000
    assert items[0].wbs_element == "P-100.1"
    assert schedules[0].requested_date == date(2026, 9, 15)
    assert schedules[0].confirmed_date is None, "istatistik tarihi teyit diye uydurulmamali"
    assert receipts[0].po_item == "00010" and receipts[0].quantity == 6
    assert invoices[0].po_item_quantities == {"4500001/00010": 4.0}
    assert invoices[0].gross_amount == 11_900
    assert invoices[0].tax_amount == 1_900
    assert invoices[0].net_amount == 10_000
    assert invoices[0].status == "blocked"
    # ZLSPR odeme blokaj anahtari, OMR6 tolerans anahtari DEGILDIR: kendi
    # alaninda tasinir ve neden uydurulmaz. Eskiden anahtar `tolerance_key`e
    # yazilip neden "manual" deniyordu; 'R' gibi OTOMATIK bir blokaj boylece
    # elle konmus gibi gorunuyor ve kullaniciyi yanlis islemin ustune
    # yolluyordu.
    block = invoices[0].blocks[0]
    assert block.payment_block_key == "PP"
    assert block.tolerance_key == "", "odeme blokaj anahtari tolerans anahtari sayilmamali"
    assert block.block_reason == "unknown", "okunamayan neden uydurulmamali"
    assert len(fake.calls_to("A_SuplrInvcItemPurOrdRef")) == 1
    assert len(fake.calls_to("A_SupplierInvoice")) == 1, "fatura basliginda N+1 olmamali"


def test_p2p_miktarlari_gr_ve_fatura_referansindan_netlesir(
    settings_factory, tmp_path
):
    """PO complete bayragi yerine gercek 101/102 ve RSEG miktarlari kullanilir."""
    backend, _ = build(
        settings_factory,
        tmp_path,
        {
            "A_PurchaseOrderItem": [{
                "PurchaseOrder": "4500001", "PurchaseOrderItem": "00010",
                "Material": "R-1000", "OrderQuantity": "10",
                "PurchaseOrderQuantityUnit": "ST", "DocumentCurrency": "EUR",
                "NetPriceAmount": "100", "NetPriceQuantity": "1",
            }],
            "A_PurchaseOrderScheduleLine": [],
            "A_MaterialDocumentItem": [
                {"MaterialDocument": "5001", "MaterialDocumentYear": "2026",
                 "MaterialDocumentItem": "0001", "GoodsMovementType": "101",
                 "PurchaseOrder": "4500001", "PurchaseOrderItem": "00010",
                 "QuantityInEntryUnit": "8", "EntryUnit": "ST"},
                {"MaterialDocument": "5002", "MaterialDocumentYear": "2026",
                 "MaterialDocumentItem": "0001", "GoodsMovementType": "102",
                 "PurchaseOrder": "4500001", "PurchaseOrderItem": "00010",
                 "QuantityInEntryUnit": "2", "EntryUnit": "ST"},
            ],
            "A_SuplrInvcItemPurOrdRef": [{
                "SupplierInvoice": "5101", "FiscalYear": "2026",
                "SupplierInvoiceItem": "0001", "PurchaseOrder": "4500001",
                "PurchaseOrderItem": "00010", "QuantityInPurchaseOrderUnit": "4",
            }],
            "A_SupplierInvoice": [{
                "SupplierInvoice": "5101", "FiscalYear": "2026",
                "DocumentCurrency": "EUR", "InvoiceGrossAmount": "400",
            }],
        },
    )
    items = backend.get_purchase_order_items("4500001")
    receipts = backend.get_goods_receipts(po_id="4500001")
    invoices = backend.get_supplier_invoices(po_id="4500001")

    delivered = sum((-1 if gr.is_reversal else 1) * gr.quantity for gr in receipts)
    invoiced = sum(inv.po_item_quantities.get("4500001/00010", 0) for inv in invoices)
    items[0].delivered_qty = delivered
    items[0].invoiced_qty = invoiced
    assert items[0].delivered_qty == 6
    assert items[0].invoiced_qty == 4
    assert items[0].uninvoiced_qty == 2


def test_tool_call_budget_is_checked_against_real_odata_roundtrips(
    settings_factory, tmp_path, purchaser
):
    """Mock port metodu degil, OData HTTP transport istekleri sayilir."""
    from robotics_agent.cache import reset_tool_cache
    from robotics_agent.tools import ToolContext, execute_tool, load_all_tools
    from robotics_agent.tools.registry import REGISTRY

    backend, fake = build(
        settings_factory,
        tmp_path,
        {
            "A_SupplierInvoice": [{
                "SupplierInvoice": "5100001",
                "FiscalYear": "2026",
                "CompanyCode": "1000",
                "InvoicingParty": "V-100",
                "DocumentCurrency": "EUR",
                "InvoiceGrossAmount": "11900",
                "PaymentBlockingReason": "PP",
                "SupplierInvoiceStatus": "POSTED",
                "to_SuplrInvcItemPurOrdRef": {"results": []},
                "to_SupplierInvoiceTax": {"results": []},
            }],
        },
    )
    load_all_tools()
    reset_tool_cache()
    ctx = ToolContext(settings=backend.settings, sap=backend, actor=purchaser)

    payload, is_error = execute_tool(
        "sap_supplier_invoice_status", {"only_blocked": True}, ctx
    )

    assert not is_error, payload
    actual_roundtrips = len(fake.requests)
    budget = REGISTRY["sap_supplier_invoice_status"].performance_budget.max_sap_calls
    assert actual_roundtrips == backend.sap_call_count == ctx.sap_call_count
    assert actual_roundtrips <= budget
    reset_tool_cache()


def test_supplier_invoice_po_filter_is_rechecked_when_server_ignores_it(
    settings_factory, tmp_path
):
    """Hub sandbox HTTP 200 ile filtresiz veri verse bile baska PO sizmaz."""
    backend, fake = build(
        settings_factory,
        tmp_path,
        {
            # FakeS4 kasitli olarak `$filter` uygulamaz. Bu rota hem hedef
            # PO'yu hem de ilgisiz bir PO'yu dondurerek Hub davranisini taklit
            # eder.
            "A_SuplrInvcItemPurOrdRef": [
                {
                    "SupplierInvoice": "5100001",
                    "FiscalYear": "2026",
                    "SupplierInvoiceItem": "0001",
                    "PurchaseOrder": "4500000012",
                    "PurchaseOrderItem": "00010",
                    "QuantityInPurchaseOrderUnit": "1",
                    "DocumentCurrency": "USD",
                    "SupplierInvoiceItemAmount": "100",
                },
                {
                    "SupplierInvoice": "5199999",
                    "FiscalYear": "2026",
                    "SupplierInvoiceItem": "0001",
                    "PurchaseOrder": "4599999999",
                    "PurchaseOrderItem": "00010",
                    "QuantityInPurchaseOrderUnit": "9",
                    "DocumentCurrency": "USD",
                    "SupplierInvoiceItemAmount": "900",
                },
            ],
            # Ikinci sorgunun filtresi de yok sayilmis gibi iki baslik gelir.
            "A_SupplierInvoice": [
                {
                    "SupplierInvoice": "5100001",
                    "FiscalYear": "2026",
                    "CompanyCode": "1000",
                    "InvoicingParty": "V-100",
                    "DocumentCurrency": "USD",
                    "InvoiceGrossAmount": "100",
                    "SupplierInvoiceStatus": "POSTED",
                    "to_SuplrInvcItemPurOrdRef": {"results": []},
                    "to_SupplierInvoiceTax": {"results": []},
                },
                {
                    "SupplierInvoice": "5199999",
                    "FiscalYear": "2026",
                    "CompanyCode": "1000",
                    "InvoicingParty": "V-999",
                    "DocumentCurrency": "USD",
                    "InvoiceGrossAmount": "900",
                    "SupplierInvoiceStatus": "POSTED",
                    "to_SuplrInvcItemPurOrdRef": {"results": []},
                    "to_SupplierInvoiceTax": {"results": []},
                },
            ],
        },
    )

    invoices = backend.get_supplier_invoices(po_id="4500000012")

    assert [invoice.invoice_id for invoice in invoices] == ["5100001"]
    assert invoices[0].po_ids == ["4500000012"]
    assert "PurchaseOrder eq '4500000012'" in fake.filter_of(
        "A_SuplrInvcItemPurOrdRef"
    )


# ---------------------------------------------------------------------------
# Kimlik iletimi ve SAP tarafi atfedilebilirlik
# ---------------------------------------------------------------------------
def test_calisan_kisinin_kimligi_sap_istegine_eklenir(settings_factory, tmp_path):
    """SAP'a giden istek, cagriyi tetikleyen insani tasimali.

    Bu bir YETKILENDIRME degil izlenebilirlik ozelligidir: SAP yetkileri hala
    baglantinin kimligine gore uygulanir. Ama baslik olmadan SAP tarafindaki
    gateway izleri hicbir insana baglanamaz.
    """
    backend, fake = build(
        settings_factory, tmp_path,
        {"A_Product": [{"Product": "R-1000", "ProductType": "HALB"}]},
    )
    backend.set_acting_subject("ali@firma.test")
    backend.get_material("R-1000")

    sent = [r for r in fake.requests if r.method == "GET"]
    assert sent, "hic GET gitmedi"
    assert any(r.headers.get("X-CertaOps-On-Behalf-Of") == "ali@firma.test" for r in sent)


def test_kimlik_temizlenince_baslik_gonderilmez(settings_factory, tmp_path):
    """Bos kimlik baslik uretmemeli: onceki turun kullanicisi sizmamali."""
    backend, fake = build(
        settings_factory, tmp_path,
        {"A_Product": [{"Product": "R-1000", "ProductType": "HALB"}]},
    )
    backend.set_acting_subject("ali@firma.test")
    backend.get_material("R-1000")
    backend.set_acting_subject("")
    fake.requests.clear()
    backend.get_material("R-1000")

    assert all("X-CertaOps-On-Behalf-Of" not in r.headers for r in fake.requests)


def test_basic_auth_sap_tarafinda_atfedilemez_sayilir(settings_factory, tmp_path):
    """Teknik kullanici ile baglanan bir sistem islemi insana atfedemez.

    Bunun gorunur olmasi onemli: aksi halde "SAP'ta da izi var" varsayimi
    sessizce yanlis kalir.
    """
    backend, _ = build(settings_factory, tmp_path)
    described = backend.connection.describe()

    assert described["principal_propagation"] is False
    assert described["sap_attribution"] == "technical_user"


def test_principal_propagation_destination_ile_taninir():
    """Destination principal propagation kullaniyorsa SAP kisiyi gorur."""
    from robotics_agent.adapters.sap.destination import ResolvedConnection

    for auth_type in ("PrincipalPropagation", "SAMLAssertion", "OAuth2SAMLBearerAssertion"):
        connection = ResolvedConnection(base_url=BASE, auth_type=auth_type)
        assert connection.principal_propagation is True
        assert connection.describe()["sap_attribution"] == "principal"

    assert ResolvedConnection(base_url=BASE, auth_type="BasicAuthentication").principal_propagation is False


# ---------------------------------------------------------------------------
# Regresyon: gercek SAP hazirlik incelemesinde bulunan adapter hatalari.
# ---------------------------------------------------------------------------
def test_arama_eslesme_bulamayinca_filtresiz_okuma_yapmaz(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory, tmp_path,
        routes={"A_ProductDescription": [], "A_Product": []},
    )

    assert backend.search_materials("kalorifer kazani", limit=5) == []
    unfiltered = [r for r in fake.calls_to("A_Product") if not r.url.params.get("$filter")]
    assert not unfiltered


def test_aciklama_aramasi_buyuk_harf_varyantini_da_dener(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory, tmp_path,
        routes={"A_ProductDescription": [], "A_Product": []},
    )
    backend.search_materials("vana", limit=5)

    filter_expr = fake.filter_of("A_ProductDescription")
    assert "substringof('vana',ProductDescription)" in filter_expr
    assert "substringof('VANA',ProductDescription)" in filter_expr


def test_arama_sonucu_okumasi_alan_secimi_yapar(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory, tmp_path,
        routes={
            "A_ProductDescription": [{"Product": "100000", "ProductDescription": "VANA"}],
            "A_Product": [{"Product": "100000", "ProductType": "ROH"}],
        },
    )
    backend.search_materials("vana", limit=5)

    select = fake.calls_to("A_Product")[-1].url.params.get("$select", "")
    assert "ProductGroup" in select and "to_Plant/Plant" in select


def test_stok_okumasi_malzeme_sayisindan_bagimsiz_cagri_yapar(settings_factory, tmp_path):
    ids = [f"MAT-{i}" for i in range(10)]
    backend, fake = build(
        settings_factory, tmp_path,
        routes={"A_MatlStkInAcctMod": [], "A_PurchaseOrderItem": [], "SupplyDemandItems": []},
    )
    backend.get_stock(ids)

    assert len(fake.calls_to("SupplyDemandItems")) == 1
    filter_expr = fake.filter_of("SupplyDemandItems")
    assert "MAT-0" in filter_expr and "MAT-9" in filter_expr


def test_metadata_istegi_format_json_tasimaz(settings_factory, tmp_path):
    backend, fake = build(settings_factory, tmp_path)
    backend.metadata_contract("product")

    metadata_calls = [r for r in fake.requests if "$metadata" in r.url.path]
    assert metadata_calls
    for request in metadata_calls:
        assert "$format" not in request.url.params, request.url
