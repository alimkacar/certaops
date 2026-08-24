#!/usr/bin/env python3
"""CAL acilmadan once kod, bagimlilik ve guvenlik hazirlik kapisi."""

from __future__ import annotations

import argparse
import subprocess
import sys

from cal_acceptance_lib import (
    BLOCKED,
    PROJECT_ROOT,
    add_common_args,
    load_profile,
    make_report,
    timed,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser, live=False)
    parser.add_argument(
        "--skip-tests", action="store_true", help="Secili yerel pytest/ruff kapilarini atla."
    )
    args = parser.parse_args(argv)
    report, started = make_report("CAL-00", "CAL oncesi yerel hazirlik")

    profile = load_profile(args.env_file, required=False)
    report.check(
        "CAL profil sablonu mevcut",
        (PROJECT_ROOT / ".env.cal.example").exists(),
        ".env.cal.example",
    )
    if profile.exists():
        report.add("Kisisel CAL profili hazir", "PASS", str(profile))
    else:
        report.add(
            "Kisisel CAL profili henuz yok",
            BLOCKED,
            "Normal: CAL Connection Details belli olunca .env.cal.example -> .env.cal kopyalanacak.",
        )

    from robotics_agent.adapters.sap import CAPABILITY_MANIFEST
    from robotics_agent.sap.odata import ODataSAPBackend
    from robotics_agent.tools import load_all_tools
    from robotics_agent.tools.registry import REGISTRY

    load_all_tools()
    report.check("Tool envanteri tam", len(REGISTRY) == 24, f"kayitli={len(REGISTRY)}")
    expected_services = {
        "product", "stock", "availability", "mrp", "inforecord", "supplier",
        "purchase_requisition_v2", "purchase_order_v2", "material_document",
        "supplier_invoice", "project_cost",
    }
    missing = expected_services - set(CAPABILITY_MANIFEST)
    report.check(
        "CAL servis manifesti tam",
        not missing,
        f"eksik={sorted(missing)}" if missing else f"{len(expected_services)} servis ailesi",
    )
    implemented = {
        name
        for name in (
            "get_purchase_order_items", "get_schedule_lines", "get_goods_receipts",
            "get_supplier_invoices", "get_document_flow",
        )
        if name in ODataSAPBackend.__dict__
    }
    report.check(
        "S/4 OData P2P uygulamalari mevcut",
        len(implemented) == 5,
        ", ".join(sorted(implemented)),
    )
    report.add(
        "Workflow gercek kaynak secimi",
        BLOCKED,
        "CAL flexible workflow/CDS veya BPA endpoint'i gorulmeden adapter uydurulmayacak.",
    )
    report.add(
        "Proje maliyeti custom servis",
        BLOCKED,
        "ZAPI_PROJECT_COST_SRV appliance'ta hazir degilse bu tool canli kabulden haric kalir.",
    )

    if not args.skip_tests:
        commands = [
            (
                "Secili pytest kapisi",
                [
                    sys.executable, "-m", "pytest",
                    "tests/unit/test_odata_backend.py",
                    "tests/unit/test_p2p_tools.py",
                    "tests/unit/test_cal_analysis.py",
                    "tests/contract/test_metadata_contracts.py", "-q",
                ],
            ),
            (
                "Ruff statik kalite kapisi",
                [
                    sys.executable, "-m", "ruff", "check",
                    "scripts/cal_acceptance_lib.py", "scripts/cal_00_preflight.py",
                    "scripts/cal_01_connection.py", "scripts/cal_02_contracts.py",
                    "scripts/cal_03_read_scenarios.py", "scripts/cal_04_p2p_scenarios.py",
                    "scripts/cal_05_security_scenarios.py", "scripts/cal_06_write_pr.py",
                    "scripts/cal_07_service_inventory.py", "scripts/cal_08_data_profile.py",
                    "scripts/cal_09_tool_opportunities.py", "scripts/cal_analysis_lib.py",
                    "scripts/cal_acceptance.py",
                    "src/robotics_agent/sap/odata.py",
                    "src/robotics_agent/adapters/sap/capabilities.py",
                    "src/robotics_agent/tools/p2p_read.py",
                ],
            ),
        ]
        for label, command in commands:
            result, elapsed, _, error = timed(
                lambda command=command: subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    text=True,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
            )
            if error:
                report.check(label, False, error, duration_ms=elapsed)
                continue
            output = (result.stdout + "\n" + result.stderr).strip().splitlines()
            report.check(
                label,
                result.returncode == 0,
                output[-1] if output else f"exit={result.returncode}",
                duration_ms=elapsed,
            )

    return report.finish(started, out=args.out or "artifacts/cal/preflight.json")


if __name__ == "__main__":
    raise SystemExit(main())
