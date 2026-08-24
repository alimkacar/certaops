#!/usr/bin/env python3
"""CAL'da 15 cekirdek read/hazirlik tool'unu gercek is anahtarlariyla calistirir."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from typing import Any

from cal_acceptance_lib import (
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
    report, started = make_report("CAL-03", "Gercek salt-okunur tool senaryolari")

    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/03-read.json")

    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    supplied = supplied_from_args(args)
    try:
        seeds = supplied
        if not args.no_discover:
            discovered, elapsed, calls, error = timed(
                lambda: discover_seeds(backend, supplied), backend
            )
            report.check(
                "Gercek is anahtari kesfi",
                error is None and bool(discovered),
                error or discovered,
                duration_ms=elapsed,
                sap_calls=calls,
            )
            if discovered:
                seeds = discovered
        report.seeds = seeds
        report.check("Malzeme senaryosu mevcut", bool(seeds.get("material")), seeds)
        report.check("PO senaryosu mevcut", bool(seeds.get("po")), seeds.get("po") or "yok")

        ctx = tool_context(settings, backend, roles=("PURCHASER", "AUDITOR"))
        required_date = (date.today() + timedelta(days=45)).isoformat()
        material, vendor = seeds.get("material", ""), seeds.get("vendor", "")
        evidence_id = ""

        def run(name: str, arguments: dict[str, Any], *, required: bool = True) -> dict[str, Any]:
            nonlocal evidence_id
            result, elapsed, calls, crash = timed(
                lambda: call_tool(ctx, name, arguments), backend
            )
            if crash:
                report.check(name, False, crash, critical=required, duration_ms=elapsed, sap_calls=calls)
                return {}
            payload, is_error = result
            error = payload_error(payload, is_error)
            report.check(
                name,
                not error,
                error or "tool yiginindan gecti",
                critical=required,
                duration_ms=elapsed,
                sap_calls=calls,
            )
            evidence_id = evidence_id or find_value(payload, "evidence_id")
            return payload

        run("sap_connection_health", {})
        run("sap_discover_capabilities", {"probe": False})
        run(
            "sap_explain_authorization_failure",
            {
                "http_status": 403,
                "sap_message": "No authorization for plant 1710",
                "target_api": "API_PRODUCT_SRV",
            },
        )
        run("sap_list_domains", {"message": f"{material} stok ve tedarik analizi"})
        search = run("sap_search_materials", {"query": material, "limit": 10}) if material else {}
        report.check(
            "Arama gercek malzemeyi buldu",
            bool(material and find_value(search, "material_id", "Product")),
            material or "malzeme yok",
        )
        if material:
            run("sap_material_360", {"material_id": material, "detail": "standard"})
            stock = run("sap_stock_overview", {"material_ids": [material], "detail": "standard"})
            report.check(
                "Stok senaryosu sayisal sonuc uretti",
                any(key in str(stock) for key in ("unrestricted", "available", "stock")),
                "stok sifir olabilir; alanin gercek SAP'tan gelmesi aranir",
            )
            run(
                "sap_atp_check",
                {"requests": [{
                    "material_id": material, "quantity": 1,
                    "required_date": required_date,
                }]},
            )
            run("sap_mrp_shortage_explain", {"material_id": material})
            comparison = run(
                "sap_compare_vendors",
                {"material_id": material, "quantity": 1, "required_date": required_date},
            )
            if not find_value(comparison, "vendor_id", "supplier"):
                report.add(
                    "Coklu tedarikci veri senaryosu",
                    WARN,
                    "Secilen malzemede gecerli info record/tedarikci adayi yok; teknik cagri gecti ama karsilastirma UAT'i eksik.",
                )
            run("sap_track_purchase_orders", {"material_id": material, "only_open": False})
            run(
                "sap_pr_prepare",
                {"items": [{
                    "material_id": material, "quantity": 1,
                    "delivery_date": required_date,
                }], "header_text": "CERTAOPS CAL DRY RUN"},
            )
        if vendor:
            run("sap_supplier_score_360", {"vendor_ids": [vendor]})
        else:
            report.add("sap_supplier_score_360", WARN, "Gercek tedarikci cekirdegi bulunamadi.")
        run("sap_reconcile_execution", {"list_pending": True})
        if evidence_id:
            run("get_evidence", {"evidence_id": evidence_id})
        else:
            report.add("get_evidence", WARN, "Onceki canli tool sonucu evidence_id uretmedi.")
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/03-read.json")


if __name__ == "__main__":
    raise SystemExit(main())
