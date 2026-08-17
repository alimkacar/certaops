"""Gercek SAP'a baglanmadan once duzeltilen somut hatalarin regresyon testleri.

Her test bir arizayi temsil eder; hepsi gercek bir S/4HANA sisteminde
gozlenecek davranislari sahte transport uzerinden dogrular.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from robotics_agent.adapters.sap import SAPError
from robotics_agent.adapters.sap.http import HostNotAllowed, ODataHttpCore
from robotics_agent.sap.models import PurchaseRequisitionItem
from robotics_agent.sap.odata import ODataSAPBackend

BASE = "https://s4.test"


class FakeS4:
    """Entity set adina gore yanit veren sahte S/4 sistemi."""

    def __init__(self, routes=None, metadata_sets=None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.created: list[dict] = []
        self.create_response: dict = {}
        self.metadata_sets = metadata_sets
        self.not_found: set[str] = set()

    def paths(self, needle: str) -> list[httpx.Request]:
        return [r for r in self.requests if needle in r.url.path]

    def _metadata(self) -> str:
        if self.metadata_sets is None:
            return "<edmx/>"
        sets = "".join(
            f'<EntitySet Name="{name}" EntityType="x.{name}Type"/>'
            for name in self.metadata_sets
        )
        types = "".join(
            f'<EntityType Name="{name}Type"><Property Name="Id"/></EntityType>'
            for name in self.metadata_sets
        )
        return (
            '<?xml version="1.0"?><edmx:Edmx '
            'xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">'
            '<edmx:DataServices><Schema '
            'xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="x">'
            f"{types}<EntityContainer Name=\"c\">{sets}</EntityContainer>"
            "</Schema></edmx:DataServices></edmx:Edmx>"
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if "$metadata" in path:
            for needle in self.not_found:
                if needle in path:
                    return httpx.Response(404, json={"error": {"message": "yok"}})
            return httpx.Response(
                200, headers={"x-csrf-token": "tok"}, text=self._metadata()
            )
        if request.method == "POST":
            self.created.append(json.loads(request.content or b"{}"))
            return httpx.Response(
                201,
                json=self.create_response,
                headers={"Content-Type": "application/json", "ETag": 'W/"1"'},
            )
        entity_set = path.rstrip("/").rsplit("/", 1)[-1].split("(")[0]
        rows = self.routes.get(entity_set, [])
        body = {"value": rows} if "/odata4/" in path else {"d": {"results": rows}}
        return httpx.Response(200, json=body, headers={"Content-Type": "application/json"})


def build(settings_factory, tmp_path, routes=None, create=None, metadata_sets=None,
          not_found=(), **overrides):
    base = {
        "sap.backend": "odata", "sap.base_url": BASE, "sap.auth_mode": "basic",
        "sap.username": "svc", "sap.password": "pw", "sap.plant": "1100",
        "sap.purch_org": "1000", "sap.purch_group": "R01",
        "sap.company_code": "1000", "sap.currency": "EUR",
        "security.allowed_sap_hosts": ("s4.test",),
    }
    base.update(overrides)
    settings = settings_factory(tmp_path, **base)
    fake = FakeS4(routes, metadata_sets)
    fake.not_found = set(not_found)
    fake.create_response = create or {}
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(fake.handler))

    import robotics_agent.sap.odata as mod

    original = mod.build_http_client
    mod.build_http_client = lambda connection, cfg: client  # noqa: ARG005
    try:
        backend = ODataSAPBackend(settings)
    finally:
        mod.build_http_client = original
    return backend, fake


PRODUCT_ROW = {
    "Product": "MAT-1", "ProductType": "ROH", "ProductGroup": "G1", "BaseUnit": "ST",
    "to_Description": {"results": [{"Language": "TR", "ProductDescription": "Test"}]},
    "to_Plant": {"results": [{"Plant": "1100", "PlndDelryDurnInDays": "10",
                              "MinimumLotSizeQuantity": "1"}]},
}
INFO_ROW = {
    "PurchasingInfoRecord": "IR1", "Material": "MAT-1", "Supplier": "V-1", "IsDeleted": False,
    "to_PurgInfoRecdOrgPlantData": {"results": [{
        "PurchasingOrganization": "1000", "NetPriceAmount": "100.00", "Currency": "EUR",
        "MinimumPurchaseOrderQuantity": "1", "MaterialPlannedDeliveryDurn": "5"}]},
}
BASE_ROUTES = {
    "A_Product": [PRODUCT_ROW],
    "A_PurchasingInfoRecord": [INFO_ROW],
    "A_MaterialValuation": [{"Material": "MAT-1", "MovingAveragePrice": "90",
                             "Currency": "EUR", "PriceUnitQty": "1"}],
    "A_Supplier": [{"Supplier": "V-1", "SupplierName": "Test Tedarikci"}],
}


# --- 1. PR hesap atamasi ----------------------------------------------------
def test_wbs_hesap_atamasi_alt_entitye_yazilir(settings_factory, tmp_path):
    """WBS kalemin uzerine degil, `_PurchaseReqnAcctAssgmt` icine gider.

    Eski surum `WBSElement` alanini dogrudan kaleme yaziyordu; gercek sistemde
    bu ya 400 doner ya da alan yok sayilip hesap atamasi OLMAYAN bir talep
    olusur - ikincisi sessiz ve daha tehlikelidir.
    """
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=2, wbs_element="P-1-2")]
    )
    item = draft.payload["items"][0]
    assert item["AccountAssignmentCategory"] == "P"
    assert item["_PurchaseReqnAcctAssgmt"] == [
        {"WBSElement": "P-1-2", "PurchaseRequisitionAcctAssgmt": "01"}
    ]
    assert "WBSElement" not in item, "WBS kalemin uzerinde kalmamali"


def test_masraf_merkezi_kategorisi_k(settings_factory, tmp_path):
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1, cost_center="CC-100")]
    )
    item = draft.payload["items"][0]
    assert item["AccountAssignmentCategory"] == "K"
    assert item["_PurchaseReqnAcctAssgmt"][0]["CostCenter"] == "CC-100"


def test_hesap_atamasi_yoksa_kategori_gonderilmez(settings_factory, tmp_path):
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1)]
    )
    item = draft.payload["items"][0]
    assert "AccountAssignmentCategory" not in item
    assert "_PurchaseReqnAcctAssgmt" not in item


def test_bos_alanlar_payloada_girmez(settings_factory, tmp_path):
    """`"FixedSupplier": ""` Gateway dogrulamasinda gecersiz deger sayilabilir."""
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1)]
    )
    item = draft.payload["items"][0]
    assert "" not in item.values()
    assert "FixedSupplier" not in item


def test_miktar_ve_fiyat_sayi_olarak_gonderilir(settings_factory, tmp_path):
    """OData V4 JSON'da Edm.Decimal sayidir; string yalniz IEEE754Compatible ile."""
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=3)]
    )
    item = draft.payload["items"][0]
    assert isinstance(item["RequestedQuantity"], float)
    assert isinstance(item["PurchaseRequisitionPrice"], float)


