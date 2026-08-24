"""Tenant profili: sirkete ozgu SAP gerceklerinin KOD DEGIL VERI oldugu.

Ayni SAP modulu her sirkette ayni sozlesmeye sahiptir; degisen yapilandirmadir.
Bu dosya sunu sabitler: yeni bir sirkete uyum saglamak icin **kod degismez**,
profil degisir.
"""

from __future__ import annotations

import json

import pytest

from robotics_agent.config import get_settings
from robotics_agent.contracts import ActorContext
from robotics_agent.core.profile_store import TenantProfileStore
from robotics_agent.core.store import get_state_db
from robotics_agent.core.tenant_profile import DEFAULT_DOCUMENT_TYPE, SapTenantProfile
from robotics_agent.sap import build_backend
from robotics_agent.sap.models import PurchaseRequisitionItem
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    load_all_tools()
    settings = get_settings(reload=True)
    settings.ensure_dirs()
    actor = ActorContext.local_operator(
        subject="ops@firma.test", tenant=settings.sap.tenant, roles=("PURCHASER", "APPROVER"),
        company_code=settings.sap.company_code, plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )
    yield settings, actor
    get_settings(reload=True)


# --- Model --------------------------------------------------------------
def test_profil_yoksa_sap_standardi_gecerlidir():
    """Profil yoklugu hata degil, gecerli bir durumdur."""
    profile = SapTenantProfile.default("100")

    assert profile.document_type == DEFAULT_DOCUMENT_TYPE
    assert profile.missing_required({}) == ()
    assert profile.describe()["source"] == "default"


def test_sifir_ve_false_eksik_sayilmaz():
    """0 gecerli bir is degeridir; "bos" ile karistirilmamali."""
    profile = SapTenantProfile(required_fields=("Quantity", "Flag"))

    assert profile.missing_required({"Quantity": 0, "Flag": False}) == ()
    assert profile.missing_required({"Quantity": "", "Flag": None}) == ("Quantity", "Flag")


def test_z_alan_eslemesi_uygulanir():
    """Z-alanlari tahmin edilemez; eslenmeleri bildirilmek zorundadir."""
    profile = SapTenantProfile(
        required_fields=("butce_kodu",),
        field_map=(("butce_kodu", "YY1_BudgetCode_PRI"),),
    )

    assert profile.sap_field("butce_kodu") == "YY1_BudgetCode_PRI"
    assert profile.missing_required({"YY1_BudgetCode_PRI": "B-1"}) == ()
    assert profile.missing_required({}) == ("butce_kodu",)


def test_varsayilanlar_mevcut_degeri_ezmez():
    profile = SapTenantProfile(defaults=(("AccountAssignmentCategory", "K"),))

    assert profile.apply_defaults({})["AccountAssignmentCategory"] == "K"
    assert profile.apply_defaults({"AccountAssignmentCategory": "F"})[
        "AccountAssignmentCategory"
    ] == "F"


# --- Depo ---------------------------------------------------------------
def test_profil_kaydedilir_ve_geri_okunur():
    store = TenantProfileStore(get_state_db(":memory:"))
    store.save(SapTenantProfile(tenant="100", document_type="ZNB", required_fields=("CostCenter",)))

    loaded = store.load("100")

    assert loaded.document_type == "ZNB"
    assert loaded.required_fields == ("CostCenter",)
    assert loaded.describe()["source"] == "profile"


def test_tenantsiz_profil_kaydedilemez():
    store = TenantProfileStore(get_state_db(":memory:"))
    with pytest.raises(ValueError):
        store.save(SapTenantProfile(document_type="ZNB"))


def test_ayni_red_tekrarlarsa_sayac_artar():
    """"Bir kez oldu" ile "her seferinde oluyor" ayrimi kurala yukseltme kararinin dayanagi."""
    store = TenantProfileStore(get_state_db(":memory:"))
    for _ in range(3):
        store.record_rejection(
            "100", tool="sap_pr_submit", sap_code="ME083",
            field="AccountAssignmentCategory", message="Enter account assignment category",
        )

    records = store.rejections("100")

    assert len(records) == 1
    assert records[0].seen_count == 3
    assert records[0].field == "AccountAssignmentCategory"


def test_red_otomatik_kurala_donusmez():
    """Tek seferlik bir SAP hatasi kalici kisitlama olmamali - karar insanindir."""
    store = TenantProfileStore(get_state_db(":memory:"))
    store.record_rejection("100", tool="sap_pr_submit", field="CostCenter", message="x")

    assert store.load("100").required_fields == (), "red kendiliginden kural olmamali"

    store.promote_to_required("100", "CostCenter")
    assert store.load("100").required_fields == ("CostCenter",)


# --- Uctan uca: kod degismeden davranis degisiyor -------------------------
def test_belge_tipi_profilden_gelir_kodda_sabit_degil(env):
    """Yeni bir sirkete uyum: ZNB kullaniyorsa kod degil profil degisir."""
    settings, _ = env
    backend = build_backend(settings)

    backend.set_active_profile(None)
    standart = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="SFT-SCN-270", quantity=1)], header_text="T"
    )
    assert standart.payload["PurchaseRequisitionType"] == "NB"

    backend.set_active_profile(SapTenantProfile(tenant="100", document_type="ZNB"))
    ozel = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="SFT-SCN-270", quantity=1)], header_text="T"
    )
    assert ozel.payload["PurchaseRequisitionType"] == "ZNB"


def test_eksik_zorunlu_alan_yazmadan_once_engellenir(env):
    """SAP reddetmeden ONCE durdurmak, reddi guzel gostermekten iyidir."""
    settings, actor = env
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=actor)
    ctx.profiles.save(
        SapTenantProfile(tenant=actor.tenant, required_fields=("AccountAssignmentCategory",))
    )

    raw, is_error = execute_tool(
        "sap_pr_prepare",
        {"items": [{"material_id": "SFT-SCN-270", "quantity": 1}]},
        ctx,
    )
    body = json.loads(raw)

    assert not is_error, "prepare bir teshis aracidir, patlamaz"
    assert body["submittable"] is False
    assert any("AccountAssignmentCategory" in m for m in body["blocking"])


def test_profil_yokken_davranis_degismez(env):
    """Geriye donuk uyumluluk: profil tanimlamayan kurulum bugunku gibi calisir."""
    settings, actor = env
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=actor)

    raw, is_error = execute_tool(
        "sap_pr_prepare",
        {"items": [{"material_id": "SFT-SCN-270", "quantity": 1}]},
        ctx,
    )
    body = json.loads(raw)

    assert not is_error
    assert body["submittable"] is True
    assert body["document_type"] == DEFAULT_DOCUMENT_TYPE
