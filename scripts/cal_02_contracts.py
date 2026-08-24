#!/usr/bin/env python3
"""CAL sistemindeki released OData servis ve alan sozlesmelerini canli dogrular."""

from __future__ import annotations

import argparse

from cal_acceptance_lib import (
    BLOCKED,
    add_common_args,
    load_profile,
    make_report,
    target_summary,
    timed,
)

REQUIRED = (
    "product",
    "classification",
    "valuation",
    "stock",
    "availability",
    "mrp",
    "inforecord",
    "supplier",
    "supplier_score",
    "purchase_requisition_v2",
    "purchase_order_v2",
    "material_document",
    "supplier_invoice",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    report, started = make_report("CAL-02", "Canli OData kontratlari")

    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/02-contracts.json")

    from robotics_agent.adapters.sap import CAPABILITY_MANIFEST
    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    try:
        results, elapsed, calls, error = timed(
            lambda: backend.probe_capabilities(REQUIRED), backend
        )
        if error:
            report.check(
                "Metadata sondasi tamamlandi", False, error,
                duration_ms=elapsed, sap_calls=calls,
            )
            return report.finish(started, out=args.out or "artifacts/cal/02-contracts.json")
        by_alias = {entry["alias"]: entry for entry in results}
        for alias in REQUIRED:
            entry = by_alias.get(alias, {})
            capability = CAPABILITY_MANIFEST[alias]
            detail = "uyumlu"
            if not entry.get("contract_ok"):
                missing_sets = entry.get("missing_entity_sets") or []
                missing_fields = entry.get("missing_properties") or {}
                detail = (
                    entry.get("error")
                    or f"entity={missing_sets}; alan={missing_fields}; "
                    "V2 icin /IWFND/MAINT_SERVICE ve SICF aktivasyonunu kontrol edin"
                )
            report.check(
                f"{alias}: {capability.odata_version} kontrati",
                bool(entry.get("contract_ok")),
                detail,
                duration_ms=round(float(entry.get("latency_ms") or 0)),
            )
        report.check(
            "Tum zorunlu released servisler hazir",
            all(by_alias.get(alias, {}).get("contract_ok") for alias in REQUIRED),
            f"{sum(bool(by_alias.get(a, {}).get('contract_ok')) for a in REQUIRED)}/{len(REQUIRED)}",
        )
        report.add(
            "project_cost custom kontrati",
            BLOCKED,
            "ZAPI_PROJECT_COST_SRV ayri Tier-2/RAP gelistirmesidir; CAL standardinda beklenmez.",
        )
        report.add(
            "workflow kontrati",
            BLOCKED,
            "Appliance'taki flexible workflow veya BPA kaynagi secilmeden sabit endpoint yok.",
        )
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/02-contracts.json")


if __name__ == "__main__":
    raise SystemExit(main())
