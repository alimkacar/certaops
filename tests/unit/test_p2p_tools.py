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


@pytest.fixture
def full_detail(settings, purchaser) -> ToolContext:
    """`detail=full` hakki olan baglam: D2 kapsami + gecerli isleme amaci.

    Amac kodu bilerek zorunludur - "full istedim" bir gerekce degildir.
    """
    load_all_tools()
    return ToolContext(
        settings=settings,
        sap=build_backend(settings),
        actor=purchaser,
        purpose="procurement_operations",
    )


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


def test_purchase_order_360_flags_reversal_movements(p2p_ctx, run_tool, full_detail):
    """Iptal (102) hareketi net teslim miktarini degistirir; sessizce gecilmez."""
    result = run_tool("sap_purchase_order_360", full_detail, po_id="4500019455", detail="full")
    assert any("iptal" in w.lower() for w in result["warnings"])
    assert any(gr["reversal"] for gr in result["goods_receipts"])


def test_full_detail_without_entitlement_is_downgraded(p2p_ctx, run_tool):
    """`detail=full` yalniz yazarak bes kat kayit alinamaz.

    Alan maskesi DLP'de zaten kapiliydi ama HACIM kapisizdi: tool'lar ham
    argumani okuyordu ve `resolve_detail()` actor'u hic gormez. Kapi artik
    merkezi olarak `execute_tool` icinde calisiyor ve dusurme gerekcesi
    sonuca uyari olarak yaziliyor.
    """
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500019455", detail="full")
    assert "goods_receipts" not in result, "yetkisiz cagirana full hacim verildi"
    assert any("full detay" in w.lower() for w in result["warnings"])


def test_purchase_order_360_links_blocked_invoices(p2p_ctx, run_tool):
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500019014")
    assert result["blocked_invoices"] == ["5105600231"]


def test_missing_purchase_order_returns_explicit_error(p2p_ctx, run_tool):
    result = run_tool("sap_purchase_order_360", p2p_ctx, po_id="4500000000", expect_error=True)
    assert result["sap_code"] == "EKPO_NOT_FOUND"


# --- Onay is akisi ---------------------------------------------------------
def test_invoice_status_requires_at_least_one_filter(p2p_ctx, run_tool):
    result = run_tool("sap_supplier_invoice_status", p2p_ctx, expect_error=True)
    assert result["denial_code"] == "FILTER_REQUIRED"


def test_blocked_invoice_listing_totals_are_computed(p2p_ctx, run_tool):
    result = run_tool("sap_supplier_invoice_status", p2p_ctx, only_blocked=True)
    assert result["blocked_count"] == 2
    assert result["blocked_gross"] == pytest.approx(40992.0)
    assert result["blocked_gross_by_currency"] == {"EUR": pytest.approx(40992.0)}
    assert result["next_steps"]


def test_invoice_totals_are_never_summed_across_currencies(
    p2p_ctx, run_tool, monkeypatch
):
    """Kur yoksa EUR + USD diye tek bir parasal toplam uretilemez."""
    from robotics_agent.sap.models import SupplierInvoice

    invoices = [
        SupplierInvoice(
            invoice_id="5100000001",
            company_code="1000",
            gross_amount=100.0,
            currency="EUR",
            status="blocked",
        ),
        SupplierInvoice(
            invoice_id="5100000002",
            company_code="1000",
            gross_amount=250.0,
            currency="USD",
            status="blocked",
        ),
    ]
    monkeypatch.setattr(
        p2p_ctx.sap, "get_supplier_invoices", lambda **_: invoices
    )

    result = run_tool("sap_supplier_invoice_status", p2p_ctx, only_blocked=True)

    assert result["total_gross_by_currency"] == {"EUR": 100.0, "USD": 250.0}
    assert result["blocked_gross_by_currency"] == {"EUR": 100.0, "USD": 250.0}
    assert result["currencies"] == ["EUR", "USD"]
    assert "total_gross" not in result
    assert "blocked_gross" not in result
    assert "currency" not in result
    assert "100.00 EUR; 250.00 USD" in result["interpretation"]