# --- 2. Tedarikci adlari: V2 servise V4 batch gonderilmemeli ---------------
def test_tedarikci_adlari_v2_filter_ile_okunur(settings_factory, tmp_path):
    """`API_BUSINESS_PARTNER` V2'dir; JSON `$batch` gercek Gateway'de 400 doner."""
    backend, fake = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    records = backend.get_info_records("MAT-1")

    assert not fake.paths("$batch"), "V2 servise V4 $batch gonderildi"
    assert records and records[0].vendor_name == "Test Tedarikci"
    supplier_calls = fake.paths("A_Supplier")
    assert len(supplier_calls) == 1, "tedarikci adlari tek cagride okunmali"
    assert "Supplier eq 'V-1'" in supplier_calls[0].url.params.get("$filter", "")


# --- 3. Alan minimizasyonu ($select) ---------------------------------------
def test_tedarikci_okumasinda_vergi_ve_banka_alanlari_istenmez(
    settings_factory, tmp_path
):
    """En ucuz gizlilik kontrolu: gereksiz alani hic okumamak."""
    backend, fake = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    backend.get_vendor("V-1")
    select = fake.paths("A_Supplier")[0].url.params.get("$select", "")
    assert select, "$select verilmeden tum entity okunuyor"
    for forbidden in ("TaxNumber", "BankAccount", "StreetName"):
        assert forbidden not in select


