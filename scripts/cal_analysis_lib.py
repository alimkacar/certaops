#!/usr/bin/env python3
"""CAL canli kesfinden guvenli veri profili ve tool firsati uretir.

Bu modulun temel guvenlik kurali ham SAP degerlerini rapora yazmamaktir.
Yalniz alan adlari, doluluk/benzersizlik sayilari, join kapsami ve performans
olculeri kalici hale getirilir. Gercek is anahtarlari join hesabi sirasinda
bellekte kullanilir ve ciktiya tasinmaz.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "material": ("material_id", "material", "Material", "Product", "product"),
    "vendor": (
        "vendor_id",
        "vendor",
        "supplier",
        "Supplier",
        "InvoicingParty",
    ),
    "po": ("po_id", "purchase_order", "PurchaseOrder", "po_ids"),
    "item": (
        "item_no",
        "po_item",
        "PurchaseOrderItem",
        "PurchasingDocumentItem",
    ),
    "invoice": ("invoice_id", "SupplierInvoice", "supplier_invoice"),
    "plant": ("plant", "Plant", "MRPPlant"),
    "wbs": ("wbs_element", "WBSElementExternalID", "WBSElement"),
}


OPPORTUNITIES: tuple[dict[str, Any], ...] = (
    {
        "name": "sap_procurement_exception_cockpit",
        "title": "Satinalma istisna kokpiti",
        "business_value": 5,
        "required_services": (
            ("purchase_order", "purchase_order_v2"),
            ("material_document",),
            ("supplier_invoice",),
        ),
        "optional_services": (("mrp",), ("supplier_score",)),
        "required_datasets": (
            "purchase_orders",
            "po_items",
            "schedule_lines",
            "goods_receipts",
            "supplier_invoices",
        ),
        "required_joins": (
            "purchase_order_to_items",
            "items_to_schedule",
            "items_to_receipts",
            "items_to_invoices",
        ),
        "next_step": (
            "Gecikme, acik miktar, GR/IR farki ve blokajlari tek deterministik "
            "oncelik puaninda birlestir. Tarama mutlaka tesis/tarih/limit ile sinirli olmali."
        ),
    },
    {
        "name": "sap_shortage_to_pr_recommendation",
        "title": "Eksikten PR taslagina onerisi",
        "business_value": 5,
        "required_services": (
            ("product",),
            ("stock",),
            ("mrp",),
            ("inforecord",),
            ("supplier",),
            ("purchase_requisition", "purchase_requisition_v2"),
        ),
        "optional_services": (("availability",), ("supplier_score",)),
        "required_datasets": ("product", "stock", "mrp", "info_records", "vendor"),
        "required_joins": (
            "product_to_stock",
            "product_to_mrp",
            "product_to_info_records",
            "info_records_to_vendor",
        ),
        "next_step": (
            "Eksik miktar/tarih ile MOQ ve teslim suresini hesapla; sonuc yalniz oneridir ve "
            "mevcut sap_pr_prepare kapisina girdi olur."
        ),
    },
    {
        "name": "sap_gr_ir_reconciliation",
        "title": "Toplu GR/IR mutabakati",
        "business_value": 5,
        "required_services": (
            ("purchase_order", "purchase_order_v2"),
            ("material_document",),
            ("supplier_invoice",),
        ),
        "optional_services": (),
        "required_datasets": ("po_items", "goods_receipts", "supplier_invoices"),
        "required_joins": ("items_to_receipts", "items_to_invoices"),
        "next_step": (
            "PO/kalem bazinda net 101-102-122-162 miktarini RSEG miktariyla karsilastir; "
            "belgesiz eslesme yapma."
        ),
    },
    {
        "name": "sap_supplier_risk_exposure",
        "title": "Tedarikci risk maruziyeti",
        "business_value": 4,
        "required_services": (
            ("supplier",),
            ("supplier_score",),
            ("purchase_order", "purchase_order_v2"),
            ("material_document",),
            ("supplier_invoice",),
        ),
        "optional_services": (("mrp",),),
        "required_datasets": (
            "vendor",
            "supplier_score",
            "purchase_orders",
            "goods_receipts",
            "supplier_invoices",
        ),
        "required_joins": (
            "vendor_to_score",
            "purchase_order_to_receipts",
            "purchase_order_to_invoices",
        ),
        "next_step": (
            "Skorun yanina acik siparis degeri, gecikme ve bloke fatura maruziyetini ekle; "
            "tahmini skor alanlarini karar puanindan ayir."
        ),
    },
    {
        "name": "sap_inventory_value_risk",
        "title": "Stok degeri ve eksik malzeme riski",
        "business_value": 4,
        "required_services": (("product",), ("valuation",), ("stock",), ("mrp",)),
        "optional_services": (("availability",),),
        "required_datasets": ("product", "valuation", "stock", "mrp"),
        "required_joins": (
            "product_to_valuation",
            "product_to_stock",
            "product_to_mrp",
        ),
        "next_step": (
            "Serbest/kalite/bloke stok miktarini dogru price-unit ile degerle; tuketim gecmisi "
            "olmadan yavas hareket iddiasi uretme."
        ),
    },
    {
        "name": "sap_approval_bottleneck_monitor",
        "title": "Onay darboğazi izleme",
        "business_value": 4,
        "required_services": (("workflow",),),
        "optional_services": (),
        "required_datasets": ("workflow_steps",),
        "required_joins": (),
        "next_step": (
            "Flexible Workflow CDS veya BPA task API kaynagi kanitlanmadan gelistirme yapma."
        ),
    },
)


def rows_from(value: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Pydantic/dict/list sonucunu sinirli satir listesine cevirir."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    rows: list[dict[str, Any]] = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, Mapping):
            row = dict(item)
        elif hasattr(item, "model_dump"):
            row = item.model_dump(mode="json")
        elif hasattr(item, "__dict__"):
            row = {key: val for key, val in vars(item).items() if not key.startswith("_")}
        else:
            continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def profile_records(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Ham degerleri cikarmadan alan doluluk ve benzersizlik profili uretir."""
    total = len(rows)
    fields = sorted({str(key) for row in rows for key in row})
    field_profiles: dict[str, dict[str, Any]] = {}
    for field in fields:
        present = [row.get(field) for row in rows if _present(row.get(field))]
        field_profiles[field] = {
            "non_null_count": len(present),
            "non_null_pct": round(len(present) / total * 100, 1) if total else 0.0,
            "distinct_count": len({_stable(value) for value in present}),
            "types": sorted({type(value).__name__ for value in present}),
        }

    join_keys: dict[str, dict[str, Any]] = {}
    for canonical in CANONICAL_ALIASES:
        values = canonical_values(rows, canonical)
        if values:
            join_keys[canonical] = {
                "present_count": sum(
                    1 for row in rows if canonical_values([row], canonical)
                ),
                "coverage_pct": round(
                    sum(1 for row in rows if canonical_values([row], canonical))
                    / total
                    * 100,
                    1,
                )
                if total
                else 0.0,
                "distinct_count": len(values),
            }
    po_items = canonical_values(rows, "po_item")
    if po_items:
        join_keys["po_item"] = {
            "present_count": sum(1 for row in rows if canonical_values([row], "po_item")),
            "coverage_pct": round(
                sum(1 for row in rows if canonical_values([row], "po_item"))
                / total
                * 100,
                1,
            )
            if total
            else 0.0,
            "distinct_count": len(po_items),
        }
    return {
        "sample_count": total,
        "field_count": len(fields),
        "fields": field_profiles,
        "join_keys": join_keys,
        "raw_values_persisted": False,
    }


