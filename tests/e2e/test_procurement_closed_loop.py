"""Uctan uca guvenli satinalma kapali cevrimi kabul testi.

Akis:
    malzeme ana verisi -> MRP shortage -> tedarikci skoru ->
    PR prepare (yazmaz) -> onay -> PR submit (idempotent) -> read-after-write ->
    audit zinciri

Model cagrilmaz: tool katmani deterministik oldugu icin akis Claude API anahtari
olmadan da uctan uca dogrulanabilir.
"""

from __future__ import annotations

import pytest

from robotics_agent.contracts import ActorContext
from robotics_agent.core import build_idempotency_key
from robotics_agent.sap import build_backend
from robotics_agent.tools import ToolContext, load_all_tools


@pytest.fixture(autouse=True)
def _tools_loaded():
    load_all_tools()


@pytest.fixture
def writer_ctx(settings, purchaser):
    """Gercek yazma acik (SAP_DRY_RUN=false) satinalmaci baglami."""
    object.__setattr__(settings.sap, "read_only", False)
    object.__setattr__(settings.sap, "dry_run", False)
    return ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)


PROJECT = "R-2026-021-1"


def test_sap_demand_to_verified_requisition(writer_ctx, run_tool, grant_approval):
    ctx = writer_ctx

    # --- 1. SAP ana verisinden is nesnesini dogrula
    material_id = "HD-GEAR-CSF25-100"
    material = run_tool("sap_material_360", ctx, material_id=material_id)
    assert material["material_id"] == material_id
    assert material["price"] > 0

    # --- 3. Eksigin kaynagini SAP arz/talep elementlerinden acikla
    shortage = run_tool("sap_mrp_shortage_explain", ctx, material_id=material_id)
    assert shortage["interpretation"]

    # --- 5. Tedarikci degerlendirmesi
    vendors = run_tool("sap_compare_vendors", ctx, material_id=material_id, quantity=30)
    best_vendor = vendors["recommendation"]["best_tco_vendor"]
    scores = run_tool("sap_supplier_score_360", ctx, vendor_ids=[best_vendor])
    assert scores["vendors"][0]["vendor_id"] == best_vendor

    # --- 6. PR taslagi: hicbir sey yazilmaz
    items = [
        {
            "material_id": material_id,
            "quantity": 30,
            "delivery_date": "2026-11-30",
            "preferred_vendor": best_vendor,
            "wbs_element": PROJECT,
        }
    ]
    prepared = run_tool(
        "sap_pr_prepare", ctx, items=items, header_text="MRP eksigi icin planli talep"
    )
    assert prepared["written_to_sap"] is False
    assert prepared["diff"]
    assert prepared["requires_human_approval"] is True  # tutar esigin uzerinde
    assert prepared["approval_task"]["status"] == "pending"

    # --- 7. Onaysiz submit reddedilmeli
    key = build_idempotency_key(PROJECT, "mrp-eksigi", "pr", "v1")
    denied = run_tool(
        "sap_pr_submit",
        ctx,
        items=items,
        header_text="MRP eksigi icin planli talep",
        idempotency_key=key,
        expect_error=True,
    )
    assert denied["written_to_sap"] is False
    assert denied["denial_code"] == "APPROVAL_SCOPE_EXCEEDED"

    # --- 8. Yetkili onay ve submit
    approval_id = grant_approval(
        ctx,
        tool="sap_pr_submit",
        arguments={"items": items, "header_text": "MRP eksigi icin planli talep"},
        max_value=prepared["total_value"] * 1.02,
    )
    created = run_tool(
        "sap_pr_submit",
        ctx,
        items=items,
        header_text="MRP eksigi icin planli talep",
        idempotency_key=key,
        approval_id=approval_id,
    )
    assert created["write_status"] == "created"
    assert created["business_object_id"]
    # --- 9. Read-after-write dogrulamasi
    assert created["verified"] is True
    assert created["verification"]["verified"] is True

    # --- 10. Ayni cagriyi tekrar denemek: tuketilmis onay yeniden yetki vermez.
    # Policy kapisi handler'dan once calistigi icin cagri handler'a hic ulasmaz;
    # dogru cikis yolu yeni onay degil mutabakattir.
    again = run_tool(
        "sap_pr_submit",
        ctx,
        items=items,
        header_text="MRP eksigi icin planli talep",
        idempotency_key=key,
        approval_id=approval_id,
        expect_error=True,
    )
    assert again["denial_code"] == "APPROVAL_CONSUMED"
    assert "sap_reconcile_execution" in again["remediation"]

    # --- 11. Mutabakat: belgenin olustugunu ve tekrar yazma gerekmedigini gosterir
    reconciled = run_tool("sap_reconcile_execution", ctx, idempotency_key=key)
    assert reconciled["status"] == "completed"
    assert reconciled["business_object_id"] == created["business_object_id"]
    assert reconciled["safe_to_retry"] is False

    # --- 12. Farkli bir anahtarla ayni onayi kullanmak da reddedilir (replay)
    replay = run_tool(
        "sap_pr_submit",
        ctx,
        items=items,
        header_text="MRP eksigi icin planli talep",
        idempotency_key=build_idempotency_key(PROJECT, "mrp-eksigi", "pr", "v2"),
        approval_id=approval_id,
        expect_error=True,
    )
    assert replay["denial_code"] == "APPROVAL_CONSUMED"

    # --- 13. Audit zinciri: actor, policy ve dogrulama izlenebilir
    # Denetci kendi kimligiyle calisir; incelenen islem execution_id ile verilir.
    # (Execution baglami actor'u tasir, bu yuzden odunc alinamaz.)
    audited_execution_id = ctx.execution.execution_id
    auditor_ctx = ToolContext(
        settings=ctx.settings,
        sap=ctx.sap,
        actor=ActorContext(subject="denetci@firma.test", tenant="100", roles=("AUDITOR",)),
    )
    audit = run_tool(
        "sap_get_execution_audit",
        auditor_ctx,
        execution_id=audited_execution_id,
        verify_chain=True,
        limit=100,
    )
    assert audit["chain_verification"]["valid"] is True
    events = {entry["event"] for entry in audit["entries"]}
    assert "tool.policy_decision" in events
    assert "write.completed" in events


