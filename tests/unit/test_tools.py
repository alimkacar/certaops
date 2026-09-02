"""SAP-only tool registry ve temel is kurali testleri."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from robotics_agent.tools import execute_tool, load_all_tools
from robotics_agent.tools.registry import REGISTRY


@pytest.fixture(autouse=True)
def _tools_loaded():
    load_all_tools()


def run(name: str, ctx, **kwargs) -> dict:
    payload, is_error = execute_tool(name, kwargs, ctx)
    assert not is_error, f"{name} hata dondurdu: {payload}"
    return json.loads(payload)


def test_registry_contains_only_sap_operations():
    expected = {
        "sap_discover_capabilities",
        "sap_connection_health",
        "sap_explain_authorization_failure",
        "sap_get_execution_audit",
        "sap_reconcile_execution",
        "get_evidence",
        "sap_search_materials",
        "sap_material_360",
        "sap_stock_overview",
        "sap_mrp_shortage_explain",
        "sap_compare_vendors",
        "sap_supplier_score_360",
        "sap_pr_prepare",
        "sap_pr_submit",
        "sap_track_purchase_orders",
        "sap_generate_report",
        # Salt-okunur procure-to-pay belge ve onay gorunurlugu.
        "sap_document_flow",
        "sap_purchase_order_360",
        "sap_supplier_invoice_status",
        "sap_invoice_block_explain",
        "sap_list_domains",
    }
    assert set(REGISTRY) == expected
    removed = {
        "analyze_technical_spec",
        "check_standards_compliance",
        "propose_solution_architecture",
        "run_engineering_calculation",
        "estimate_project_cost",
        "forecast_budget",
        "select_domain_pack",
    }
    assert not (removed & set(REGISTRY))


def test_every_tool_schema_and_risk_contract_is_valid():
    for spec in REGISTRY.values():
        schema = spec.input_schema
        assert schema["type"] == "object", spec.name
        assert "properties" in schema, spec.name
        for required in schema.get("required", []):
            assert required in schema["properties"], f"{spec.name}: {required}"
        assert len(spec.description) > 60
        assert spec.result_token_budget > 0
        if spec.risk_tier.is_mutating:
            assert spec.required_scopes
            assert spec.approval_policy != "none"
            assert spec.idempotent


def test_search_materials_by_sap_classification(ctx):
    result = run(
        "sap_search_materials",
        ctx,
        material_group="R100",
        attribute_filters={"payload_kg": [15, 30], "reach_mm": [1600, 2000]},
    )
    assert result["result_count"] >= 1
    for material in result["materials"]:
        assert 15 <= material["attributes"]["payload_kg"] <= 30


def test_stock_overview_is_not_atp(ctx):
    result = run(
        "sap_stock_overview",
        ctx,
        material_ids=["HD-GEAR-CSF25-100", "YOK-123"],
        required_quantities={"HD-GEAR-CSF25-100": 30},
        required_date="2026-09-30",
    )
    assert "YOK-123" in result["not_found"]
    assert all(row["material_id"] != "YOK-123" for row in result["materials"])
    gear = result["materials"][0]
    assert gear["shortfall"] > 0
    assert "unreserved" in gear and "available" not in gear
    assert "ATP" in result["basis"] or "atp" in result["basis"].lower()


def test_stock_overview_kucuk_harfli_id_celiskili_raporlanmaz(ctx):
    result = run("sap_stock_overview", ctx, material_ids=["hd-gear-csf25-100"])

    returned = {row["material_id"] for row in result["materials"]}
    assert not (returned & set(result["not_found"]))
    assert returned or result["not_found"]


def test_vendor_comparison_is_tco_ranked(ctx):
    result = run("sap_compare_vendors", ctx, material_id="ROB-6AX-20-1800", quantity=5)
    tcos = [row["total_cost_of_ownership"] for row in result["candidates"]]
    assert tcos == sorted(tcos)
    assert any(row["quality_cost"] > 0 for row in result["candidates"])


def test_pr_prepare_validates_without_writing(ctx):
    result = run(
        "sap_pr_prepare",
        ctx,
        items=[{"material_id": "SFT-SCN-270", "quantity": 1, "delivery_date": "2026-08-05"}],
        header_text="Test talebi",
    )
    assert result["written_to_sap"] is False
    assert result["payload_sha256"]
    assert result["diff"]
    assert result["findings"]


def test_purchase_order_tracking_flags_delay(ctx):
    result = run("sap_track_purchase_orders", ctx, only_open=True)
    assert result["order_count"] >= 5
    assert result["delayed_open_value"] > 0
    gear = next(row for row in result["orders"] if row["po_id"] == "4500018821")
    assert gear["delay_days"] == 43


def test_sap_report_creates_provenance_files(ctx):
    result = run(
        "sap_generate_report",
        ctx,
        title="SAP WBS Ozeti",
        format="both",
        executive_summary="SAP PS maliyet durumu.",
        source_references=["S4-MOCK:project_cost", "WBS:R-2026-014"],
        tables=[
            {
                "name": "WBS",
                "columns": ["WBS", "Plan", "Fiili"],
                "rows": [["R-2026-014-1", 100000, 80000]],
            }
        ],
        filename="sap_wbs_test",
    )
    assert result["source_system"] == ctx.settings.sap.system_alias
    assert len(result["created_files"]) == 2
    for created in result["created_files"]:
        assert Path(created).exists()


def test_sap_report_handles_duplicate_sheet_names(ctx):
    result = run(
        "sap_generate_report",
        ctx,
        title="Coklu SAP Tablo",
        format="xlsx",
        tables=[
            {"name": "Tablo", "columns": ["A"], "rows": [[1]]},
            {"name": "Tablo", "columns": ["B"], "rows": [[2]]},
        ],
        filename="sap_duplicate",
    )
    assert Path(result["created_files"][0]).exists()


def test_sap_report_metni_excel_formulu_veya_url_yapmaz(ctx):
    result = run(
        "sap_generate_report",
        ctx,
        title="Guvenli Excel",
        format="xlsx",
        tables=[{
            "name": "Veri",
            "columns": ["Deger"],
            "rows": [["=1+1"], ["https://example.invalid/click"]],
        }],
        filename="sap_safe_strings",
    )

    with zipfile.ZipFile(result["created_files"][0]) as archive:
        worksheets = "".join(
            archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        assert "<f>" not in worksheets
        assert "<hyperlink" not in worksheets