def canonical_values(rows: Sequence[Mapping[str, Any]], canonical: str) -> set[str]:
    """Join icin degerleri bellekte normalize eder; cagiran bunlari kalici yazmaz."""
    if canonical == "po_item":
        values: set[str] = set()
        for row in rows:
            po_values = _values_for_aliases(row, CANONICAL_ALIASES["po"])
            item_values = _values_for_aliases(row, CANONICAL_ALIASES["item"])
            values.update(f"{po}/{item}" for po in po_values for item in item_values)
            quantities = row.get("po_item_quantities")
            if isinstance(quantities, Mapping):
                values.update(str(key).strip() for key in quantities if str(key).strip())
        return values
    aliases = CANONICAL_ALIASES.get(canonical, (canonical,))
    values: set[str] = set()
    for row in rows:
        values.update(_values_for_aliases(row, aliases))
    return values


def join_profile(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
    canonical: str,
) -> dict[str, Any]:
    """Iki veri kumesindeki gercek anahtar ortusmesini degerleri aciklamadan olcer."""
    left_values = canonical_values(left, canonical)
    right_values = canonical_values(right, canonical)
    intersection = left_values & right_values
    return {
        "canonical_key": canonical,
        "left_distinct": len(left_values),
        "right_distinct": len(right_values),
        "intersection_count": len(intersection),
        "left_coverage_pct": round(len(intersection) / len(left_values) * 100, 1)
        if left_values
        else 0.0,
        "right_coverage_pct": round(len(intersection) / len(right_values) * 100, 1)
        if right_values
        else 0.0,
        "raw_values_persisted": False,
    }


