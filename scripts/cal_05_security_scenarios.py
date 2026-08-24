#!/usr/bin/env python3
"""CAL profilinde yetki reddi, negatif veri, egress, DLP ve audit senaryolari."""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

from cal_acceptance_lib import (
    add_common_args,
    call_tool,
    load_profile,
    make_report,
    payload_error,
    target_summary,
    timed,
    tool_context,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    report, started = make_report("CAL-05", "Negatif ve guvenlik senaryolari")
    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/05-security.json")

    from robotics_agent.adapters.sap import SAPError
    from robotics_agent.config import Settings
    from robotics_agent.privacy import (
        DataClass,
        DataPolicy,
        DLPEngine,
        FieldAccessPolicy,
        get_pseudonymizer,
    )
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    try:
        material = args.material or "CERTAOPS-NOT-EXIST-MATERIAL"
        soon = (date.today() + timedelta(days=45)).isoformat()
        viewer = tool_context(settings, backend, roles=("VIEWER",))
        before = backend.sap_call_count
        payload, is_error = call_tool(
            viewer,
            "sap_pr_submit",
            {
                "items": [{"material_id": material, "quantity": 1, "delivery_date": soon}],
                "header_text": "MUST NOT WRITE",
                "idempotency_key": "cal:viewer-denial:pr:v1",
            },
        )
        denial = payload_error(payload, is_error)
        report.check(
            "VIEWER yazma yetkisi reddedildi",
            is_error and any(code in str(payload) for code in ("MISSING_SCOPE", "DENY", "FORBIDDEN")),
            denial or payload,
            sap_calls=backend.sap_call_count - before,
        )
        report.check(
            "Yetki reddinde SAP'a cagri gitmedi",
            backend.sap_call_count == before,
            f"once={before}, sonra={backend.sap_call_count}",
        )

        purchaser = tool_context(settings, backend, roles=("PURCHASER", "AUDITOR"))
        ghost_id = "CERTAOPS-NOT-EXIST-999999"
        result, elapsed, calls, crash = timed(
            lambda: call_tool(purchaser, "sap_material_360", {"material_id": ghost_id}), backend
        )
        if crash:
            report.check("Olmayan malzeme acik hata", False, crash, duration_ms=elapsed)
        else:
            ghost_payload, ghost_error = result
            report.check(
                "Olmayan malzeme acik hata",
                ghost_error or bool(ghost_payload.get("error")),
                ghost_payload.get("error") or ghost_payload,
                duration_ms=elapsed,
                sap_calls=calls,
            )

        result, elapsed, calls, crash = timed(
            lambda: call_tool(purchaser, "sap_purchase_order_360", {"po_id": "9999999999"}),
            backend,
        )
        if crash:
            report.check("Olmayan PO tahmin uretemiyor", False, crash, duration_ms=elapsed)
        else:
            ghost_po, po_error = result
            report.check(
                "Olmayan PO tahmin uretemiyor",
                po_error or bool(ghost_po.get("error")),
                ghost_po.get("error") or ghost_po,
                duration_ms=elapsed,
                sap_calls=calls,
            )

        try:
            backend._core_v2._assert_host_allowed("https://exfil.invalid/steal")
        except SAPError as exc:
            report.check("SSRF/egress allowlist engeli", True, exc.code)
        else:
            report.check("SSRF/egress allowlist engeli", False, "Izinsiz host kabul edildi")

        iban = "DE89370400440532013000"
        engine = DLPEngine(
            field_policy=FieldAccessPolicy(), pseudonymizer=get_pseudonymizer()
        )
        policy = DataPolicy(fields={"supplier_iban": DataClass.D3})
        leaks = []
        for sink in ("model", "log", "handoff", "client"):
            output = engine.apply(
                {"supplier_iban": iban}, actor=purchaser.actor, sink=sink, policy=policy
            )
            if iban in str(output.payload):
                leaks.append(sink)
        report.check("D3 veri hicbir cikisa ham sizmiyor", not leaks, leaks or "maskeli/tokenize")

        audit_payload, audit_error = call_tool(
            purchaser, "sap_get_execution_audit", {"limit": 20, "verify_chain": True}
        )
        report.check(
            "Audit zinciri dogrulandi",
            not payload_error(audit_payload, audit_error),
            audit_payload.get("error") or "audit.read kapsamiyla okundu",
        )
        report.check("SAP_DRY_RUN guvenlik kilidi", settings.sap.dry_run, settings.sap.dry_run)
        report.check(
            "Yazma entegrasyon kapisi kapali",
            os.getenv("SAP_INTEGRATION_ALLOW_WRITE", "0") != "1",
            os.getenv("SAP_INTEGRATION_ALLOW_WRITE", "0"),
        )
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/05-security.json")


if __name__ == "__main__":
    raise SystemExit(main())
