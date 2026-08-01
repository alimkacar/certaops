"""Salt-okunur procure-to-pay gorunurluk tool'larinin kabul testleri.

Kontrol edilen kabul kriterleri:
  - Tek bir business object ID ile iliskili belgeler bulunabilir.
  - Belge baglari uydurulmaz; her bag SAP referansi/evidence tasir.
  - Yetkisiz sirket kodu, tesis veya satinalma organizasyonu sonucu donmez.
  - Varsayilan sonuc summary ve en fazla 1.200 token'dir.
  - Document flow sorgusu N+1 uretmez.
"""

from __future__ import annotations

import pytest

from robotics_agent.contracts import ActorContext, estimate_tokens
from robotics_agent.sap import build_backend
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools
from robotics_agent.tools.registry import REGISTRY

P2P_TOOLS = (
    "sap_document_flow",
    "sap_purchase_order_360",
    "sap_workflow_status",
    "sap_supplier_invoice_status",
    "sap_invoice_block_explain",
)


@pytest.fixture(autouse=True)
def _clean_cache():
    from robotics_agent.cache import reset_tool_cache

    reset_tool_cache()
    yield
    reset_tool_cache()


@pytest.fixture
def p2p_ctx(settings, purchaser) -> ToolContext:
    load_all_tools()
    return ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)


# --- Kayit sozlesmesi ------------------------------------------------------
def test_all_p2p_tools_are_registered_read_only():
    load_all_tools()
    for name in P2P_TOOLS:
        spec = REGISTRY[name]
        assert not spec.risk_tier.is_mutating, name
        assert spec.approval_policy == "none", name
        assert spec.impact_profile is not None and not spec.impact_profile.is_mutating, name
        # Her tool bir performans butcesi ve cache politikasi bildirmis olmali.
        assert spec.performance_budget.max_sap_calls >= 1, name
        assert spec.cache_policy.enabled, name


def test_p2p_tools_declare_field_level_data_policy():
    load_all_tools()
    for name in P2P_TOOLS:
        policy = REGISTRY[name].data_policy
        assert policy is not None and policy.fields, name
        assert policy.export_scope, name
        assert not policy.validate(), name


# --- Belge akisi -----------------------------------------------------------
def test_document_flow_resolves_chain_from_any_document(p2p_ctx, run_tool):
    """Ayni zincir hem PR hem PO hem fatura numarasindan bulunabilir."""
    from_pr = run_tool("sap_document_flow", p2p_ctx, document_id="0010004690")
    from_po = run_tool("sap_document_flow", p2p_ctx, document_id="4500019014")
    from_invoice = run_tool("sap_document_flow", p2p_ctx, document_id="5105600231")

    for result in (from_pr, from_po, from_invoice):
        stages = {s["type"] for s in result["stages"]}
        assert {"purchase_requisition", "purchase_order", "goods_receipt", "supplier_invoice"} <= stages


def test_document_flow_reports_the_queried_document_type(p2p_ctx, run_tool):
    """Fatura ile sorgulanan zincir PR ile baslar ama tip fatura olarak raporlanir."""
    result = run_tool("sap_document_flow", p2p_ctx, document_id="5105600231")
    assert result["resolved_type"] == "supplier_invoice"


def test_every_document_link_carries_its_sap_reference_field(p2p_ctx, run_tool):
    """Belge baglari uydurulmaz; her bag kaynak SAP referansini tasir."""
    result = run_tool("sap_document_flow", p2p_ctx, document_id="4500019014")
    for node in result["chain"]:
        assert node["linked_by"], f"{node['document_id']} bag kaynagini bildirmemis"
    assert result["evidence"]["source_api"] == "document_flow"


def test_unknown_document_returns_empty_chain_not_a_guess(p2p_ctx, run_tool):
    result = run_tool("sap_document_flow", p2p_ctx, document_id="9999999999")
    assert result["chain"] == []
    assert "bulunamadi" in result["interpretation"]
    assert result.get("next_steps")


def test_document_flow_without_payment_warns(p2p_ctx, run_tool):
    result = run_tool("sap_document_flow", p2p_ctx, document_id="5105600231")
    assert result["chain_complete"] is False
    assert any("odeme" in w.lower() for w in result["warnings"])


