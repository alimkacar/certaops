#!/usr/bin/env python3
"""Validate and summarize the realistic ABAP Trial compatibility fixture."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_FIXTURE = PROJECT_ROOT / "config" / "abap_trial_scenario.json"


@dataclass(frozen=True)
class Finding:
    check: str
    status: str
    detail: str


def _rows(entity_sets: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = entity_sets.get(name)
    return value if isinstance(value, list) else []


def validate_fixture(payload: dict[str, Any]) -> list[Finding]:
    from robotics_agent.adapters.ecc.capabilities import ECC_CAPABILITY_MANIFEST

    findings: list[Finding] = []
    entity_sets = payload.get("entity_sets")
    if not isinstance(entity_sets, dict):
        return [Finding("fixture.entity_sets", "FAIL", "entity_sets must be an object")]

    required: dict[str, set[str]] = {}
    for capability in ECC_CAPABILITY_MANIFEST.values():
        for entity_set, properties in capability.critical_properties.items():
            required.setdefault(entity_set, set()).update(properties)

    missing_sets = sorted(set(required) - set(entity_sets))
    findings.append(
        Finding(
            "contract.entity_sets",
            "PASS" if not missing_sets else "FAIL",
            f"covered={len(required)}" if not missing_sets else f"missing={missing_sets}",
        )
    )

    empty_sets: list[str] = []
    missing_fields: dict[str, list[str]] = {}
    for entity_set, properties in required.items():
        rows = _rows(entity_sets, entity_set)
        if not rows:
            empty_sets.append(entity_set)
            continue
        available = set().union(*(set(row) for row in rows if isinstance(row, dict)))
        missing = sorted(properties - available)
        if missing:
            missing_fields[entity_set] = missing
    findings.append(
        Finding(
            "contract.non_empty",
            "PASS" if not empty_sets else "FAIL",
            "all required entity sets contain rows" if not empty_sets else f"empty={empty_sets}",
        )
    )
    findings.append(
        Finding(
            "contract.critical_properties",
            "PASS" if not missing_fields else "FAIL",
            "all critical properties are represented"
            if not missing_fields
            else json.dumps(missing_fields, sort_keys=True),
        )
    )

    seeds = payload.get("seeds") if isinstance(payload.get("seeds"), dict) else {}
    seed_checks = {
        "material": ("MaterialSet", "Material"),
        "vendor": ("SupplierSet", "Supplier"),
        "po": ("PurchaseOrderSet", "PurchaseOrder"),
        "invoice": ("SupplierInvoiceSet", "SupplierInvoice"),
    }
    missing_seeds: list[str] = []
    for seed_name, (entity_set, field) in seed_checks.items():
        value = seeds.get(seed_name)
        if not value or not any(row.get(field) == value for row in _rows(entity_sets, entity_set)):
            missing_seeds.append(seed_name)
    findings.append(
        Finding(
            "scenario.seeds",
            "PASS" if not missing_seeds else "FAIL",
            "all five deterministic seeds resolve"
            if not missing_seeds
            else f"unresolved={missing_seeds}",
        )
    )

    material = seeds.get("material")
    mrp_rows = [
        row for row in _rows(entity_sets, "SupplyDemandSet") if row.get("Material") == material
    ]
    net_mrp = sum(float(row.get("Quantity") or 0) for row in mrp_rows)
    findings.append(
        Finding(
            "scenario.real_shortage",
            "PASS" if net_mrp < 0 else "FAIL",
            f"net MRP quantity={net_mrp:g}; expected a negative shortage",
        )
    )

    po = seeds.get("po")
    po_items = [
        row for row in _rows(entity_sets, "PurchaseOrderItemSet")
        if row.get("PurchaseOrder") == po
    ]
    gr_qty = sum(
        float(row.get("Quantity") or 0)
        for row in _rows(entity_sets, "GoodsReceiptSet")
        if row.get("PurchaseOrder") == po
    )
    inv_qty = sum(
        float(row.get("Quantity") or 0)
        for row in _rows(entity_sets, "SupplierInvoiceItemSet")
        if row.get("PurchaseOrder") == po
    )
    linked = bool(po_items and gr_qty > 0 and inv_qty > gr_qty)
    findings.append(
        Finding(
            "scenario.p2p_variance",
            "PASS" if linked else "FAIL",
            f"PO items={len(po_items)}, received={gr_qty:g}, invoiced={inv_qty:g}",
        )
    )

    invoice = seeds.get("invoice")
    blocks = [
        row for row in _rows(entity_sets, "InvoiceBlockSet")
        if row.get("SupplierInvoice") == invoice and row.get("ToleranceKey")
    ]
    findings.append(
        Finding(
            "scenario.deterministic_invoice_block",
            "PASS" if blocks else "FAIL",
            f"block_count={len(blocks)}",
        )
    )

    return findings


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    findings = validate_fixture(payload)
    return {
        "scenario": payload.get("scenario", {}),
        "seeds": payload.get("seeds", {}),
        "entity_set_count": len(payload.get("entity_sets") or {}),
        "row_count": sum(
            len(rows) for rows in (payload.get("entity_sets") or {}).values()
            if isinstance(rows, list)
        ),
        "findings": [asdict(row) for row in findings],
        "summary": {
            "PASS": sum(row.status == "PASS" for row in findings),
            "FAIL": sum(row.status == "FAIL" for row in findings),
        },
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    scenario = report.get("scenario") or {}
    lines = [
        f"# {scenario.get('title', 'ABAP Trial scenario')}",
        "",
        str(scenario.get("description", "")),
        "",
        f"- Entity sets: {report['entity_set_count']}",
        f"- Seed rows: {report['row_count']}",
        f"- Checks: PASS={report['summary']['PASS']} FAIL={report['summary']['FAIL']}",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for finding in report["findings"]:
        lines.append(
            f"| {finding['check']} | {finding['status']} | {finding['detail']} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--out", default="")
    parser.add_argument("--markdown", default="")
    args = parser.parse_args(argv)

    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = PROJECT_ROOT / fixture
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    report = build_report(payload)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = PROJECT_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered + "\n", encoding="utf-8")
    if args.markdown:
        md = Path(args.markdown)
        if not md.is_absolute():
            md = PROJECT_ROOT / md
        write_markdown(md, report)
    return 1 if report["summary"]["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