@pytest.mark.parametrize(
    ("entity", "call"),
    [
        ("A_Product", lambda b: b.get_material("MAT-1")),
        ("A_MatlStkInAcctMod", lambda b: b.get_stock(["MAT-1"])),
        ("A_PurchasingInfoRecord", lambda b: b.get_info_records("MAT-1")),
    ],
)
def test_okumalar_select_ile_daraltilir(settings_factory, tmp_path, entity, call):
    backend, fake = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    call(backend)
    assert fake.paths(entity)[0].url.params.get("$select"), f"{entity} $select'siz okundu"


# --- 4. Toplu okuma (N+1 kaldirildi) ---------------------------------------
def test_cok_kalemli_talep_sabit_sayida_cagri_yapar(settings_factory, tmp_path):
    """Kalem sayisi arttikca SAP cagri sayisi artmamali."""
    routes = dict(BASE_ROUTES)
    routes["A_Product"] = [
        {**PRODUCT_ROW, "Product": f"MAT-{i}"} for i in range(1, 6)
    ]
    routes["A_PurchasingInfoRecord"] = [
        {**INFO_ROW, "Material": f"MAT-{i}"} for i in range(1, 6)
    ]
    backend, fake = build(settings_factory, tmp_path, routes=routes)
    items = [
        PurchaseRequisitionItem(material_id=f"MAT-{i}", quantity=1) for i in range(1, 6)
    ]
    backend.prepare_purchase_requisition(items)
    assert len(fake.paths("A_Product")) == 1
    assert len(fake.paths("A_PurchasingInfoRecord")) == 1
    assert len(fake.paths("A_MaterialValuation")) == 1


def test_coklu_malzeme_stogu_tek_cagride_okunur(settings_factory, tmp_path):
    backend, fake = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    backend.get_stock(["MAT-1", "MAT-2", "MAT-3"])
    assert len(fake.paths("A_MatlStkInAcctMod")) == 1
    filt = fake.paths("A_MatlStkInAcctMod")[0].url.params.get("$filter", "")
    assert "MAT-1" in filt and "MAT-3" in filt


# --- 5. V4 -> V2 fallback ---------------------------------------------------
def test_v4_yoksa_v2_servisine_dusulur(settings_factory, tmp_path):
    """`SAP_ODATA_VERSION=auto` iken V4 PR servisi yoksa V2 kullanilir."""
    backend, fake = build(
        settings_factory, tmp_path, routes=BASE_ROUTES,
        not_found=("api_purchaserequisition_2",),
    )
    assert backend._alias_for("purchase_requisition") == "purchase_requisition_v2"
    assert "API_PURCHASEREQ_PROCESS_SRV" in backend._alias_path("purchase_requisition")


def test_v4_zorlanirsa_fallback_yapilmaz(settings_factory, tmp_path):
    backend, _ = build(
        settings_factory, tmp_path, routes=BASE_ROUTES,
        not_found=("api_purchaserequisition_2",), **{"sap.odata_version": "v4"},
    )
    assert backend._alias_for("purchase_requisition") == "purchase_requisition"


def test_v2_zorlanirsa_v2_kullanilir(settings_factory, tmp_path):
    backend, _ = build(
        settings_factory, tmp_path, routes=BASE_ROUTES, **{"sap.odata_version": "v2"}
    )
    assert backend._alias_for("purchase_order") == "purchase_order_v2"


def test_metadata_cozumlenemezse_v4_tercihi_korunur(settings_factory, tmp_path):
    """Kanit yoksa deprecated servise DUSULMEZ; yalniz uyari verilir."""
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    assert backend._alias_for("purchase_requisition") == "purchase_requisition"


