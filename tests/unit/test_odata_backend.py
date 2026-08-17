"""S/4HANA OData backend'inin davranis testleri.

`sap/odata.py` 470 ifade ve 164 dal tasiyordu; kapsam **%0**'di. ECC backend'i
`httpx.MockTransport` ile dogrulanirken S/4 backend'i hic ayni muameleyi
gormemisti - filtre uretimi, V4 `$expand` ayristirma, agirlikli termin hesabi
ve PR gonderimi calisma zamaninda ilk kez sinaniyordu.

Sahte olan tek sey ag: HTTP cekirdegi, CSRF akisi, V2 `d`/`results` ve V4
`value` acilimi gercek koddur.

Dosyanin dogruladigi invariantlar `odata.py` docstring'inde bildirilenlerdir:
  A. Malzeme aramasi aciklamada da arar.
  B. `check_atp` gercek ATP servisini kullanir; stok fotografi yerine gecmez.
  C. PO okumasi V4 `$expand` ile yapilir; baslik basina ek GET yoktur.
  D. Tedarikci skorlari okunamazsa `estimated_fields` ile isaretlenir.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from robotics_agent.adapters.sap import SAPError, SAPNotSupported
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
                 "_PurchaseOrderScheduleLine": [{"ScheduleLineDeliveredQuantity": "8"}]},
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
def test_atp_kismi_teyit_ve_tam_teyit_tarihi(settings_factory, tmp_path):
    need = date.today() + timedelta(days=10)
    gec = need + timedelta(days=15)
    backend, _ = build(
        settings_factory, tmp_path,
        {"ProductAvailabilityInformation": [
            {"ConfirmedQuantity": "6", "ConfirmedDeliveryDate": need.isoformat(),
             "AvailabilityCheckType": "ATP"},
            {"ConfirmedQuantity": "4", "ConfirmedDeliveryDate": gec.isoformat(),
             "AvailabilityCheckType": "ATP"},
        ]},
    )
    result = backend.check_atp("R-1000", quantity=10, requested_date=need)
    assert result.confirmed_qty == 6.0, "istenen tarihten sonraki satir sayilmamali"
    assert result.shortfall_qty == 4.0
    assert result.full_confirmation_date == gec
    assert result.late_by_days == 15


def test_atp_bos_yanitta_acik_hata(settings_factory, tmp_path):
    """Stok fotografina sessizce dusmek yerine desteklenmedigini bildirir."""
    backend, _ = build(settings_factory, tmp_path, {"ProductAvailabilityInformation": []})
    with pytest.raises(SAPNotSupported) as exc:
        backend.check_atp("R-1000", quantity=5)
    assert "sap_discover_capabilities" in str(exc.value)


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
            "_PurchaseOrderScheduleLine": [
                {"ScheduleLineDeliveryDate": erken.isoformat(),
                 "ScheduleLineOrderQuantity": "9", "ScheduleLineDeliveredQuantity": "4"},
                {"ScheduleLineDeliveryDate": gec.isoformat(),
                 "ScheduleLineOrderQuantity": "1", "ScheduleLineDeliveredQuantity": "0"},
            ],
        }]},
    )
    orders = backend.get_purchase_orders(material_id="R-1000")

    assert len(fake.calls_to("PurchaseOrder")) == 1, "baslik basina ek GET = N+1 regresyonu"
    po = orders[0]
    assert po.vendor_name == "Acme"
    assert po.status == "partially_delivered"
    assert po.delivered_qty == 4.0
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
        "A_MaterialValuation": [],
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
# Clean core - released servisi olmayan alan acikca bildirilir
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [404, 403, 500])
def test_proje_maliyeti_released_servis_yoklugunu_bildirir(
    settings_factory, tmp_path, status
):
    """Servis yoksa SESSIZ BOS LISTE degil, acik hata donmeli.

    Regresyon kaydi: `ODataV2Client.read` 404'u yutup `[]` donduruyordu ve
    `get_project_costs` bunu "maliyet yok" olarak raporluyordu. Model de
    "proje butcesinde harcama gorunmuyor" diye ozetliyordu. Veri yoklugu ile
    yetenek yoklugu ayni sey degildir.
    """
    backend, _ = build(
        settings_factory, tmp_path, {}, fail={"ZAPI_PROJECT_COST_SRV": status}
    )
    with pytest.raises(SAPNotSupported):
        backend.get_project_costs(wbs_element="P-1")


def test_proje_maliyeti_gercekten_bos_ise_bos_doner(settings_factory, tmp_path):
    """Ters yon: servis VAR ve sonuc bos ise hata degil, bos liste donmeli."""
    backend, _ = build(settings_factory, tmp_path, {"ProjectCostSet": []})
    # $metadata sahte sistemde ProjectCostSet bildirmiyor -> yetenek yok sayilir.
    with pytest.raises(SAPNotSupported) as exc:
        backend.get_project_costs(wbs_element="P-1")
    assert "ProjectCostSet" in str(exc.value)


def test_izinsiz_host_engellenir(settings_factory, tmp_path):
    from robotics_agent.adapters.sap import HostNotAllowed

    backend, _ = build(settings_factory, tmp_path, {"A_Product": []})
    backend._core_v2.allowed_hosts = ("baska.host",)
    with pytest.raises(HostNotAllowed):
        backend.search_materials("robot")
