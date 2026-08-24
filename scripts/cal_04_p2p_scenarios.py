#!/usr/bin/env python3
"""CAL'da gercek PO -> GR -> fatura zinciri ve kosullu workflow/proje testleri."""

from __future__ import annotations

import argparse
from typing import Any

from cal_acceptance_lib import (
    BLOCKED,
    WARN,
    add_common_args,
    call_tool,
    discover_seeds,
    find_value,
    load_profile,
    make_report,
    payload_error,
    supplied_from_args,
    target_summary,
    timed,
    tool_context,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--no-discover", action="store_true")
    args = parser.parse_args(argv)
    report, started = make_report("CAL-04", "Procure-to-pay belge zinciri")
    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/04-p2p.json")

    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    try:
        seeds = supplied_from_args(args)
        if not args.no_discover:
            value, elapsed, calls, error = timed(
                lambda: discover_seeds(backend, seeds), backend
            )
            report.check(
                "P2P referans kesfi", error is None and bool(value),
                error or value, duration_ms=elapsed, sap_calls=calls,
            )
            if value:
                seeds = value
        report.seeds = seeds
        po_id, invoice_id, wbs = seeds.get("po", ""), seeds.get("invoice", ""), seeds.get("wbs", "")
        report.check("Gercek PO secildi", bool(po_id), po_id or "PO bulunamadi")
        ctx = tool_context(settings, backend, roles=("PURCHASER", "AUDITOR"))

        def run(name: str, arguments: dict[str, Any], *, optional: bool = False) -> dict[str, Any]:
            result, elapsed, calls, crash = timed(
                lambda: call_tool(ctx, name, arguments), backend
            )
            if crash:
                report.check(name, False, crash, critical=not optional, duration_ms=elapsed, sap_calls=calls)
                return {}
            payload, is_error = result
            error = payload_error(payload, is_error)
            if payload.get("denial_code") == "CAPABILITY_NOT_SUPPORTED":
                report.add(
                    name, BLOCKED, payload.get("remediation") or error,
                    duration_ms=elapsed, sap_calls=calls,
                )
            else:
                report.check(
                    name, not error, error or "gercek referanslarla calisti",
                    critical=not optional, duration_ms=elapsed, sap_calls=calls,
                )
            return payload

        po_payload: dict[str, Any] = {}
        if po_id:
            po_payload = run("sap_purchase_order_360", {"po_id": po_id, "detail": "full"})
            item_count = int(find_value(po_payload, "item_count") or 0)
            gr_count = int(find_value(po_payload, "goods_receipt_count") or 0)
            invoice_count = int(find_value(po_payload, "invoice_count") or 0)
            report.check("PO gercek kalem tasiyor", item_count > 0, f"item_count={item_count}")
            if gr_count == 0:
                report.add(
                    "Mal kabul senaryosu verisi", WARN,
                    "PO calisti fakat 101/102 malzeme belgesi yok; tam P2P UAT icin baska PO verin.",
                )
            else:
                report.check("PO -> GR bagi bulundu", True, f"goods_receipt_count={gr_count}")
            if invoice_count == 0:
                report.add(
                    "Fatura senaryosu verisi", WARN,
                    "PO calisti fakat RSEG PO referansli fatura yok; tam P2P UAT icin baska PO verin.",
                )
            else:
                report.check("PO -> fatura bagi bulundu", True, f"invoice_count={invoice_count}")

            flow = run(
                "sap_document_flow",
                {"document_id": po_id, "document_type": "purchase_order", "detail": "full"},
            )
            chain_blob = str(flow)
            report.check(
                "Belge baglari kanitli",
                "MSEG-EBELN" in chain_blob or "RSEG-EBELN" in chain_blob,
                "EKPO-BANFN / MSEG-EBELN / RSEG-EBELN alanlari aranir",
            )

        if not invoice_id and po_id:
            invoice_id = find_value(po_payload, "invoice_id")
        if invoice_id:
            status = run(
                "sap_supplier_invoice_status",
                {"invoice_id": invoice_id, "detail": "full"},
            )
            blocked = "blocked" in str(status).lower() or bool(find_value(status, "payment_block"))
            if blocked:
                run("sap_invoice_block_explain", {"invoice_id": invoice_id, "detail": "full"})
            else:
                # Mevcut PO faturasi bloke degilse sistemde gercek bloke fatura ara.
                rows, _, _, error = timed(
                    lambda: backend.get_supplier_invoices(only_blocked=True, limit=5), backend
                )
                blocked_id = rows[0].invoice_id if not error and rows else ""
                if blocked_id:
                    report.seeds["blocked_invoice"] = blocked_id
                    run("sap_invoice_block_explain", {"invoice_id": blocked_id, "detail": "full"})
                else:
                    report.add(
                        "sap_invoice_block_explain", WARN,
                        "Sistemde PaymentBlockingReason dolu gercek fatura bulunamadi; tool teknik olarak mevcut ama blokaj UAT verisi yok.",
                    )
        else:
            report.add("sap_supplier_invoice_status", WARN, "PO referansli fatura bulunamadi.")
            report.add("sap_invoice_block_explain", WARN, "Fatura cekirdegi olmadan calistirilamaz.")

        workflow_object = po_id or invoice_id
        if workflow_object:
            run(
                "sap_workflow_status",
                {"object_type": "purchase_order", "object_id": workflow_object},
                optional=True,
            )
        else:
            report.add("sap_workflow_status", BLOCKED, "Workflow'a baglanacak is nesnesi yok.")

        if wbs:
            run("sap_project_cost_status", {"wbs_element": wbs}, optional=True)
        else:
            report.add(
                "sap_project_cost_status", BLOCKED,
                "PO verisinde WBS yok; ayrica ZAPI_PROJECT_COST_SRV standart CAL servisi degildir.",
            )

        run(
            "sap_generate_report",
            {
                "title": "CertaOps CAL P2P Acceptance",
                "format": "markdown",
                "sections": [{
                    "heading": "Kapsam",
                    "body": (
                        f"PO={po_id or 'yok'}, invoice={invoice_id or 'yok'}. "
                        "Ayrintili kanit JSON ve audit defterindedir."
                    ),
                }],
            },
        )
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/04-p2p.json")


if __name__ == "__main__":
    raise SystemExit(main())