def test_v2_fallbackte_pr_deep_insert_v2_bicimindedir(settings_factory, tmp_path):
    backend, fake = build(
        settings_factory, tmp_path, routes=BASE_ROUTES,
        create={"PurchaseRequisition": "10000001"},
        not_found=("api_purchaserequisition_2",),
    )
    draft = backend.prepare_purchase_requisition(
        [
            PurchaseRequisitionItem(
                material_id="MAT-1", quantity=2, wbs_element="P-1",
                delivery_date=date.today() + timedelta(days=30),
            )
        ]
    )
    result = backend.submit_purchase_requisition(draft, external_reference="k:1")
    assert result.requisition_id == "10000001"
    body = fake.created[-1]
    assert "to_PurchaseReqnItem" in body
    item = body["to_PurchaseReqnItem"][0]
    assert isinstance(item["RequestedQuantity"], str), "V2 sayilari string tasir"
    assert item["DeliveryDate"].startswith("/Date("), "V2 tarih literali"
    assert item["to_PurchaseReqnAcctAssgmt"][0]["WBSElement"] == "P-1"


# --- 6. Egress: nextLink baska hosta gidemez -------------------------------
def test_allowlist_bossa_base_url_ortuk_allowlisttir():
    """Sunucudan gelen nextLink baska bir hosta yonlendiremez."""
    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.test"), allowed_hosts=()
    )
    core._assert_host_allowed("https://s4.test/sap/x")  # sorunsuz
    with pytest.raises(HostNotAllowed):
        core._assert_host_allowed("https://exfil.example/steal")


def test_allowlist_varsa_base_url_muaf_degildir():
    core = ODataHttpCore(
        client=httpx.Client(base_url="https://yanlis.test"),
        allowed_hosts=("s4.test",),
    )
    with pytest.raises(HostNotAllowed):
        core._assert_host_allowed("https://yanlis.test/sap/x")


# --- 7. Sayisal filtre enjeksiyonu -----------------------------------------
def test_sayisal_filtre_enjeksiyonu_reddedilir(settings_factory, tmp_path):
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    with pytest.raises(SAPError, match="INVALID_FILTER_VALUE|gecersiz"):
        backend.check_atp("MAT-1", quantity="1 or 1 eq 1")  # type: ignore[arg-type]


# --- 8. Yazma sekli okumadan turetilir --------------------------------------
_PR_META_CHILD = (
    "PurchaseRequisition:PurchaseRequisition,PurchaseRequisitionType|"
    "PurchaseRequisitionItem:PurchaseRequisition,PurchaseRequisitionItem,Material,"
    "Plant,RequestedQuantity,DeliveryDate,PurchaseRequisitionPrice,"
    "AccountAssignmentCategory"
)


def _edmx(spec: str, navigations: dict[str, list[tuple[str, str]]] | None = None) -> str:
    """`Set:alan,alan|Set:alan` bicimini EDMX'e cevirir."""
    navigations = navigations or {}
    types, sets = [], []
    for block in spec.split("|"):
        name, fields = block.split(":")
        props = "".join(f'<Property Name="{f}"/>' for f in fields.split(","))
        navs = "".join(
            f'<NavigationProperty Name="{n}" Type="Collection(x.{t})"/>'
            for n, t in navigations.get(name, [])
        )
        types.append(f'<EntityType Name="{name}Type">{props}{navs}</EntityType>')
        sets.append(f'<EntitySet Name="{name}" EntityType="x.{name}Type"/>')
    return (
        '<?xml version="1.0"?><edmx:Edmx Version="4.0" '
        'xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>'
        '<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="x">'
        + "".join(types)
        + f'<EntityContainer Name="c">{"".join(sets)}</EntityContainer>'
        "</Schema></edmx:DataServices></edmx:Edmx>"
    )


class MetaS4(FakeS4):
    """Belirtilen EDMX'i dondurup gerisini FakeS4 gibi davranan sahte sistem."""

    edmx = ""

    def _metadata(self) -> str:
        return self.edmx or super()._metadata()


