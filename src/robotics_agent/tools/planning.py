"""Planlama tool'lari: gercek ATP ve MRP shortage aciklamasi.

"Stok eksi rezervasyon" bir ATP teyidi degildir. Bu iki tool
farklarini acikca kurar:

  sap_atp_check              -> tarih ve miktar bazli TEYIT (API_PRODUCT_AVAILY_INFO)
  sap_mrp_shortage_explain   -> eksigin hangi arz/talep elementinden dogdugu
                                (API_MRP_MATERIALS_SRV_01/SupplyDemandItems)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..adapters.sap import SAPError, SAPNotSupported
from ..contracts import (
    DETAIL_SCHEMA,
    SCOPE_SAP_READ,
    SCOPE_SAP_SIMULATE,
    RiskTier,
    ToolResult,
    page_limit,
    resolve_detail,
)
from .registry import ToolContext, tool


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
@tool(
    name="sap_atp_check",
    group="planlama",
    domain="planning",
    risk_tier=RiskTier.R1,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=900,
    description=(
        "GERCEK ATP kontrolu: istenen miktarin hangi tarihte teyit edilebilecegini SAP'in "
        "availability kontrol kuralina gore dondurur (API_PRODUCT_AVAILY_INFO). Kismi teyit "
        "satirlarini, tam teyit tarihini, gecikme gununu ve teyidi saglayan arz elementini "
        "verir. Musteriye termin taahhudu verilecekse kullanilacak tool budur; stok fotografi "
        "(sap_stock_overview) taahhut icin yeterli degildir."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "description": "Kontrol edilecek kalemler.",
                "items": {
                    "type": "object",
                    "properties": {
                        "material_id": {"type": "string"},
                        "quantity": {"type": "number"},
                        "required_date": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["material_id", "quantity"],
                },
            },
            "plant": {"type": "string", "description": "Tesis. Bos ise varsayilan tesis."},
            "detail": DETAIL_SCHEMA,
        },
        "required": ["requests"],
    },
)
def sap_atp_check(
    ctx: ToolContext,
    requests: list[dict[str, Any]],
    plant: str | None = None,
    detail: str = "standard",
) -> ToolResult | dict[str, Any]:
    if not requests:
        return {"error": "En az bir kontrol kalemi gerekli."}

    level = resolve_detail(detail)
    rows: list[dict[str, Any]] = []
    shortages: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    source_apis: set[str] = set()

    for item in requests:
        material_id = str(item.get("material_id", "")).strip()
        if not material_id:
            continue
        quantity = float(item.get("quantity") or 0)
        need_by = _parse_date(item.get("required_date"))

        try:
            result = ctx.sap.check_atp(
                material_id, quantity=quantity, requested_date=need_by, plant=plant
            )
        except SAPNotSupported as exc:
            unsupported.append({"material_id": material_id, "reason": str(exc)})
            continue
        except SAPError as exc:
            rows.append({"material_id": material_id, "error": str(exc), "sap_code": exc.code})
            continue

        source_apis.add(result.source_api)
        row: dict[str, Any] = {
            "material_id": result.material_id,
            "plant": result.plant,
            "requested_qty": result.requested_qty,
            "requested_date": result.requested_date.isoformat() if result.requested_date else None,
            "confirmed_qty": result.confirmed_qty,
            "shortfall_qty": result.shortfall_qty or None,
            "fully_confirmed": result.fully_confirmed,
            "full_confirmation_date": (
                result.full_confirmation_date.isoformat() if result.full_confirmation_date else None
            ),
            "late_by_days": result.late_by_days or None,
            "unit": result.unit,
            "calendar_considered": result.calendar_considered,
        }
        if level != "summary":
            row["schedule_lines"] = [
                {
                    "date": line.confirmed_date.isoformat(),
                    "qty": line.confirmed_qty,
                    "supply": line.supply_element,
                }
                for line in result.schedule_lines[: page_limit(level, None, default=10)]
            ]
            if result.messages:
                row["notes"] = result.messages
        rows.append(row)

        if not result.fully_confirmed or result.late_by_days:
            shortages.append(
                {
                    "material_id": result.material_id,
                    "shortfall_qty": result.shortfall_qty,
                    "late_by_days": result.late_by_days,
                    "full_confirmation_date": (
                        result.full_confirmation_date.isoformat()
                        if result.full_confirmation_date
                        else None
                    ),
                }
            )

    critical = max(shortages, key=lambda s: (s["late_by_days"], s["shortfall_qty"]), default=None)
    data: dict[str, Any] = {
        "checked_on": date.today().isoformat(),
        "plant": plant or ctx.settings.sap.plant,
        "results": rows,
        "shortage_count": len(shortages),
        "shortages": shortages,
        "critical_item": critical,
    }
    if unsupported:
        data["unsupported"] = unsupported
        data["remediation"] = (
            "ATP servisi bu sistemde aktif degil. sap_discover_capabilities ile kontrol edin; "
            "aktive edilmeden termin taahhudu verilmemeli."
        )
    data["recommendation"] = (
        "Eksik/gecikmeli kalemler icin sap_mrp_shortage_explain ile kok nedeni cikarin, "
        "sonra alternatif kaynak/tarih senaryolarini degerlendirin."
        if shortages
        else "Tum kalemler istenen tarihte teyit edilebiliyor."
    )

    result = ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            ", ".join(sorted(source_apis)) or "atp",
            record_count=len(rows),
            notes=("Teyit tarihleri SAP availability kontrol kuralindan gelir.",),
        ),
        returned_count=len(rows),
    )
    if unsupported:
        result.warn("Bazi kalemler icin ATP servisi yok; bu kalemler teyit edilmedi.")
    return result


# ---------------------------------------------------------------------------
@tool(
    name="sap_mrp_shortage_explain",
    group="planlama",
    domain="planning",
    risk_tier=RiskTier.R1,
    required_scopes=(SCOPE_SAP_READ, SCOPE_SAP_SIMULATE),
    result_token_budget=1100,
    description=(
        "Bir malzemedeki eksigin HANGI arz/talep elementinden dogdugunu aciklar "
        "(API_MRP_MATERIALS_SRV_01/SupplyDemandItems). Kumulatif kullanilabilirlik egrisini "
        "kurar, ilk negatife dustugu tarihi (shortage date) ve o tarihe kadarki elementleri "
        "gosterir. 'Neden eksik?' sorusunun cevabi buradadir; sap_atp_check 'ne zaman teyit "
        "edilir?' sorusunu cevaplar."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "material_id": {"type": "string"},
            "plant": {"type": "string"},
            "horizon_days": {
                "type": "integer",
                "description": "Kac gunluk ufuk incelenecek (varsayilan 180).",
                "default": 180,
            },
            "additional_demand": {
                "type": "number",
                "description": "Planlanan yeni ihtiyac miktari; egriye talep olarak eklenir.",
            },
            "additional_demand_date": {"type": "string", "description": "YYYY-MM-DD"},
            "detail": DETAIL_SCHEMA,
        },
        "required": ["material_id"],
    },
)
def sap_mrp_shortage_explain(
    ctx: ToolContext,
    material_id: str,
    plant: str | None = None,
    horizon_days: int = 180,
    additional_demand: float | None = None,
    additional_demand_date: str | None = None,
    detail: str = "standard",
) -> ToolResult | dict[str, Any]:
    level = resolve_detail(detail)
    try:
        items = ctx.sap.get_supply_demand(material_id, plant=plant, horizon_days=horizon_days)
    except SAPNotSupported as exc:
        return {
            "error": str(exc),
            "remediation": exc.hint,
            "denial_code": "CAPABILITY_NOT_SUPPORTED",
        }

    today = date.today()
    extra_date = _parse_date(additional_demand_date) or today
    timeline: list[dict[str, Any]] = []
    for item in items:
        timeline.append(
            {
                "date": (item.availability_date or today).isoformat(),
                "element": item.mrp_element,
                "element_id": item.element_id or None,
                "quantity": item.quantity,
                "description": item.description or None,
                "wbs_element": item.wbs_element,
            }
        )
    if additional_demand:
        timeline.append(
            {
                "date": extra_date.isoformat(),
                "element": "PLAN",
                "quantity": -abs(float(additional_demand)),
                "description": "Degerlendirilen yeni ihtiyac (SAP'ta kayitli degil)",
            }
        )
    timeline.sort(key=lambda row: (row["date"], -row["quantity"]))

    # Kumulatif kullanilabilirlik egrisi
    running = 0.0
    shortage_date: str | None = None
    max_shortage = 0.0
    drivers: list[dict[str, Any]] = []
    for row in timeline:
        running = round(running + float(row["quantity"]), 3)
        row["cumulative"] = running
        if running < 0:
            if shortage_date is None:
                shortage_date = row["date"]
            if abs(running) > max_shortage:
                max_shortage = abs(running)
        if float(row["quantity"]) < 0:
            drivers.append(
                {
                    "date": row["date"],
                    "element": row["element"],
                    "quantity": row["quantity"],
                    "description": row.get("description"),
                }
            )

    supply_total = round(sum(r["quantity"] for r in timeline if r["quantity"] > 0), 3)
    demand_total = round(sum(-r["quantity"] for r in timeline if r["quantity"] < 0), 3)
    top_drivers = sorted(drivers, key=lambda d: d["quantity"])[:5]

    data: dict[str, Any] = {
        "material_id": material_id.upper(),
        "plant": plant or ctx.settings.sap.plant,
        "horizon_days": horizon_days,
        "supply_total": supply_total,
        "demand_total": demand_total,
        "net_position": round(supply_total - demand_total, 3),
        "shortage_date": shortage_date,
        "max_shortage_qty": round(max_shortage, 3) or None,
        "has_shortage": shortage_date is not None,
        "top_demand_drivers": top_drivers,
    }
    if level != "summary":
        limit = page_limit(level, None, default=25)
        data["timeline"] = timeline[:limit]
        if len(timeline) > limit:
            data["timeline_truncated"] = len(timeline) - limit

    if shortage_date:
        data["interpretation"] = (
            f"Kumulatif kullanilabilirlik {shortage_date} tarihinde negatife duser "
            f"(en yuksek eksik {max_shortage:g}). En buyuk talep kaynaklari yukarida listelendi."
        )
        data["next_steps"] = [
            "sap_atp_check ile taahhut edilebilir tarihi dogrulayin.",
            "Alternatif tedarikci/ikame veya tarih kaydirma senaryolarini karsilastirin.",
            "Kritikse sap_pr_prepare ile ek tedarik talebi hazirlayin.",
        ]
    else:
        data["interpretation"] = "Incelenen ufukta kumulatif eksik olusmuyor."

    return ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            "mrp:supply_demand",
            business_object=material_id.upper(),
            record_count=len(timeline),
            notes=(
                "Emniyet stogu ve rezervasyonlar talep olarak modellenmistir.",
            ),
        ),
        returned_count=len(timeline),
    )