def test_below_threshold_retry_is_duplicate_prevented(writer_ctx, run_tool):
    """Onay gerekmeyen tutarda ayni anahtarla tekrar cagri yeni belge uretmez."""
    ctx = writer_ctx
    items = [{"material_id": "SFT-SCN-270", "quantity": 4, "wbs_element": PROJECT}]
    key = build_idempotency_key(PROJECT, "tarayici-esik-alti", "pr", "v1")

    prepared = run_tool("sap_pr_prepare", ctx, items=items)
    assert prepared["requires_human_approval"] is False

    first = run_tool("sap_pr_submit", ctx, items=items, idempotency_key=key)
    assert first["write_status"] == "created"
    assert first["verified"] is True

    second = run_tool("sap_pr_submit", ctx, items=items, idempotency_key=key)
    assert second["write_status"] == "duplicate_prevented"
    assert second["business_object_id"] == first["business_object_id"]


def test_changing_items_after_approval_invalidates_it(writer_ctx, run_tool, grant_approval):
    """Onay is icerigine baglidir: miktar degisirse onay gecersizdir."""
    ctx = writer_ctx
    items = [{"material_id": "ROB-6AX-20-1800", "quantity": 2}]
    approval_id = grant_approval(ctx, tool="sap_pr_submit", arguments={"items": items})

    tampered = [{"material_id": "ROB-6AX-20-1800", "quantity": 5}]
    denied = run_tool(
        "sap_pr_submit",
        ctx,
        items=tampered,
        idempotency_key="tamper:pr:v1",
        approval_id=approval_id,
        expect_error=True,
    )
    assert denied["denial_code"] == "APPROVAL_INVALID"


