#!/usr/bin/env python3
"""CAL'da once PR dry-run, acik uc kapida ise yalniz BIR gercek PR olusturur."""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta

from cal_acceptance_lib import (
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

CONFIRMATION = "CREATE-ONE-CAL-PR"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--execute-write", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--run-id", default=date.today().isoformat())
    args = parser.parse_args(argv)
    title = "Tek PR yazma" if args.execute_write else "PR dry-run ve policy"
    report, started = make_report("CAL-06", title)
    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/06-write.json")

    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    try:
        seeds, elapsed, calls, error = timed(
            lambda: discover_seeds(backend, supplied_from_args(args), max_po_checks=0), backend
        )
        if error:
            report.check("Yazma malzemesi kesfi", False, error, duration_ms=elapsed, sap_calls=calls)
            return report.finish(started, out=args.out or "artifacts/cal/06-write.json")
        report.seeds = seeds
        material = args.material or seeds.get("material", "")
        report.check("Gercek test malzemesi secildi", bool(material), material or "yok")
        if not material:
            return report.finish(started, out=args.out or "artifacts/cal/06-write.json")

        external_gate = os.getenv("SAP_INTEGRATION_ALLOW_WRITE", "0") == "1"
        gates = {
            "--execute-write": args.execute_write,
            "--confirm": args.confirm == CONFIRMATION,
            "SAP_DRY_RUN=false": not settings.sap.dry_run,
            "SAP_INTEGRATION_ALLOW_WRITE=1": external_gate,
        }
        if args.execute_write:
            for gate, opened in gates.items():
                report.check(f"Yazma kapisi: {gate}", opened, opened)
            if not all(gates.values()):
                report.check(
                    "Gercek yazma kapilari",
                    False,
                    "Eksik kapi var; SAP'a POST gonderilmedi.",
                )
                return report.finish(started, out=args.out or "artifacts/cal/06-write.json")
        else:
            report.check("Dry-run modu zorunlu", settings.sap.dry_run, settings.sap.dry_run)

        ctx = tool_context(settings, backend, roles=("PURCHASER", "AUDITOR"))
        delivery = (date.today() + timedelta(days=45)).isoformat()
        items = [{"material_id": material, "quantity": 1, "delivery_date": delivery}]
        prepare, elapsed, calls, crash = timed(
            lambda: call_tool(
                ctx,
                "sap_pr_prepare",
                {"items": items, "header_text": "CERTAOPS CAL ACCEPTANCE"},
            ),
            backend,
        )
        if crash:
            report.check("PR prepare", False, crash, duration_ms=elapsed, sap_calls=calls)
            return report.finish(started, out=args.out or "artifacts/cal/06-write.json")
        prepare_payload, prepare_error = prepare
        error_text = payload_error(prepare_payload, prepare_error)
        report.check(
            "PR prepare gercek fiyatlandirma",
            not error_text,
            error_text or f"total={find_value(prepare_payload, 'total_value')} item=1",
            duration_ms=elapsed,
            sap_calls=calls,
        )
        approval_needed = bool(
            find_value(prepare_payload, "approval_task")
            or prepare_payload.get("requires_human_approval")
        )
        report.check(
            "Tek kalem onay esigi altinda",
            not approval_needed,
            "Onay gerekiyorsa script otomatik onay uretmez; farkli malzeme secin veya gercek onay verin.",
        )
        if error_text or approval_needed:
            return report.finish(started, out=args.out or "artifacts/cal/06-write.json")

        idempotency_key = f"cal:{args.run_id}:{material}:pr:v1"
        submit_args = {
            "items": items,
            "header_text": "CERTAOPS CAL ACCEPTANCE",
            "idempotency_key": idempotency_key,
        }
        result, elapsed, calls, crash = timed(
            lambda: call_tool(ctx, "sap_pr_submit", submit_args), backend
        )
        if crash:
            report.check("sap_pr_submit", False, crash, duration_ms=elapsed, sap_calls=calls)
            return report.finish(started, out=args.out or "artifacts/cal/06-write.json")
        submit, submit_error = result
        error_text = payload_error(submit, submit_error)
        requisition_id = find_value(submit, "requisition_id", "PurchaseRequisition")
        if args.execute_write:
            report.check(
                "Tek PR olusturuldu ve geri okundu",
                not error_text and bool(requisition_id),
                error_text or f"PR={requisition_id}; idempotency={idempotency_key}",
                duration_ms=elapsed,
                sap_calls=calls,
            )
        else:
            simulated = find_value(submit, "write_status") == "simulated"
            report.check(
                "PR yazma guvenle simule edildi",
                not error_text and simulated and submit.get("written_to_sap") is not True,
                error_text or f"write_status={find_value(submit, 'write_status')}",
                duration_ms=elapsed,
                sap_calls=calls,
            )

        if args.execute_write and requisition_id:
            before = backend.sap_call_count
            replay, replay_error = call_tool(ctx, "sap_pr_submit", submit_args)
            replay_id = find_value(replay, "requisition_id", "PurchaseRequisition")
            report.check(
                "Idempotent tekrar ikinci belge olusturmadi",
                not payload_error(replay, replay_error)
                and (not replay_id or replay_id == requisition_id)
                and backend.sap_call_count == before,
                f"ilk={requisition_id}, tekrar={replay_id or 'cached'}; ek SAP cagri={backend.sap_call_count-before}",
            )
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/06-write.json")


if __name__ == "__main__":
    raise SystemExit(main())
