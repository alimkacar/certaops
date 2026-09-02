"""SAP operasyon tool'larinin davranis ve sozlesme testleri.

Odak: ATP ile stok fotografinin ayrimi, MRP shortage aciklamasi, tedarikci
skorunda tahmin isaretleri, malzeme 360 veri bosluklari ve platform tool'lari.
"""

from __future__ import annotations

from datetime import date

import pytest

from robotics_agent.tools import load_all_tools


@pytest.fixture(autouse=True)
def _tools_loaded():
    load_all_tools()


# --- MRP arz/talep aciklamasi ----------------------------------------------
def test_mrp_explains_shortage_source(ctx, run_tool):
    result = run_tool("sap_mrp_shortage_explain", ctx, material_id="HD-GEAR-CSF25-100")
    assert result["has_shortage"] is True
    assert result["shortage_date"]
    assert result["max_shortage_qty"] > 0
    # Talep suruculeri gorunmeli: neden eksik sorusunun cevabi bu.
    assert result["top_demand_drivers"]
    elements = {row["element"] for row in result["timeline"]}
    assert "BE" in elements  # acik satinalma siparisi arz elementi
    assert {"SH", "VC"} & elements  # emniyet stogu / rezervasyon talebi


def test_mrp_timeline_is_cumulative(ctx, run_tool):
    result = run_tool("sap_mrp_shortage_explain", ctx, material_id="HD-GEAR-CSF25-100")
    timeline = result["timeline"]
    running = 0.0
    for row in timeline:
        running = round(running + row["quantity"], 3)
        assert row["cumulative"] == pytest.approx(running, abs=0.01)


def test_mrp_additional_demand_moves_shortage_earlier(ctx, run_tool):
    base = run_tool("sap_mrp_shortage_explain", ctx, material_id="SFT-SCN-270")
    with_demand = run_tool(
        "sap_mrp_shortage_explain",
        ctx,
        material_id="SFT-SCN-270",
        additional_demand=500,
        additional_demand_date=date.today().isoformat(),
    )
    assert with_demand["has_shortage"] is True
    if base["has_shortage"]:
        assert with_demand["max_shortage_qty"] >= base["max_shortage_qty"]
    assert with_demand["demand_total"] > base["demand_total"]


def test_mrp_reports_no_shortage_for_healthy_material(ctx, run_tool):
    result = run_tool("sap_mrp_shortage_explain", ctx, material_id="PLC-CPU-1516F")
    assert result["has_shortage"] is False
    assert "eksik olusmuyor" in result["interpretation"]


# --- Malzeme 360 -----------------------------------------------------------
def test_material_360_aggregates_views(ctx, run_tool):
    result = run_tool("sap_material_360", ctx, material_id="ROB-6AX-20-1800")
    assert result["description"]
    assert result["price"] > 0
    assert result["stock"]["unreserved"] is not None
    assert result["classification"]["characteristics"]["payload_kg"] == 20
    assert result["sources"]
    assert result["source_count"] >= 1
    assert "note" in result["stock"]  # ATP olmadigi hatirlatilmali


def test_material_360_reports_where_used(ctx, run_tool):
    result = run_tool("sap_material_360", ctx, material_id="HD-GEAR-CSF25-100")
    assert result["open_order_count"] >= 1
    assert "R-2026-014-1" in result["used_in_projects"]


def test_material_360_marks_single_source(ctx, run_tool):
    result = run_tool("sap_material_360", ctx, material_id="HD-GEAR-CSF25-100")
    assert result["single_source"] is True


def test_material_360_summary_hides_sources(ctx, run_tool):
    result = run_tool("sap_material_360", ctx, material_id="ROB-6AX-20-1800", detail="summary")
    assert "sources" not in result
    assert len(result["classification"]["characteristics"]) <= 6


def test_material_360_unknown_material_errors(ctx, run_tool):
    result = run_tool("sap_material_360", ctx, material_id="YOK-999", expect_error=True)
    assert "error" in result
    assert "sap_search_materials" in result["hint"]


# --- Tedarikci skoru -------------------------------------------------------
def test_supplier_score_360_reports_real_and_measured_data(ctx, run_tool):
    result = run_tool("sap_supplier_score_360", ctx, vendor_ids=["0010002", "0010004"])
    assert len(result["vendors"]) == 2
    top = result["vendors"][0]
    assert top["scores"]["overall_score"] > 0
    assert top["has_real_evaluation_data"] is True
    # Gerceklesen termin sapmasi PO verisinden olculur.
    with_variance = [v for v in result["vendors"] if v["measured_delivery_variance"]]
    assert with_variance, "Acik siparisi olan tedarikcide olculen sapma bekleniyor"
    variance = with_variance[0]["measured_delivery_variance"]
    assert variance["orders_evaluated"] >= 1
    assert "gerceklesen" in variance["source"]


def test_supplier_score_360_is_ranked(ctx, run_tool):
    result = run_tool(
        "sap_supplier_score_360", ctx, vendor_ids=["0010005", "0010004", "0010002"]
    )
    scores = [v["scores"]["overall_score"] for v in result["vendors"]]
    assert scores == sorted(scores, reverse=True)