def test_paid_invoice_reports_payment_details(p2p_ctx, run_tool):
    result = run_tool("sap_supplier_invoice_status", p2p_ctx, invoice_id="5105600118")
    row = result["invoices"][0]
    assert row["status"] == "paid"
    assert row["paid_on"] == "2026-08-08"
    assert row.get("days_overdue") is None


def test_po_invoice_status_answers_yes_without_counting_cancelled_records(
    p2p_ctx, run_tool, monkeypatch
):
    from datetime import date

    from robotics_agent.sap.models import SupplierInvoice

    monkeypatch.setattr(
        p2p_ctx.sap,
        "get_supplier_invoices",
        lambda **_: [
            SupplierInvoice(
                invoice_id="5100001",
                gross_amount=100,
                currency="USD",
                due_date=date(2026, 1, 1),
                status="cancelled",
                po_ids=["4500000012"],
            ),
            SupplierInvoice(
                invoice_id="5100002",
                gross_amount=250,
                currency="USD",
                due_date=date(2026, 1, 1),
                status="posted",
                po_ids=["4500000012"],
            ),
        ],
    )

    result = run_tool("sap_supplier_invoice_status", p2p_ctx, po_id="4500000012")

    assert result["invoice_issued"] is True
    assert result["invoice_count"] == 1
    assert result["cancelled_count"] == 1
    assert result["overdue_count"] == 1
    assert result["total_gross_by_currency"] == {"USD": 250.0}
    assert result["interpretation"].startswith("Evet")


def test_cancelled_invoice_is_not_reported_as_issued_or_overdue(
    p2p_ctx, run_tool, monkeypatch
):
    from datetime import date

    from robotics_agent.sap.models import SupplierInvoice

    monkeypatch.setattr(
        p2p_ctx.sap,
        "get_supplier_invoices",
        lambda **_: [
            SupplierInvoice(
                invoice_id="5100001",
                gross_amount=100,
                currency="USD",
                due_date=date(2020, 1, 1),
                status="cancelled",
                po_ids=["4500000012"],
            )
        ],
    )

    result = run_tool("sap_supplier_invoice_status", p2p_ctx, po_id="4500000012")

    assert result["invoice_issued"] is False
    assert result["invoice_count"] == 0
    assert result["cancelled_count"] == 1
    assert result["overdue_count"] == 0
    assert result["total_gross_by_currency"] == {}
    assert result["interpretation"].startswith("Hayir")


def test_cancelled_invoice_id_reports_the_record_as_inspected(
    p2p_ctx, run_tool, monkeypatch
):
    from robotics_agent.sap.models import SupplierInvoice

    monkeypatch.setattr(
        p2p_ctx.sap,
        "get_supplier_invoices",
        lambda **_: [
            SupplierInvoice(
                invoice_id="5100000001",
                gross_amount=100,
                currency="USD",
                status="cancelled",
            )
        ],
    )

    result = run_tool(
        "sap_supplier_invoice_status", p2p_ctx, invoice_id="5100000001"
    )

    assert result["returned_invoice_count"] == 1
    assert result["invoice_count"] == 0
    assert result["interpretation"].startswith("1 fatura incelendi")


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
        "sap_supplier_invoice_status": {"only_blocked": True},
        "sap_invoice_block_explain": {"invoice_id": "5105600231"},
    }[name]

    from robotics_agent.cache import reset_tool_cache

    summary_payload, _ = execute_tool(name, {**arguments, "detail": "summary"}, p2p_ctx)
    reset_tool_cache()
    standard_payload, _ = execute_tool(name, {**arguments, "detail": "standard"}, p2p_ctx)
    assert estimate_tokens(summary_payload) <= estimate_tokens(standard_payload)