def infer_join_fields(properties: Iterable[str]) -> dict[str, list[str]]:
    """Metadata alanlarini standart is anahtari ailelerine esler."""
    fields = sorted({str(prop) for prop in properties})
    inferred: dict[str, list[str]] = {}
    for canonical, aliases in CANONICAL_ALIASES.items():
        alias_lowers = {alias.lower() for alias in aliases}
        matches = [field for field in fields if field.lower() in alias_lowers]
        if matches:
            inferred[canonical] = matches
    return inferred


def evaluate_opportunities(
    inventory: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    core_tests_passed: bool,
) -> dict[str, Any]:
    """Canli kanitlari aday tool'lara map eder ve gelistirme kapisini hesaplar."""
    services = {
        str(row.get("alias")): bool(row.get("contract_ok"))
        for row in inventory.get("services", [])
        if isinstance(row, Mapping)
    }
    datasets = profile.get("datasets", {}) if isinstance(profile, Mapping) else {}
    joins = profile.get("joins", {}) if isinstance(profile, Mapping) else {}

    candidates: list[dict[str, Any]] = []
    for spec in OPPORTUNITIES:
        required_groups = spec["required_services"]
        service_hits = [any(services.get(alias, False) for alias in group) for group in required_groups]
        dataset_hits = [
            bool((datasets.get(name) or {}).get("sample_count"))
            and not bool((datasets.get(name) or {}).get("error"))
            for name in spec["required_datasets"]
        ]
        join_hits = [
            bool((joins.get(name) or {}).get("intersection_count"))
            for name in spec["required_joins"]
        ]
        service_ratio = _ratio(service_hits)
        dataset_ratio = _ratio(dataset_hits)
        join_ratio = _ratio(join_hits)
        optional_groups = spec["optional_services"]
        optional_ratio = _ratio(
            [any(services.get(alias, False) for alias in group) for group in optional_groups]
        )
        if not optional_groups:
            optional_ratio = 1.0
        perf_ratio = _performance_ratio(datasets, spec["required_datasets"])
        score = round(
            40 * (spec["business_value"] / 5)
            + 25 * service_ratio
            + 20 * dataset_ratio
            + 10 * join_ratio
            + 5 * perf_ratio
        )

        all_required = all(service_hits) and all(dataset_hits) and all(join_hits)
        if not core_tests_passed:
            status = "DEFERRED"
        elif all_required:
            status = "READY"
        elif service_ratio >= 0.6 and dataset_ratio >= 0.5:
            status = "CONDITIONAL"
        else:
            status = "BLOCKED"

        candidates.append(
            {
                "name": spec["name"],
                "title": spec["title"],
                "status": status,
                "score": score,
                "business_value": spec["business_value"],
                "service_readiness_pct": round(service_ratio * 100),
                "data_readiness_pct": round(dataset_ratio * 100),
                "join_evidence_pct": round(join_ratio * 100),
                "optional_service_pct": round(optional_ratio * 100),
                "missing_service_groups": [
                    list(group) for group, hit in zip(required_groups, service_hits, strict=True) if not hit
                ],
                "missing_datasets": [
                    name
                    for name, hit in zip(spec["required_datasets"], dataset_hits, strict=True)
                    if not hit
                ],
                "missing_joins": [
                    name
                    for name, hit in zip(spec["required_joins"], join_hits, strict=True)
                    if not hit
                ],
                "next_step": spec["next_step"],
            }
        )

    candidates.sort(key=lambda row: (-int(row["score"]), str(row["name"])))
    ready = [row["name"] for row in candidates if row["status"] == "READY"]
    return {
        "core_tests_passed": core_tests_passed,
        "development_gate": "OPEN" if core_tests_passed and ready else "CLOSED",
        "ready_candidates": ready,
        "candidates": candidates,
        "policy": (
            "Yeni tool yalniz core CAL testleri gectiyse, gerekli servis/veri/join kaniti "
            "mevcutsa ve ham SAP degeri rapora yazilmiyorsa gelistirilir."
        ),
    }