def test_supplier_score_360_handles_unknown_vendor(ctx, run_tool):
    result = run_tool("sap_supplier_score_360", ctx, vendor_ids=["9999999"])
    assert result["vendors"][0]["error"]


# --- Stok fotografi ATP degil ---------------------------------------------
def test_stock_overview_declares_it_is_not_atp(ctx, run_tool):
    result = run_tool("sap_stock_overview", ctx, material_ids=["HD-GEAR-CSF25-100"])
    assert "ATP" in result["basis"] or "atp" in result["basis"]
    row = result["materials"][0]
    assert "unreserved" in row
    assert "available" not in row
    assert "stoktan karsilanabilir" in result["recommendation"]


def test_stock_overview_on_order_excludes_delivered(ctx, run_tool):
    """Acik siparis miktari daha once teslim edilen miktari dusmelidir."""
    result = run_tool("sap_stock_overview", ctx, material_ids=["SFT-SCN-270"])
    row = result["materials"][0]
    # Mock veride 20 siparis, 12 teslim -> 8 acik.
    assert row["on_order"] == pytest.approx(8.0)


# --- Platform tool'lari ----------------------------------------------------
def test_discover_capabilities_lists_manifest_and_backend_support(ctx, run_tool):
    result = run_tool("sap_discover_capabilities", ctx)
    assert result["backend"] == "mock"
    assert result["backend_capabilities"]["stock"] is True
    aliases = {entry["alias"] for entry in result["service_manifest"]}
    assert {"product", "stock", "mrp", "purchase_requisition"} <= aliases
    assert "released OData V4" in result["preferred_order"]


def test_discover_capabilities_probe_is_skipped_on_mock(ctx, run_tool):
    result = run_tool("sap_discover_capabilities", ctx, probe=True)
    assert result["probe"]["skipped"] is True


def test_connection_health_reports_guardrails(ctx, run_tool):
    result = run_tool("sap_connection_health", ctx)
    assert result["sap"]["status"] == "ok"
    assert "dry_run" in result["guardrails"]
    assert result["actor"]["subject"]
    # Allowlist tanimli degilse uyarilmali.
    assert any("allowlist" in w for w in result.get("warnings", []))


def test_reconcile_reports_not_found_for_unused_key(ctx, run_tool):
    result = run_tool("sap_reconcile_execution", ctx, idempotency_key="hic:kullanilmadi:v1")
    assert result["status"] == "not_found"
    assert "hic denenmemis" in result["conclusion"]


def test_reconcile_lists_pending(ctx, run_tool):
    result = run_tool("sap_reconcile_execution", ctx, list_pending=True)
    assert "pending_count" in result


def test_sap_list_domains_returns_capability_view(ctx, run_tool):
    result = run_tool("sap_list_domains", ctx)
    assert result["architecture"] == "certaops-single-runtime"
    assert {row["domain"] for row in result["domains"]} == {
        "platform", "master_data", "supply_chain", "procurement", "finance"
    }
    # Ayri calisan agent'lar VARMIS gibi bilgi vermez.
    assert "handoff_schema" not in result
    assert all("agent" not in row for row in result["domains"])


def test_sap_list_domains_previews_deterministic_routing(ctx, run_tool):
    result = run_tool(
        "sap_list_domains",
        ctx,
        message="HD-GEAR icin stok kontrol et ve satinalma talebi olustur",
    )
    assert result["routing_preview"]["domains"] == ["procurement"]
    assert "procurement_write" in result["routing_preview"]["packs"]


def test_authorization_failure_explanation_grants_nothing(ctx, run_tool):
    result = run_tool(
        "sap_explain_authorization_failure",
        ctx,
        http_status=403,
        sap_message="No authorization for purchase requisition",
        target_api="/sap/opu/odata4/sap/api_purchaserequisition_2",
    )
    assert result["likely_missing_authorizations"]
    assert any("M_BANF" in item for item in result["likely_missing_authorizations"])
    assert "yetki vermez" in result["note"]


def test_authorization_explanation_rejects_non_auth_status(ctx, run_tool):
    result = run_tool(
        "sap_explain_authorization_failure", ctx, http_status=500, expect_error=True
    )
    assert "error" in result


def test_evidence_handle_roundtrip_through_tool(ctx, run_tool):
    """Butce nedeniyle kirpilan sonucun tam kaydi get_evidence ile alinabilmeli."""
    stored = ctx.store_evidence(
        {"rows": [{"i": i} for i in range(50)]},
        tool="test",
        evidence=ctx.sap_evidence("test", record_count=50),
    )
    result = run_tool("get_evidence", ctx, evidence_id=stored)
    assert len(result["payload"]["rows"]) == 50


def test_get_evidence_rejects_unknown_handle(ctx, run_tool):
    result = run_tool("get_evidence", ctx, evidence_id="ev_yok", expect_error=True)
    assert result["denial_code"] == "EVIDENCE_NOT_FOUND"