def test_completed_chain_includes_payment(p2p_ctx, run_tool):
    result = run_tool("sap_document_flow", p2p_ctx, document_id="4500019188")
    assert result["chain_complete"] is True
    assert "payment" in {s["type"] for s in result["stages"]}


# --- PO 360 ----------------------------------------------------------------
def test_purchase_order_360_computes_open_and_gr_ir_gap(p2p_ctx, run_tool):
    """Acik miktar ve GR/IR farki kodda hesaplanir, modele birakilmaz."""
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500019014")
    # 20 siparis - 12 teslim = 8 acik; 12 teslim - 8 fatura = 4 GR/IR farki.
    assert result["open_item_count"] == 1
    assert result["gr_ir_gap_qty"] == 4.0
    assert result["gr_ir_gap_value"] == pytest.approx(9920.0)
    assert result["open_value"] == pytest.approx(19840.0)
    assert result["delivered_pct"] == pytest.approx(60.0)


def test_purchase_order_360_reports_delay_from_schedule_lines(p2p_ctx, run_tool):
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500019014")
    assert result["max_delay_days"] == 26
    assert any("gecikmeli" in w for w in result["warnings"])


def test_purchase_order_360_flags_reversal_movements(p2p_ctx, run_tool):
    """Iptal (102) hareketi net teslim miktarini degistirir; sessizce gecilmez."""
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500019455", detail="full")
    assert any("iptal" in w.lower() for w in result["warnings"])
    assert any(gr["reversal"] for gr in result["goods_receipts"])


def test_purchase_order_360_links_blocked_invoices(p2p_ctx, run_tool):
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500019014")
    assert result["blocked_invoices"] == ["5105600231"]


def test_missing_purchase_order_returns_explicit_error(p2p_ctx, run_tool):
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500000000", expect_error=True)
    assert result["sap_code"] == "EKPO_NOT_FOUND"


# --- Onay is akisi ---------------------------------------------------------
def test_workflow_status_shows_where_and_why_it_waits(p2p_ctx, run_tool):
    result = run_tool(
        "sap_workflow_status", p2p_ctx,
        object_type="purchase_requisition", object_id="0010004801",
    )
    step = result["current_step"]
    assert step["step_no"] == 3
    assert step["role"] == "Satinalma muduru"
    assert step["waiting_days"] >= 1
    assert step["reason"]
    assert result["overdue"] is True


def test_workflow_processor_name_is_masked_by_default(p2p_ctx, run_tool):
    """Islemci adi D2 kisisel veridir; varsayilan karar icin yalniz rol gosterilir."""
    result = run_tool(
        "sap_workflow_status", p2p_ctx,
        object_type="purchase_requisition", object_id="0010004801",
    )
    assert result["current_step"]["processor_name"] == "***"
    assert result["current_step"]["role"] == "Satinalma muduru"


def test_workflow_without_instance_is_explicit(p2p_ctx, run_tool):
    result = run_tool(
        "sap_workflow_status", p2p_ctx,
        object_type="purchase_order", object_id="4500018821",
    )
    assert result["workflow_found"] is False
    assert "bulunamadi" in result["interpretation"]


def test_unclaimed_step_is_reported_as_pooled(p2p_ctx, run_tool):
    result = run_tool(
        "sap_workflow_status", p2p_ctx,
        object_type="supplier_invoice", object_id="5105600402",
    )
    assert result["current_step"]["status"] == "ready"
    assert "havuzda" in result["interpretation"]


# --- Fatura durumu ---------------------------------------------------------
def test_invoice_status_requires_at_least_one_filter(p2p_ctx, run_tool):
    result = run_tool("sap_supplier_invoice_status", p2p_ctx, expect_error=True)
    assert result["denial_code"] == "FILTER_REQUIRED"


def test_blocked_invoice_listing_totals_are_computed(p2p_ctx, run_tool):
    result = run_tool("sap_supplier_invoice_status", p2p_ctx, only_blocked=True)
    assert result["blocked_count"] == 2
    assert result["blocked_gross"] == pytest.approx(40992.0)
    assert result["next_steps"]