def _build_with_metadata(settings_factory, tmp_path, edmx):
    import robotics_agent.sap.odata as mod

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
    fake = MetaS4(BASE_ROUTES)
    fake.edmx = edmx
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(fake.handler))
    original = mod.build_http_client
    mod.build_http_client = lambda connection, cfg: client  # noqa: ARG005
    try:
        backend = ODataSAPBackend(settings)
    finally:
        mod.build_http_client = original
    return backend, fake


def test_sozlesme_alt_entity_diyorsa_alt_entity_gonderilir(settings_factory, tmp_path):
    edmx = _edmx(
        _PR_META_CHILD + "|PurchaseReqnAcctAssgmt:WBSElement,CostCenter,"
        "PurchaseRequisitionAcctAssgmt",
        navigations={
            "PurchaseRequisition": [("_PurchaseRequisitionItem", "PurchaseRequisitionItemType")],
            "PurchaseRequisitionItem": [
                ("_PurchaseReqnAcctAssgmt", "PurchaseReqnAcctAssgmtType")
            ],
        },
    )
    backend, _ = _build_with_metadata(settings_factory, tmp_path, edmx)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1, wbs_element="P-9")]
    )
    item = draft.payload["items"][0]
    assert item["AccountAssignmentCategory"] == "P"
    assert item["_PurchaseReqnAcctAssgmt"][0]["WBSElement"] == "P-9"
    assert "WBSElement" not in item


def test_sozlesme_kalem_uzerinde_diyorsa_inline_gonderilir(settings_factory, tmp_path):
    """Bazi sistemlerde WBS dogrudan kalemdedir; kod sozlesmeye uyar."""
    edmx = _edmx(
        "PurchaseRequisition:PurchaseRequisition,PurchaseRequisitionType|"
        "PurchaseRequisitionItem:PurchaseRequisition,PurchaseRequisitionItem,Material,"
        "Plant,RequestedQuantity,WBSElement,CostCenter",
        navigations={
            "PurchaseRequisition": [("_PurchaseRequisitionItem", "PurchaseRequisitionItemType")]
        },
    )
    backend, _ = _build_with_metadata(settings_factory, tmp_path, edmx)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1, wbs_element="P-9")]
    )
    item = draft.payload["items"][0]
    assert item["WBSElement"] == "P-9"
    assert "_PurchaseReqnAcctAssgmt" not in item
    # Kategori alani bu sozlesmede yok -> gonderilmez.
    assert "AccountAssignmentCategory" not in item


def test_sozlesme_okunamazsa_released_sekil_varsayilir(settings_factory, tmp_path):
    """Kanit yoksa alan DUSURULMEZ; released API sekli varsayilir."""
    backend, _ = build(settings_factory, tmp_path, routes=BASE_ROUTES)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1, wbs_element="P-9")]
    )
    item = draft.payload["items"][0]
    assert item["AccountAssignmentCategory"] == "P"
    assert item["_PurchaseReqnAcctAssgmt"][0]["WBSElement"] == "P-9"


def test_uyumsuz_govde_sapa_gonderilmeden_reddedilir(settings_factory, tmp_path):
    """Yanlis alan adi, Gateway 400'u yerine acik bir mesaja donusur."""
    edmx = _edmx(
        "PurchaseRequisition:PurchaseRequisition,PurchaseRequisitionType|"
        "PurchaseRequisitionItem:PurchaseRequisition,Material",
        navigations={
            "PurchaseRequisition": [("_PurchaseRequisitionItem", "PurchaseRequisitionItemType")]
        },
    )
    backend, fake = _build_with_metadata(settings_factory, tmp_path, edmx)
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="MAT-1", quantity=1)]
    )
    with pytest.raises(SAPError) as exc:
        backend.submit_purchase_requisition(draft, external_reference="k:1")
    assert exc.value.code == "WRITE_SHAPE_MISMATCH"
    assert not [r for r in fake.requests if r.method == "POST"], "SAP'a POST gitti"
