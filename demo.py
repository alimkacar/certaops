#!/usr/bin/env python3
"""API anahtari gerektirmeyen SAP multi-agent tool-runtime demosu."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent / "src"))

from robotics_agent.config import get_settings  # noqa: E402
from robotics_agent.contracts import ActorContext  # noqa: E402
from robotics_agent.core import AGENT_SPECS, domains_for_packs, schema_token_report  # noqa: E402
from robotics_agent.sap import build_backend  # noqa: E402
from robotics_agent.tools import (  # noqa: E402
    ToolContext,
    anthropic_tool_definitions,
    execute_tool,
    load_all_tools,
    registry_summary,
    visible_tool_names,
)


def _ctx() -> ToolContext:
    settings = get_settings()
    settings.ensure_dirs()
    actor = ActorContext.local_operator(
        subject="demo-operator",
        tenant=settings.sap.tenant,
        roles=("PURCHASER", "AUDITOR"),
        company_code=settings.sap.company_code,
        plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )
    return ToolContext(settings=settings, sap=build_backend(settings), actor=actor)


def call(ctx: ToolContext, name: str, **arguments: Any) -> dict[str, Any]:
    payload, is_error = execute_tool(name, arguments, ctx)
    result = json.loads(payload)
    if is_error:
        raise RuntimeError(f"{name}: {result}")
    return result


def run_demo() -> int:
    load_all_tools()
    ctx = _ctx()
    print("CertaOps - deterministik SAP tool runtime demosu")
    print(f"Sistem: {ctx.settings.sap.system_alias} | backend: {ctx.sap.name} | dry-run: {ctx.settings.sap.dry_run}")

    profiles = call(ctx, "sap_list_domains")
    print("\nDomain profilleri:")
    for item in profiles["domains"]:
        print(f"  - {item['domain']}: {item['title']}")

    material = "SFT-SCN-270"
    master = call(ctx, "sap_material_360", material_id=material, detail="summary")
    print(f"\nAna veri: {material} | {master['description']} | fiyat {master['price']:,.2f} {master['currency']}")

    atp = call(
        ctx,
        "sap_atp_check",
        requests=[{"material_id": material, "quantity": 4, "required_date": "2026-10-15"}],
    )
    row = atp["results"][0]
    print(
        "ATP: "
        f"istenen {row['requested_qty']:g}, teyit {row['confirmed_qty']:g}, "
        f"tam teyit {row.get('full_confirmation_date', '-') }"
    )

    suppliers = call(ctx, "sap_compare_vendors", material_id=material, quantity=4)
    best = suppliers["recommendation"]["best_tco_vendor"]
    print(f"Tedarik: en iyi TCO tedarikcisi {best}")

    items = [
        {
            "material_id": material,
            "quantity": 4,
            "preferred_vendor": best,
            "wbs_element": "R-2026-021-1",
        }
    ]
    prepared = call(ctx, "sap_pr_prepare", items=items, header_text="SAP multi-agent demo")
    print(
        f"PR prepare: {prepared['total_value']:,.2f} {prepared['currency']} | "
        f"onay gerekli: {prepared['requires_human_approval']} | SAP'a yazildi: {prepared['written_to_sap']}"
    )
    submitted = call(
        ctx,
        "sap_pr_submit",
        items=items,
        header_text="SAP multi-agent demo",
        idempotency_key="DEMO:SFT-SCN-270:pr:v3",
    )
    print(
        f"PR submit: {submitted['write_status']} | "
        f"belge: {submitted.get('business_object_id', '-')} | verified: {submitted.get('verified', False)}"
    )

    finance = call(ctx, "sap_project_cost_status", wbs_element="R-2026-014")
    summary = finance["portfolio_summary"]
    print(
        f"WBS finans: plan {summary['total_plan']:,.0f}, fiili {summary['total_actual']:,.0f}, "
        f"EAC {summary['total_eac']:,.0f} {finance['currency']}"
    )

    # --- Salt-okunur procure-to-pay gorunurlugu ----------------------------
    print("\nProcure-to-pay gorunurlugu:")
    po_status = call(ctx, "sap_purchase_order_360", po_id="4500019014")
    print(
        f"  PO 4500019014: %{po_status['delivered_pct']:.0f} teslim, "
        f"{po_status['max_delay_days']} gun gecikme, "
        f"GR/IR farki {po_status['gr_ir_gap_value']:,.0f} {po_status['currency']}"
    )

    flow = call(ctx, "sap_document_flow", document_id="5105600231")
    chain = " -> ".join(f"{s['type']}({s['count']})" for s in flow["stages"])
    print(f"  Belge zinciri: {chain}")

    blocked = call(ctx, "sap_supplier_invoice_status", only_blocked=True)
    print(
        f"  Bloke fatura: {blocked['blocked_count']} adet, "
        f"{blocked['blocked_gross']:,.0f} {blocked['currency']}"
    )

    block = call(ctx, "sap_invoice_block_explain", invoice_id="5105600231")
    price = next(f for f in block["findings"] if f["reason"] == "price")
    print(
        f"  Blokaj nedeni: fiyat farki %{price['variance_pct']:.2f} "
        f"(tolerans %{price['tolerance_limit_pct']:.0f}, anahtar {price['tolerance_key']})"
    )

    workflow = call(
        ctx, "sap_workflow_status",
        object_type="purchase_requisition", object_id="0010004801",
    )
    step = workflow["current_step"]
    # `processor_name` DLP tarafindan maskelenir; karar icin rol yeterlidir.
    print(f"  Onay: '{step['name']}' adiminda {step['role']} tarafinda {step['waiting_days']} gun")

    assert ctx.audit
    print(f"\nAudit zinciri: {ctx.audit.verify()}")
    if ctx.cache is not None:
        print(f"Cache: {ctx.cache.stats.to_dict()}")
    return 0


def list_tools() -> int:
    load_all_tools()
    for item in registry_summary():
        print(
            f"{item['name']:<34} {item['domain']:<20} {item['risk']:<3} "
            f"onay={item['approval']}"
        )
    return 0


def token_report() -> int:
    """Agent basina sema token raporu.

    Butce asilirsa **sifir disi** kod doner: bu komut CI performans kapisidir,
    yalnizca bilgi amacli bir cikti degildir.
    """
    load_all_tools()
    ctx = _ctx()
    breaches: list[str] = []
    for key, spec in AGENT_SPECS.items():
        names = visible_tool_names(domains_for_packs(spec.packs), ctx.actor)
        report = schema_token_report(
            anthropic_tool_definitions(names),
            budget=ctx.settings.budget.schema_tokens_per_turn,
        )
        if not report["within_budget"]:
            breaches.append(f"{key} ({report['schema_tokens']} token)")
        print(
            f"{key:<14} {len(names):>2} tool | {report['schema_tokens']:>5} token | "
            f"butce={'OK' if report['within_budget'] else 'ASIM'}"
        )
    if breaches:
        print(
            f"\nSema token butcesi asildi: {', '.join(breaches)} "
            f"(sinir {ctx.settings.budget.schema_tokens_per_turn}).",
            file=sys.stderr,
        )
        return 1
    return 0


def run_single(name: str, raw_args: str) -> int:
    load_all_tools()
    result = call(_ctx(), name, **json.loads(raw_args or "{}"))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CertaOps SAP agent toolkit demosu")
    parser.add_argument("--list", action="store_true", help="SAP tool katalogunu listele")
    parser.add_argument("--tokens", action="store_true", help="Agent bazli sema butcesini goster")
    parser.add_argument("--tool", help="Tek bir tool calistir")
    parser.add_argument("--args", default="{}", help="Tool argumanlari JSON")
    args = parser.parse_args()
    if args.list:
        return list_tools()
    if args.tokens:
        return token_report()
    if args.tool:
        return run_single(args.tool, args.args)
    return run_demo()


if __name__ == "__main__":
    raise SystemExit(main())