def test_paid_invoice_reports_payment_details(p2p_ctx, run_tool):
    result = run_tool("sap_supplier_invoice_status", p2p_ctx, invoice_id="5105600118")
    row = result["invoices"][0]
    assert row["status"] == "paid"
    assert row["paid_on"] == "2026-08-08"
    assert row.get("days_overdue") is None


# --- Blokaj aciklamasi -----------------------------------------------------
def test_invoice_block_explain_computes_variance_and_limits(p2p_ctx, run_tool):
    """Sapma ve tolerans karsilastirmasi deterministik kodda yapilir."""
    result = run_tool("sap_invoice_block_explain", p2p_ctx, invoice_id="5105600231")
    price = next(f for f in result["findings"] if f["reason"] == "price")
    assert price["variance_abs"] == pytest.approx(155.0)
    assert price["variance_pct"] == pytest.approx(6.25)
    assert price["tolerance_key"] == "PP"
    # Hem mutlak hem yuzde sinir asilmis.
    assert len(price["exceeded_limits"]) == 2


def test_block_explain_does_not_claim_limits_it_does_not_know(p2p_ctx, run_tool):
    """Tolerans siniri tanimlanmamis blokajda 'asildi' iddiasi uretilmez."""
    result = run_tool("sap_invoice_block_explain", p2p_ctx, invoice_id="5105600231")
    quantity = next(f for f in result["findings"] if f["reason"] == "quantity")
    assert quantity.get("tolerance_limit_abs") is None
    assert quantity.get("exceeded_limits") is None


def test_block_explain_states_assumptions_and_stays_read_only(p2p_ctx, run_tool):
    result = run_tool("sap_invoice_block_explain", p2p_ctx, invoice_id="5105600231")
    assert result["assumptions"]
    assert any("kaldirma" in w for w in result["warnings"])
    assert result["next_steps"]


def test_unblocked_invoice_needs_no_explanation(p2p_ctx, run_tool):
    result = run_tool("sap_invoice_block_explain", p2p_ctx, invoice_id="5105600118")
    assert result["blocked"] is False
    assert "bloke degil" in result["interpretation"]


def test_missing_invoice_returns_explicit_error(p2p_ctx, run_tool):
    result = run_tool(
        "sap_invoice_block_explain", p2p_ctx, invoice_id="0000000000", expect_error=True
    )
    assert result["sap_code"] == "RBKP_NOT_FOUND"


# --- Yetki ve butce --------------------------------------------------------
def test_unauthorized_actor_gets_no_p2p_data(settings):
    """Kapsam tasimayan cagirici hicbir P2P sonucu goremez."""
    load_all_tools()
    ctx = ToolContext(
        settings=settings, sap=build_backend(settings), actor=ActorContext.anonymous()
    )
    for name, args in (
        ("sap_document_flow", {"document_id": "4500019014"}),
        ("sap_purchase_order_360", {"po_id": "4500019014"}),
        ("sap_supplier_invoice_status", {"only_blocked": True}),
    ):
        payload, is_error = execute_tool(name, args, ctx)
        assert is_error and "AUTH_REQUIRED" in payload


@pytest.mark.parametrize("name", P2P_TOOLS)
def test_summary_is_the_narrowest_projection(p2p_ctx, run_tool, name):
    """Varsayilan dar: summary, standard'dan kucuk olmali."""
    arguments = {
        "sap_document_flow": {"document_id": "5105600231"},
        "sap_purchase_order_360": {"po_id": "4500019014"},
        "sap_workflow_status": {
            "object_type": "purchase_requisition", "object_id": "0010004801",
        },
        "sap_supplier_invoice_status": {"only_blocked": True},
        "sap_invoice_block_explain": {"invoice_id": "5105600231"},
    }[name]

    from robotics_agent.cache import reset_tool_cache

    summary_payload, _ = execute_tool(name, {**arguments, "detail": "summary"}, p2p_ctx)
    reset_tool_cache()
    standard_payload, _ = execute_tool(name, {**arguments, "detail": "standard"}, p2p_ctx)
    assert estimate_tokens(summary_payload) <= estimate_tokens(standard_payload)
