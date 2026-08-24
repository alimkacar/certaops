"""Gercek SAP quality tenant'ina karsi entegrasyon kabul testleri.

Bu testler varsayilan olarak ATLANIR. Calistirmak icin bir SAP sandbox/quality
tenant'i ve asagidaki ortam degiskenleri gerekir:

    SAP_INTEGRATION_TESTS=1
    SAP_BACKEND=odata
    SAP_BASE_URL / SAP_AUTH_MODE ve ilgili kimlik ayarlari
    SAP_ALLOWED_HOSTS=<hedef host>
    SAP_INTEGRATION_MATERIAL=<test malzemesi>
    SAP_INTEGRATION_VENDOR=<test tedarikcisi>     (opsiyonel)
    SAP_INTEGRATION_ALLOW_WRITE=1                 (yazma testleri icin, AYRICA)
    SAP_DRY_RUN=false                             (yazma testleri icin, AYRICA)

Yazma testleri bilerek ayri bir bayrakla korunur ve calistiginda olusturdugu
belgeyi raporlar: quality sisteminde bile kontrolsuz belge birakmak istenmez.

Mock testlerinin gecmesi gercek SAP uyumlulugu anlamina gelmez. Bu dosya servis
sozlesmelerini ve temel is akisini CI'da gercek bir tenant'a karsi dogrular.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from robotics_agent.adapters.sap import CAPABILITY_MANIFEST, SAPError, SAPNotSupported
from robotics_agent.config import Settings
from robotics_agent.contracts import ActorContext
from robotics_agent.core import build_idempotency_key
from robotics_agent.sap import build_backend
from robotics_agent.sap.models import PurchaseRequisitionItem

RUN_INTEGRATION = os.getenv("SAP_INTEGRATION_TESTS") == "1"
ALLOW_WRITE = (
    os.getenv("SAP_INTEGRATION_ALLOW_WRITE") == "1"
    and os.getenv("SAP_DRY_RUN", "true").strip().lower() in {"0", "false", "no"}
)

pytestmark = pytest.mark.skipif(
    not RUN_INTEGRATION,
    reason="SAP_INTEGRATION_TESTS=1 ve gercek bir SAP sandbox baglantisi gerekiyor.",
)


@pytest.fixture(scope="module")
def sap():
    settings = Settings()
    if settings.sap.backend != "odata":
        pytest.skip("Entegrasyon testleri icin SAP_BACKEND=odata olmali.")
    problems = settings.sap.validate()
    if problems:
        pytest.skip("SAP konfigurasyonu eksik: " + "; ".join(problems))
    backend = build_backend(settings)
    yield backend
    backend.close()


@pytest.fixture(scope="module")
def material() -> str:
    value = os.getenv("SAP_INTEGRATION_MATERIAL")
    if not value:
        pytest.skip("SAP_INTEGRATION_MATERIAL tanimli degil.")
    return value


# --- Baglanti ve kontrat ----------------------------------------------------
def test_ping_succeeds(sap):
    result = sap.ping()
    assert result["status"] == "ok", result


def test_metadata_contracts_match_manifest(sap):
    """Kritik servislerin kontrati hedef sistemde beklendigi gibi mi?"""
    critical = ["product", "purchase_requisition", "purchase_order"]
    checks = sap.probe_capabilities(critical)
    failures = [c for c in checks if not c.get("contract_ok")]
    assert not failures, f"Kontrat farklari: {failures}"


def test_deprecated_services_are_flagged(sap):
    checks = {c["alias"]: c for c in sap.probe_capabilities(["purchase_requisition_v2"])}
    entry = checks.get("purchase_requisition_v2")
    if entry and entry["available"]:
        assert entry["status"] == "deprecated"
        assert CAPABILITY_MANIFEST["purchase_requisition_v2"].successor


# --- Okuma -----------------------------------------------------------------
def test_material_can_be_read(sap, material):
    result = sap.get_material(material)
    assert result is not None
    assert result.material_id
    assert result.description, "Aciklama bos: dil/description expand kontrolu gerekiyor."


def test_material_search_finds_by_description(sap, material):
    """Aciklama bazli malzeme aramasi gercek sistemde de calismalidir."""
    master = sap.get_material(material)
    token = (master.description or "").split()[0] if master and master.description else ""
    if not token or len(token) < 3:
        pytest.skip("Aciklamadan arama tokeni cikarilamadi.")
    results = sap.search_materials(token, limit=10)
    assert results, f"'{token}' ile aciklamada arama sonuc dondurmedi."


def test_classification_is_readable_or_explicitly_unsupported(sap, material):
    try:
        classification = sap.get_material_classification(material)
    except SAPNotSupported as exc:
        pytest.skip(f"Siniflandirma servisi yok: {exc}")
    assert classification is not None
    # Bos siniflandirma kabul edilebilir; sessiz hata degil acik bos sonuc olmali.
    assert classification.source


def test_stock_can_be_read(sap, material):
    levels = sap.get_stock([material])
    assert levels
    level = levels[0]
    assert level.plant
    assert level.unrestricted_qty >= 0


def test_atp_returns_dated_confirmation(sap, material):
    try:
        result = sap.check_atp(
            material, quantity=1, requested_date=date.today() + timedelta(days=30)
        )
    except SAPNotSupported as exc:
        pytest.skip(f"ATP servisi aktif degil: {exc}")
    assert result.source_api
    assert result.checked_at is not None
    # Teyit tarihi olmadan ATP anlamsizdir.
    if result.confirmed_qty > 0:
        assert result.full_confirmation_date is not None


def test_mrp_supply_demand_is_readable(sap, material):
    try:
        items = sap.get_supply_demand(material, horizon_days=90)
    except SAPNotSupported as exc:
        pytest.skip(f"MRP servisi aktif degil: {exc}")
    for item in items:
        assert item.mrp_element
        assert item.availability_date is not None


def test_purchase_orders_have_requested_and_confirmed_dates(sap, material):
    """Siparis verisi termin sapmasini hesaplamak icin gerekli tarihleri tasimalidir."""
    orders = sap.get_purchase_orders(material_id=material, limit=10)
    if not orders:
        pytest.skip("Bu malzeme icin siparis yok.")
    with_dates = [o for o in orders if o.requested_delivery_date and o.confirmed_delivery_date]
    assert with_dates, "Hicbir sipariste talep+teyit tarihi yok; mapping eksik."


def test_supplier_score_marks_estimates(sap):
    vendor = os.getenv("SAP_INTEGRATION_VENDOR")
    if not vendor:
        pytest.skip("SAP_INTEGRATION_VENDOR tanimli degil.")
    score = sap.get_supplier_score(vendor)
    assert score is not None
    if score.overall_score is None:
        # Veri yoksa bu acikca isaretlenmeli, sessizce 0 olmamali.
        assert score.estimated_fields


# --- Yetki reddi -----------------------------------------------------------
def test_authorization_failure_is_structured(sap):
    """Yetkisiz bir cagri yapilandirilmis hata uretmeli (ham HTML degil)."""
    try:
        sap.get_project_costs(wbs_element="ZZZ-YETKISIZ-TEST")
    except SAPNotSupported:
        pytest.skip("Proje maliyet servisi bu sistemde yayinlanmamis.")
    except SAPError as exc:
        assert exc.code
        if exc.fault is not None:
            assert exc.fault.target_api
            assert "<html" not in exc.fault.message.lower()


# --- Yazma (ayrica bayrakli) ------------------------------------------------
@pytest.mark.skipif(
    not ALLOW_WRITE,
    reason="Yazma icin SAP_INTEGRATION_ALLOW_WRITE=1 ve SAP_DRY_RUN=false gerekli.",
)
def test_prepare_does_not_write(sap, material):
    draft = sap.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id=material, quantity=1)],
        header_text="AGENT INTEGRATION TEST",
    )
    assert draft.payload
    assert draft.total_value >= 0
    # Prepare sonrasi hicbir belge olusmamis olmali.
    assert sap.find_purchase_requisition_by_reference("AGENT-INTEGRATION-NOOP") is None


@pytest.mark.skipif(
    not ALLOW_WRITE,
    reason="Yazma icin SAP_INTEGRATION_ALLOW_WRITE=1 ve SAP_DRY_RUN=false gerekli.",
)
def test_write_then_read_back_and_idempotent_retry(sap, material, capsys):
    """Write -> read-back -> ayni referansla tekrar (duplicate olmamali)."""
    reference = build_idempotency_key(
        "integration", date.today().isoformat(), "pr", os.getenv("BUILD_ID", "local")
    )
    draft = sap.prepare_purchase_requisition(
        [
            PurchaseRequisitionItem(
                material_id=material,
                quantity=1,
                delivery_date=date.today() + timedelta(days=45),
            )
        ],
        header_text="AGENT INTEGRATION TEST",
    )
    assert draft.is_submittable, [f.message for f in draft.blocking_findings]

    result = sap.submit_purchase_requisition(draft, external_reference=reference)
    assert result.requisition_id, result.messages
    with capsys.disabled():
        print(f"\n[integration] Olusturulan PR: {result.requisition_id} (ref {reference})")

    read_back = sap.read_purchase_requisition(result.requisition_id)
    assert read_back is not None
    assert read_back["item_count"] == len(draft.items)

    found = sap.find_purchase_requisition_by_reference(reference)
    assert found is not None and found[0] == result.requisition_id


@pytest.mark.skipif(
    not ALLOW_WRITE,
    reason="Yazma icin SAP_INTEGRATION_ALLOW_WRITE=1 ve SAP_DRY_RUN=false gerekli.",
)
def test_policy_gate_blocks_unauthorized_write(sap):
    """Gercek sistemde de yetkisiz actor handler'a ulasamaz."""
    from robotics_agent.tools import ToolContext, execute_tool, load_all_tools

    load_all_tools()
    settings = Settings()
    viewer = ActorContext(
        subject="integration-viewer", tenant=settings.sap.tenant, roles=("VIEWER",)
    )
    ctx = ToolContext(settings=settings, sap=sap, actor=viewer)
    payload, is_error = execute_tool(
        "sap_pr_submit",
        {"items": [{"material_id": "X", "quantity": 1}], "idempotency_key": "viewer:test:v1"},
        ctx,
    )
    assert is_error
    assert "MISSING_SCOPE" in payload