def test_timeout_is_reconciled_without_duplicate(writer_ctx, run_tool, grant_approval, monkeypatch):
    """Yazma cagrisi kesilirse: tekrar POST degil, read-back ve mutabakat."""
    import httpx

    ctx = writer_ctx
    items = [{"material_id": "SFT-SCN-270", "quantity": 4, "wbs_element": PROJECT}]
    key = build_idempotency_key(PROJECT, "tarayici", "pr", "v1")

    real_submit = ctx.sap.submit_purchase_requisition
    state = {"calls": 0}

    def flaky_submit(draft, *, external_reference, correlation_id=""):
        state["calls"] += 1
        if state["calls"] == 1:
            # SAP belgeyi olusturur ama yanit istemciye ulasmaz.
            real_submit(draft, external_reference=external_reference, correlation_id=correlation_id)
            raise httpx.TimeoutException("baglanti koptu")
        raise AssertionError("Timeout sonrasi tekrar POST edilmemeliydi")

    monkeypatch.setattr(ctx.sap, "submit_purchase_requisition", flaky_submit)

    result = run_tool(
        "sap_pr_submit", ctx, items=items, idempotency_key=key, expect_error=False
    )
    # Guard read-back yapip mutabakat kurmali.
    assert result["write_status"] == "reconciled"
    assert result["business_object_id"]
    assert state["calls"] == 1

    # Mutabakat tool'u da ayni sonucu bildirmeli.
    reconciled = run_tool("sap_reconcile_execution", ctx, idempotency_key=key)
    assert reconciled["status"] == "completed"
    assert reconciled["safe_to_retry"] is not True


def test_unknown_outcome_requires_review_when_nothing_was_created(
    writer_ctx, run_tool, monkeypatch
):
    """Belge olusmadiysa: needs_review + kontrollu tekrar izni."""
    import httpx

    ctx = writer_ctx
    items = [{"material_id": "SFT-LC-1200", "quantity": 2, "wbs_element": PROJECT}]
    key = build_idempotency_key(PROJECT, "isik-perdesi", "pr", "v1")

    def timeout_submit(draft, *, external_reference, correlation_id=""):
        raise httpx.TimeoutException("baglanti koptu")

    monkeypatch.setattr(ctx.sap, "submit_purchase_requisition", timeout_submit)

    result = run_tool("sap_pr_submit", ctx, items=items, idempotency_key=key)
    assert result["write_status"] == "needs_review"
    assert result["needs_review"] is True
    assert "sap_reconcile_execution" in result["remediation"]

    reconciled = run_tool("sap_reconcile_execution", ctx, idempotency_key=key)
    assert reconciled["safe_to_retry"] is True
    assert "kontrollu tekrar" in reconciled["conclusion"]


def test_dry_run_environment_blocks_real_write(settings, purchaser, run_tool, grant_approval):
    """SAP_DRY_RUN=true iken onay gecse bile SAP'a yazilmaz."""
    object.__setattr__(settings.sap, "read_only", False)
    object.__setattr__(settings.sap, "dry_run", True)
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    items = [{"material_id": "SFT-SCN-270", "quantity": 4, "wbs_element": PROJECT}]

    result = run_tool(
        "sap_pr_submit",
        ctx,
        items=items,
        idempotency_key=build_idempotency_key(PROJECT, "dry", "pr", "v1"),
    )
    assert result["write_status"] == "simulated"
    assert result["written_to_sap"] is False
    assert any("SAP_DRY_RUN" in message for message in result["messages"])
    # Hicbir belge olusmamis olmali.
    assert ctx.sap.find_purchase_requisition_by_reference(
        build_idempotency_key(PROJECT, "dry", "pr", "v1")
    ) is None


def test_engineer_cannot_submit_requisition(ctx, run_tool):
    """Rol ayrimi: muhendis taslak hazirlar, yazamaz."""
    items = [{"material_id": "SFT-SCN-270", "quantity": 4}]
    prepared = run_tool("sap_pr_prepare", ctx, items=items)
    assert prepared["written_to_sap"] is False

    denied = run_tool(
        "sap_pr_submit",
        ctx,
        items=items,
        idempotency_key="eng:test:pr:v1",
        expect_error=True,
    )
    assert denied["denial_code"] == "MISSING_SCOPE"
    assert "sap.pr.write" in denied["missing_scopes"]
