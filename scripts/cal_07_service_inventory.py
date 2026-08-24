#!/usr/bin/env python3
"""CAL hedefindeki OData semasini bir kez okuyup guvenli envanter olarak kaydeder."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from cal_acceptance_lib import (
    WARN,
    add_common_args,
    load_profile,
    make_report,
    target_summary,
    timed,
)
from cal_analysis_lib import infer_join_fields, write_json


def _entity_inventory(contract: Any) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for entity_set in sorted(contract.entity_sets):
        properties = list(contract.properties_of_set(entity_set))
        navigations = contract.navigations_of_set(entity_set)
        entities.append(
            {
                "entity_set": entity_set,
                "entity_type": contract.type_of_set(entity_set),
                "properties": properties,
                "keys": list(contract.key_properties(entity_set)),
                "required_properties": list(contract.required_properties(entity_set)),
                "navigations": [
                    {
                        "name": name,
                        "target_type": navigation.target_type,
                        "is_collection": navigation.is_collection,
                    }
                    for name, navigation in sorted(navigations.items())
                ],
                "inferred_join_fields": infer_join_fields(properties),
            }
        )
    return entities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    report, started = make_report("CAL-07", "OData servis ve sema envanteri")

    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/07-inventory.json")

    from robotics_agent.adapters.sap import CAPABILITY_MANIFEST, verify_contract
    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    services: list[dict[str, Any]] = []
    contracts: dict[tuple[str, str], tuple[Any, int, int, Exception | None]] = {}
    try:
        for alias, capability in CAPABILITY_MANIFEST.items():
            service_key = (capability.service_path, capability.odata_version)
            reused = service_key in contracts
            if not reused:
                contracts[service_key] = timed(
                    lambda alias=alias: backend.metadata_contract(alias), backend
                )
            contract, fetched_elapsed, fetched_calls, error = contracts[service_key]
            elapsed = 0 if reused else fetched_elapsed
            calls = 0 if reused else fetched_calls
            row: dict[str, Any] = {
                "alias": alias,
                "service_path": capability.service_path,
                "odata_version": capability.odata_version,
                "status": capability.status,
                "purpose": capability.purpose,
                "latency_ms": elapsed,
                "sap_calls": calls,
                "metadata_reused": reused,
                "contract_ok": False,
                "entities": [],
                "actions": [],
                "functions": [],
            }
            if error is not None:
                row["error"] = type(error).__name__
                report.add(
                    f"{alias} semasi",
                    WARN,
                    f"{type(error).__name__}; servis gelistirme adayinda kullanilmayacak",
                    duration_ms=elapsed,
                    sap_calls=calls,
                )
            else:
                check = verify_contract(capability, contract)
                row.update(
                    {
                        "metadata_version": contract.version,
                        "contract_ok": check.contract_ok,
                        "missing_entity_sets": list(check.missing_entity_sets),
                        "missing_properties": {
                            key: list(value)
                            for key, value in check.missing_properties.items()
                        },
                        "entities": _entity_inventory(contract),
                        "actions": list(contract.actions),
                        "functions": list(contract.functions),
                    }
                )
                report.add(
                    f"{alias} semasi",
                    "PASS" if check.contract_ok else WARN,
                    (
                        f"entity={len(contract.entity_sets)}, action={len(contract.actions)}, "
                        f"function={len(contract.functions)}"
                    ),
                    duration_ms=elapsed,
                    sap_calls=calls,
                )
            services.append(row)

        available = sum(bool(row.get("entities")) for row in services)
        compatible = sum(bool(row.get("contract_ok")) for row in services)
        report.check(
            "Envanter gelistirme analizi icin yeterli",
            available > 0,
            f"semasi_okunan={available}/{len(services)}, kontrati_uyumlu={compatible}",
            critical=False,
        )
        artifact_path = (
            Path(args.out).with_name("service_inventory.json")
            if args.out
            else Path("artifacts/cal/service_inventory.json")
        )
        write_json(
            artifact_path,
            {
                "schema_version": 1,
                "raw_values_persisted": False,
                "physical_metadata_requests": sum(row[2] for row in contracts.values()),
                "service_alias_count": len(services),
                "physical_service_count": len(contracts),
                "services": services,
            },
        )
        report.add(
            "Guvenli servis envanteri kaydedildi",
            "PASS",
            str(artifact_path),
        )
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/07-inventory.json")


if __name__ == "__main__":
    raise SystemExit(main())