def render_opportunity_markdown(result: Mapping[str, Any]) -> str:
    gate = result.get("development_gate", "CLOSED")
    lines = [
        "# CAL sonrasi tool gelistirme notlari",
        "",
        f"- Gelistirme kapisi: **{gate}**",
        f"- Cekirdek canli testler: {'gecti' if result.get('core_tests_passed') else 'eksik/basarisiz'}",
        "- Ham SAP kayitlari saklanmadi; yalniz sema, doluluk, join ve performans olculeri kullanildi.",
        "",
        "| Aday | Durum | Puan | Servis | Veri | Join |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in result.get("candidates", []):
        lines.append(
            f"| `{row['name']}` | {row['status']} | {row['score']} "
            f"| %{row['service_readiness_pct']} | %{row['data_readiness_pct']} "
            f"| %{row['join_evidence_pct']} |"
        )
    lines.extend(["", "## Aday bazinda sonraki adim", ""])
    for row in result.get("candidates", []):
        missing = []
        if row.get("missing_service_groups"):
            missing.append(f"servis={row['missing_service_groups']}")
        if row.get("missing_datasets"):
            missing.append(f"veri={row['missing_datasets']}")
        if row.get("missing_joins"):
            missing.append(f"join={row['missing_joins']}")
        suffix = f" Eksik: {'; '.join(missing)}." if missing else ""
        lines.append(
            f"- **`{row['name']}` / {row['status']}**: {row['next_step']}{suffix}"
        )
    lines.extend(
        [
            "",
            "## Gelistirme kurali",
            "",
            str(result.get("policy", "")),
            "",
        ]
    )
    return "\n".join(lines)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _stable(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _values_for_aliases(row: Mapping[str, Any], aliases: Iterable[str]) -> set[str]:
    values: set[str] = set()
    for alias in aliases:
        if alias not in row:
            continue
        value = row.get(alias)
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        for candidate in candidates:
            if _present(candidate):
                values.add(str(candidate).strip())
    return {value for value in values if value}


def _ratio(values: Sequence[bool]) -> float:
    return sum(values) / len(values) if values else 1.0


def _performance_ratio(datasets: Mapping[str, Any], names: Iterable[str]) -> float:
    latencies = [
        float((datasets.get(name) or {}).get("latency_ms") or 0)
        for name in names
        if datasets.get(name)
    ]
    if not latencies:
        return 0.0
    average = sum(latencies) / len(latencies)
    if average <= 1000:
        return 1.0
    if average <= 3000:
        return 0.8
    if average <= 6000:
        return 0.5
    return 0.2
