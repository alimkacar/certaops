#!/usr/bin/env python3
"""CAL is verisini ham deger saklamadan, sinirli orneklerle profiller."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cal_acceptance_lib import (
    WARN,
    add_common_args,
    load_profile,
    make_report,
    supplied_from_args,
    target_summary,
    timed,
)
from cal_analysis_lib import canonical_values, join_profile, profile_records, rows_from, write_json

JOIN_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("product_to_valuation", "product", "valuation", "material"),
    ("product_to_stock", "product", "stock", "material"),
    ("product_to_mrp", "product", "mrp", "material"),
    ("product_to_info_records", "product", "info_records", "material"),
    ("info_records_to_vendor", "info_records", "vendor", "vendor"),
    ("vendor_to_score", "vendor", "supplier_score", "vendor"),
    ("purchase_order_to_items", "purchase_orders", "po_items", "po"),
    ("items_to_schedule", "po_items", "schedule_lines", "po_item"),
    ("items_to_receipts", "po_items", "goods_receipts", "po_item"),
    ("items_to_invoices", "po_items", "supplier_invoices", "po_item"),
    ("purchase_order_to_receipts", "purchase_orders", "goods_receipts", "po"),
    ("purchase_order_to_invoices", "purchase_orders", "supplier_invoices", "po"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args(argv)
    limit = min(50, max(1, args.sample_limit))
    report, started = make_report("CAL-08", "Guvenli veri ve join profili")

    try:
        load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/08-profile.json")

    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    backend = build_backend(settings)
    report.target = target_summary(settings, backend)
    supplied = supplied_from_args(args)
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {}
    datasets: dict[str, dict[str, Any]] = {}
    total_calls = 0
    total_latency = 0

    def sample(name: str, source: str, call: Callable[[], Any]) -> list[dict[str, Any]]:
        nonlocal total_calls, total_latency
        value, elapsed, calls, error = timed(call, backend)
        total_calls += calls
        total_latency += elapsed
        rows = rows_from(value, limit=limit) if error is None else []
        rows_by_dataset[name] = rows
        profile = profile_records(rows)
        profile.update(
            {
                "source": source,
                "latency_ms": elapsed,
                "sap_calls": calls,
                "error": type(error).__name__ if error is not None else "",
            }
        )
        datasets[name] = profile
        report.add(
            name,
            "PASS" if rows else WARN,
            (
                f"ornek={len(rows)}, alan={profile['field_count']}"
                if error is None
                else f"{type(error).__name__}; ham hata mesaji kaydedilmedi"
            ),
            duration_ms=elapsed,
            sap_calls=calls,
        )
        return rows

    try:
        order_rows = sample(
            "purchase_orders",
            "get_purchase_orders",
            lambda: backend.get_purchase_orders(
                material_id=supplied.get("material") or None,
                vendor_id=supplied.get("vendor") or None,
                only_open=False,
                limit=limit,
            ),
        )
        first_order = order_rows[0] if order_rows else {}
        material_id = supplied.get("material") or _first(first_order, "material_id")
        vendor_id = supplied.get("vendor") or _first(first_order, "vendor_id")
        po_id = supplied.get("po") or _first(first_order, "po_id")

        if material_id:
            sample("product", "get_material", lambda: backend.get_material(material_id))
            sample(
                "classification",
                "get_material_classification",
                lambda: backend.get_material_classification(material_id),
            )
            sample("valuation", "get_valuation", lambda: backend.get_valuation(material_id))
            sample("stock", "get_stock", lambda: backend.get_stock([material_id]))
            sample(
                "mrp",
                "get_supply_demand",
                lambda: backend.get_supply_demand(material_id, horizon_days=180),
            )
            info_rows = sample(
                "info_records",
                "get_info_records",
                lambda: backend.get_info_records(material_id),
            )
            if not vendor_id:
                vendor_ids = sorted(canonical_values(info_rows, "vendor"))
                vendor_id = vendor_ids[0] if vendor_ids else ""
        else:
            for name, source in (
                ("product", "get_material"),
                ("classification", "get_material_classification"),
                ("valuation", "get_valuation"),
                ("stock", "get_stock"),
                ("mrp", "get_supply_demand"),
                ("info_records", "get_info_records"),
            ):
                sample(name, source, lambda: [])

        if vendor_id:
            sample("vendor", "get_vendor_master", lambda: backend.get_vendor_master(vendor_id))
            sample(
                "supplier_score",
                "get_supplier_score",
                lambda: backend.get_supplier_score(vendor_id),
            )
        else:
            sample("vendor", "get_vendor_master", lambda: [])
            sample("supplier_score", "get_supplier_score", lambda: [])

        if po_id:
            sample("po_items", "get_purchase_order_items", lambda: backend.get_purchase_order_items(po_id))
            sample("schedule_lines", "get_schedule_lines", lambda: backend.get_schedule_lines(po_id))
            sample(
                "goods_receipts",
                "get_goods_receipts",
                lambda: backend.get_goods_receipts(po_id=po_id, limit=limit),
            )
            sample(
                "supplier_invoices",
                "get_supplier_invoices",
                lambda: backend.get_supplier_invoices(po_id=po_id, limit=limit),
            )
        else:
            for name, source in (
                ("po_items", "get_purchase_order_items"),
                ("schedule_lines", "get_schedule_lines"),
                ("goods_receipts", "get_goods_receipts"),
                ("supplier_invoices", "get_supplier_invoices"),
            ):
                sample(name, source, lambda: [])

        joins = {
            name: join_profile(
                rows_by_dataset.get(left, []),
                rows_by_dataset.get(right, []),
                canonical,
            )
            for name, left, right, canonical in JOIN_SPECS
        }
        for name, value in joins.items():
            report.add(
                name,
                "PASS" if value["intersection_count"] else WARN,
                (
                    f"ortusen_anahtar={value['intersection_count']}, "
                    f"sol_kapsam=%{value['left_coverage_pct']}"
                ),
            )

        non_empty = sum(bool(value["sample_count"]) for value in datasets.values())
        report.check(
            "Profil gelistirme analizi icin yeterli",
            non_empty >= 3,
            f"dolu_veri_kumesi={non_empty}/{len(datasets)}",
            critical=False,
        )
        artifact_path = (
            Path(args.out).with_name("data_profile.json")
            if args.out
            else Path("artifacts/cal/data_profile.json")
        )
        write_json(
            artifact_path,
            {
                "schema_version": 1,
                "raw_values_persisted": False,
                "sample_limit": limit,
                "observed_seed_presence": {
                    "material": bool(material_id),
                    "vendor": bool(vendor_id),
                    "po": bool(po_id),
                },
                "total_sap_calls": total_calls,
                "total_latency_ms": total_latency,
                "datasets": datasets,
                "joins": joins,
            },
        )
        report.add("Guvenli veri profili kaydedildi", "PASS", str(artifact_path))
    finally:
        backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/08-profile.json")


def _first(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return str(value).strip() if value not in (None, "") else ""


if __name__ == "__main__":
    raise SystemExit(main())