def test_audit_tool_requires_audit_scope(ctx, run_tool):
    """ENGINEER audit okuyamaz: deny-by-default calisiyor."""
    result = run_tool("sap_get_execution_audit", ctx, expect_error=True)
    assert result["denial_code"] == "MISSING_SCOPE"


def test_audit_tool_returns_chain_for_auditor(settings, auditor, run_tool):
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext

    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=auditor)
    run_tool("sap_connection_health", ctx)
    result = run_tool("sap_get_execution_audit", ctx, verify_chain=True)
    assert result["entry_count"] >= 1
    assert result["chain_verification"]["valid"] is True


# --- Tool sozlesmesi ve timeout zorlamalari --------------------------------
def test_tool_timeout_is_enforced(settings, purchaser):
    """`timeout_s` yalnizca metadata degil: asan tool durdurulur."""
    import json
    import time

    from robotics_agent.contracts import RiskTier
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext, execute_tool
    from robotics_agent.tools.registry import REGISTRY, ToolSpec

    def slow_handler(ctx, **kwargs):  # noqa: ARG001
        time.sleep(2.0)
        return {"ok": True}

    REGISTRY["_test_slow_tool"] = ToolSpec(
        name="_test_slow_tool",
        description="Test amacli yavas tool." + " x" * 40,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=slow_handler,
        domain="platform",
        risk_tier=RiskTier.R0,
        required_scopes=(),
        timeout_s=0.2,
        org_scoped=False,
    )
    try:
        ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
        payload, is_error = execute_tool("_test_slow_tool", {}, ctx)
        result = json.loads(payload)
        assert is_error
        assert result["denial_code"] == "TOOL_TIMEOUT"
    finally:
        REGISTRY.pop("_test_slow_tool", None)


def test_mutating_tool_timeout_requires_review(settings, purchaser):
    """Yazma tool'unda timeout 'yazilmadi' anlamina gelmez."""
    import json
    import time

    from robotics_agent.contracts import SCOPE_PR_WRITE, RiskTier
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext, execute_tool
    from robotics_agent.tools.registry import REGISTRY, ToolSpec

    object.__setattr__(settings.sap, "read_only", False)

    def slow_write(ctx, **kwargs):  # noqa: ARG001
        time.sleep(2.0)
        return {"ok": True}

    REGISTRY["_test_slow_write"] = ToolSpec(
        name="_test_slow_write",
        description="Test amacli yavas yazma tool'u." + " x" * 40,
        input_schema={
            "type": "object",
            "properties": {"idempotency_key": {"type": "string"}},
            "required": ["idempotency_key"],
        },
        handler=slow_write,
        domain="procurement_write",
        risk_tier=RiskTier.R3,
        required_scopes=(SCOPE_PR_WRITE,),
        approval_policy="threshold",
        idempotent=True,
        timeout_s=0.2,
        org_scoped=False,
    )
    try:
        ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
        payload, is_error = execute_tool(
            "_test_slow_write", {"idempotency_key": "k:v1"}, ctx
        )
        result = json.loads(payload)
        assert is_error
        assert result["needs_review"] is True
        assert "sap_reconcile_execution" in result["remediation"]
    finally:
        REGISTRY.pop("_test_slow_write", None)


def test_confidential_results_are_masked_before_the_model(settings, purchaser):
    """data_classification=confidential sonucu modele verilmeden maskelenir."""
    import json

    from robotics_agent.contracts import RiskTier
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext, execute_tool
    from robotics_agent.tools.registry import REGISTRY, ToolSpec

    def leaky(ctx, **kwargs):  # noqa: ARG001
        return {"sap_password": "hunter2", "contact": "ali.veli@firma.com", "amount": 100}

    REGISTRY["_test_confidential"] = ToolSpec(
        name="_test_confidential",
        description="Test amacli gizli siniflandirmali tool." + " x" * 40,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=leaky,
        domain="platform",
        risk_tier=RiskTier.R0,
        required_scopes=(),
        data_classification="confidential",
        org_scoped=False,
    )
    try:
        ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
        payload, _ = execute_tool("_test_confidential", {}, ctx)
        result = json.loads(payload)
        assert result["sap_password"] == "***"
        assert "ali.veli@firma.com" not in payload
        assert result["amount"] == 100  # is verisi korunur
    finally:
        REGISTRY.pop("_test_confidential", None)


def test_removed_tools_are_not_registered():
    """Silinen tool'lar geri gelmemeli.

    `sap_list_agents` deprecated bir takma addi; `sap_validate_change` ise
    `sap_pr_prepare` ile ayni dogrulamayi yapiyordu. Modele ayni isi yapan
    iki tool gostermek yanlis secim uretir.
    """
    from robotics_agent.tools.registry import REGISTRY

    assert "sap_list_agents" not in REGISTRY
    assert "sap_validate_change" not in REGISTRY
    assert "sap_list_domains" in REGISTRY, "yerine gecen tool durmali"
    assert "sap_pr_prepare" in REGISTRY, "dogrulamayi devralan tool durmali"
