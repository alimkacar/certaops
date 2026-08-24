"""SAP ERP satinalma toollari.

Kapsam: malzeme arama ve 360 gorunum, stok fotografi, tedarikci TCO/skor
karsilastirmasi, satinalma talebi (prepare -> onay -> submit -> verify) ve
siparis takibi.

Yazma akisi bilerek ikiye ayrilmistir: `sap_pr_prepare`
hicbir kosulda yazmaz; `sap_pr_submit` onay kaniti, idempotency ve read-after-write
olmadan calismaz.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..adapters.bpa import ApprovalRequest
from ..adapters.sap import SAPNotSupported
from ..contracts import (
    DETAIL_SCHEMA,
    SCOPE_PR_APPROVE,
    SCOPE_PR_WRITE,
    SCOPE_SAP_PREPARE,
    SCOPE_SAP_READ,
    SCOPE_SAP_SIMULATE,
    RiskTier,
    ToolResult,
    resolve_detail,
)
from ..core import Verification, approval_payload_for, payload_hash
from ..sap.models import PurchaseRequisitionItem, ValidationFinding
from .registry import PerformanceBudget, ToolContext, tool

# Incoterm bazli tahmini ek lojistik/gumruk yuku (net fiyat uzerine oran)
INCOTERM_LANDED_ADDER = {"EXW": 0.11, "FOB": 0.08, "FCA": 0.08, "CIF": 0.04, "CIP": 0.04,
                         "DAP": 0.0, "DDP": 0.0}
# Hatali parcanin yeniden isleme/hurda carpani (parca fiyatinin kati)
DEFECT_COST_MULTIPLIER = 3.0
# Yillik sermaye maliyeti - odeme vadesi avantaji hesabinda kullanilir
COST_OF_CAPITAL = 0.12


def _material_key(value: str) -> str:
    """SAP malzeme numaralarini karsilastirmak icin normalize eder."""
    return str(value).strip().upper()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _payment_days(terms: str) -> int:
    digits = "".join(ch for ch in terms if ch.isdigit())
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
@tool(
    name="sap_search_materials",
    group="satinalma",
    domain="master_data",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    description=(
        "SAP malzeme ana verisinde (MARA/MAKT/MARC) arama yapar. Serbest metin, malzeme grubu "
        "ve siniflandirma karakteristik araliklari ile "
        "filtrelenebilir. Her sonuc icin fiyat, tedarik suresi, minimum siparis miktari ve "
        "SAP ana veri ozelliklerini dondurur; urun veya muhendislik onerisi uretmez."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "SAP malzeme ana verisinde serbest metin arama."},
            "material_group": {
                "type": "string",
                "description": "SAP malzeme grubu kodu.",
            },
            "attribute_filters": {
                "type": "object",
                "description": "SAP siniflandirma karakteristikleri icin sayisal min/max araliklari.",
                "additionalProperties": {"type": "array", "items": {"type": "number"}},
            },
            "plant": {"type": "string", "description": "Uretim yeri kodu. Bos birakilirsa varsayilan tesis."},
            "limit": {"type": "integer", "description": "Varsayilan 8. Genis tarama gerekiyorsa artirin.", "default": 8},
        },
        "required": [],
    },
)
def sap_search_materials(
    ctx: ToolContext,
    query: str = "",
    material_group: str | None = None,
    attribute_filters: dict[str, list[float]] | None = None,
    plant: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    parsed_filters: dict[str, tuple[float, float]] | None = None
    if attribute_filters:
        parsed_filters = {}
        for key, bounds in attribute_filters.items():
            if not isinstance(bounds, list | tuple) or len(bounds) != 2:
                return {"error": f"attribute_filters['{key}'] [alt, ust] seklinde 2 sayi olmali."}
            parsed_filters[key] = (float(bounds[0]), float(bounds[1]))

    materials = ctx.sap.search_materials(
        query,
        material_group=material_group,
        plant=plant,
        attribute_filters=parsed_filters,
        limit=limit,
    )

    return {
        "backend": ctx.sap.name,
        "result_count": len(materials),
        "materials": [
            {
                "material_id": m.material_id,
                "description": m.description,
                "material_group": m.material_group,
                "type": m.material_type,
                "unit": m.base_unit,
                "price": m.moving_avg_price,
                "currency": m.currency,
                "lead_time_days": m.planned_delivery_days,
                "min_order_qty": m.min_order_qty,
                "abc": m.abc_indicator,
                "weight_kg": m.gross_weight_kg,
                "attributes": m.attributes,
            }
            for m in materials
        ],
    }


# ---------------------------------------------------------------------------
@tool(
    name="sap_stock_overview",
    group="satinalma",
    domain="planning",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    performance_budget=PerformanceBudget(p95_ms=6000, max_sap_calls=6, max_records=200),
    description=(
        "Malzemelerin STOK FOTOGRAFINI dondurur (MARD serbest stok, rezervasyon, acik siparis, "
        "emniyet stogu) ve tedarik suresine gore kaba bir en erken tarih tahmini yapar. "
        "Bu bir ATP teyidi DEGILDIR: tarih bazli taahhut icin sap_atp_check, eksigin hangi "
        "arz/talep elementinden dogdugu icin sap_mrp_shortage_explain kullanilmalidir. "
        "Malzeme kritikligini (emniyet stogu alti, tek kaynak, uzun tedarik suresi) isaretler."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "material_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Kontrol edilecek malzeme numaralari.",
            },
            "required_quantities": {
                "type": "object",
                "description": "Malzeme basina gerekli miktar. Ornek: {\"ROB-6AX-20-1800\": 3}",
                "additionalProperties": {"type": "number"},
            },
            "required_date": {
                "type": "string",
                "description": "Ihtiyac tarihi (YYYY-MM-DD). Tedarik suresine gore yetisebilirlik degerlendirilir.",
            },
            "plant": {"type": "string"},
        },
        "required": ["material_ids"],
    },
)
def sap_stock_overview(
    ctx: ToolContext,
    material_ids: list[str],
    required_quantities: dict[str, float] | None = None,
    required_date: str | None = None,
    plant: str | None = None,
) -> dict[str, Any]:
    required_quantities = required_quantities or {}
    need_by = _parse_date(required_date)
    today = date.today()

    requested = [m for m in dict.fromkeys(material_ids) if m]
    levels = ctx.sap.get_stock(requested, plant=plant)
    masters = ctx.sap.get_materials(requested, plant=plant)
    records_by_material = ctx.sap.get_info_records_bulk(requested, plant=plant)

    masters_by_key = {_material_key(k): v for k, v in masters.items()}
    records_by_key = {_material_key(k): v for k, v in records_by_material.items()}
    not_found = [m for m in requested if _material_key(m) not in masters_by_key]
    levels = [lvl for lvl in levels if _material_key(lvl.material_id) in masters_by_key]

    rows: list[dict[str, Any]] = []
    shortages: list[dict[str, Any]] = []

    for level in levels:
        key = _material_key(level.material_id)
        material = masters_by_key.get(key)
        records = records_by_key.get(key, [])
        best_lead = min(
            (r.planned_delivery_days for r in records),
            default=material.planned_delivery_days if material else 30,
        )
        required = float(required_quantities.get(level.material_id, 0) or 0)
        shortfall = round(max(0.0, required - level.available_qty), 3)
        earliest = today + timedelta(days=best_lead)

        risk_flags: list[str] = []
        if level.below_safety_stock:
            risk_flags.append("emniyet stogunun altinda")
        if len(records) <= 1:
            risk_flags.append("tek kaynak (alternatif tedarikci yok)")
        if best_lead >= 90:
            risk_flags.append(f"uzun tedarik suresi ({best_lead} gun)")
        if level.blocked_qty > 0:
            risk_flags.append(f"{level.blocked_qty:g} adet bloke stok")

        on_time = None
        if shortfall > 0 and need_by:
            on_time = earliest <= need_by

        row = {
            "material_id": level.material_id,
            "description": material.description if material else "",
            "plant": level.plant,
            "unrestricted": level.unrestricted_qty,
            "reserved": level.reserved_qty,
            # Alan adi bilerek "available" degil: serbest stok eksi rezervasyon,
            # ATP teyidi degil.
            "unreserved": level.unreserved_qty,
            "quality_inspection": level.quality_inspection_qty,
            "blocked": level.blocked_qty,
            "on_order": level.on_order_qty,
            "safety_stock": level.safety_stock,
            "unit": level.unit,
            "required": required or None,
            "shortfall": shortfall or None,
            "best_lead_time_days": best_lead,
            "earliest_available": earliest.isoformat() if shortfall > 0 else "stokta",
            "meets_required_date": on_time,
            "risk_flags": risk_flags,
        }
        rows.append(row)
        if shortfall > 0:
            shortages.append(
                {
                    "material_id": level.material_id,
                    "shortfall": shortfall,
                    "earliest_available": earliest.isoformat(),
                    "late_by_days": (earliest - need_by).days if need_by and earliest > need_by else 0,
                }
            )

    critical_path = max(shortages, key=lambda s: s["late_by_days"], default=None)

    return {
        "checked_on": today.isoformat(),
        "required_date": required_date,
        "basis": "stok fotografi + tedarik suresi tahmini (ATP teyidi degil)",
        "materials": rows,
        "not_found": not_found,
        "shortage_count": len(shortages),
        "shortages": shortages,
        "critical_path_item": critical_path,
        "recommendation": (
            "Termin taahhudu vermeden once sap_atp_check ile tarih bazli teyit alin, "
            "eksigin kaynagi icin sap_mrp_shortage_explain calistirin; sonra "
            "sap_compare_vendors ve sap_pr_prepare ile talep hazirlayin."
            if shortages
            else "Tum kalemler mevcut stoktan karsilanabilir gorunuyor; taahhut icin sap_atp_check."
        ),
    }


# ---------------------------------------------------------------------------
@tool(
    name="sap_compare_vendors",
    group="satinalma",
    domain="procurement_read",
    risk_tier=RiskTier.R1,
    required_scopes=(SCOPE_SAP_READ, SCOPE_SAP_SIMULATE),
    performance_budget=PerformanceBudget(p95_ms=4000, max_sap_calls=5, max_records=200),
    description=(
        "Bir malzeme icin SAP satinalma bilgi kayitlarindaki (EINA/EINE) tum tedarikcileri "
        "toplam sahip olma maliyeti (TCO) uzerinden karsilastirir. Net fiyat + kademeli fiyat "
        "indirimi, Incoterms'e bagli lojistik/gumruk yuku, kalite hata maliyeti (PPM bazli), "
        "gec teslim risk maliyeti ve odeme vadesi finansman avantajini hesaba katar. "
        "Tedarikci performans skoru (zamaninda teslim, PPM, sertifikalar) ile birlikte siralar. "
        "Sabit varsayimlar: hatali parca yeniden isleme carpani 3x, yillik sermaye maliyeti %12, "
        "Incoterms ek yuku EXW %11 / FOB-FCA %8 / CIF-CIP %4 / DAP-DDP %0."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "material_id": {"type": "string", "description": "Karsilastirilacak malzeme numarasi."},
            "quantity": {"type": "number", "description": "Siparis miktari. Kademeli fiyat bunun uzerinden secilir.", "default": 1},
            "required_date": {"type": "string", "description": "Ihtiyac tarihi (YYYY-MM-DD). Tedarik suresi yetisen tedarikciler isaretlenir."},
            "delay_cost_per_day": {
                "type": "number",
                "description": "Gecikmenin gunluk maliyeti (proje cezasi/durus). Gec teslim risk maliyetinde kullanilir. Varsayilan 0.",
                "default": 0,
            },
        },
        "required": ["material_id"],
    },
)
def sap_compare_vendors(
    ctx: ToolContext,
    material_id: str,
    quantity: float = 1,
    required_date: str | None = None,
    delay_cost_per_day: float = 0,
) -> dict[str, Any]:
    records = ctx.sap.get_info_records(material_id)
    if not records:
        return {
            "material_id": material_id,
            "error": "Bu malzeme icin satinalma bilgi kaydi bulunamadi.",
            "suggestion": "Malzeme ic uretim (BESKZ=E) olabilir veya kaynak listesi bakimi yapilmamis olabilir.",
        }

    need_by = _parse_date(required_date)
    today = date.today()
    candidates: list[dict[str, Any]] = []
    vendors = ctx.sap.get_vendors([record.vendor_id for record in records])

    for record in records:
        vendor = vendors.get(record.vendor_id)
        unit_price = record.price_for_qty(quantity)
        base = unit_price * quantity

        landed_adder = INCOTERM_LANDED_ADDER.get(record.incoterms.upper(), 0.05)
        logistics_cost = round(base * landed_adder, 2)

        ppm = vendor.quality_ppm if vendor else 500
        quality_cost = round(base * (ppm / 1_000_000) * DEFECT_COST_MULTIPLIER, 2)

        otd = vendor.on_time_delivery_pct if vendor else 90.0
        # Gecikme olasiligi x beklenen gecikme suresi (gecmis performanstan tahmin)
        expected_delay_days = (1 - otd / 100) * max(7.0, record.planned_delivery_days * 0.15)
        delay_cost = round(expected_delay_days * delay_cost_per_day, 2)

        pay_days = _payment_days(record.payment_terms)
        financing_benefit = round(base * COST_OF_CAPITAL * pay_days / 365, 2)

        tco = round(base + logistics_cost + quality_cost + delay_cost - financing_benefit, 2)
        eta = today + timedelta(days=record.planned_delivery_days)

        warnings: list[str] = []
        if quantity < record.min_order_qty:
            warnings.append(
                f"Miktar {quantity:g} < minimum siparis miktari {record.min_order_qty:g}"
            )
        if need_by and eta > need_by:
            warnings.append(f"Termin yetismiyor: en erken {eta.isoformat()}, ihtiyac {need_by.isoformat()}")
        if vendor and vendor.blocked:
            warnings.append("Tedarikci satinalmaya kapali (LFA1-SPERM)")
        if vendor and vendor.single_source_risk:
            warnings.append("Tek kaynak riski - ikinci kaynak gelistirilmeli")

        candidates.append(
            {
                "vendor_id": record.vendor_id,
                "vendor_name": record.vendor_name or (vendor.name if vendor else ""),
                "country": vendor.country if vendor else "",
                "unit_price": round(unit_price, 2),
                "currency": record.currency,
                "base_cost": round(base, 2),
                "logistics_and_duty": logistics_cost,
                "quality_cost": quality_cost,
                "delay_risk_cost": delay_cost,
                "financing_benefit": -financing_benefit,
                "total_cost_of_ownership": tco,
                "tco_vs_price_delta_pct": round((tco / base - 1) * 100, 1) if base else 0.0,
                "lead_time_days": record.planned_delivery_days,
                "eta": eta.isoformat(),
                "incoterms": record.incoterms,
                "payment_terms": record.payment_terms,
                "min_order_qty": record.min_order_qty,
                "vendor_score": vendor.score() if vendor else None,
                "on_time_delivery_pct": otd,
                "quality_ppm": ppm,
                "certifications": vendor.certifications if vendor else [],
                "warnings": warnings,
                "feasible": not warnings or all("minimum siparis" in w for w in warnings),
            }
        )

    candidates.sort(key=lambda c: c["total_cost_of_ownership"])
    cheapest_price = min(candidates, key=lambda c: c["base_cost"])
    best_tco = candidates[0]
    best_score = max(candidates, key=lambda c: c["vendor_score"] or 0)

    insight = []
    if cheapest_price["vendor_id"] != best_tco["vendor_id"]:
        delta = cheapest_price["total_cost_of_ownership"] - best_tco["total_cost_of_ownership"]
        insight.append(
            f"En dusuk birim fiyat {cheapest_price['vendor_name']}'de, ancak TCO bazinda "
            f"{best_tco['vendor_name']} {delta:,.0f} {best_tco['currency']} daha avantajli "
            "(lojistik + kalite + gecikme riski dahil)."
        )
    if best_score["vendor_id"] != best_tco["vendor_id"]:
        insight.append(
            f"En yuksek performans skoru {best_score['vendor_name']} ({best_score['vendor_score']}). "
            "Kritik/A sinifi malzemede maliyet yerine skor onceliklendirilebilir."
        )

    return {
        "material_id": material_id,
        "quantity": quantity,
        "required_date": required_date,
        # Sabit varsayimlar tool aciklamasinda belgelidir; her cagrida tekrarlanmaz
        "assumptions": {"delay_cost_per_day": delay_cost_per_day},
        "candidates": candidates,
        "recommendation": {
            "best_tco_vendor": best_tco["vendor_id"],
            "best_tco_vendor_name": best_tco["vendor_name"],
            "savings_vs_worst": round(
                candidates[-1]["total_cost_of_ownership"] - best_tco["total_cost_of_ownership"], 2
            ),
        },
        "insights": insight,
    }




# ---------------------------------------------------------------------------
@tool(
    name="sap_track_purchase_orders",
    group="satinalma",
    domain="procurement_read",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    description=(
        "Acik satinalma siparislerini (EKKO/EKPO/EKET) izler ve gecikme analizi yapar. "
        "Talep edilen ile teyit edilen teslim tarihi arasindaki sapmayi, acik miktari ve "
        "risk seviyesini hesaplar. Malzeme, tedarikci veya WBS elemanina gore filtrelenebilir; "
        "proje bazinda toplam acik taahhut tutarini ozetler."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "material_id": {"type": "string"},
            "vendor_id": {"type": "string"},
            "wbs_element": {"type": "string", "description": "Proje WBS elemani veya on eki (or. R-2026-021)."},
            "only_open": {"type": "boolean", "default": True},
            "horizon_days": {
                "type": "integer",
                "description": "Kac gun icindeki teslimler 'yakin vadeli' sayilsin (varsayilan 30).",
                "default": 30,
            },
        },
        "required": [],
    },
)
def sap_track_purchase_orders(
    ctx: ToolContext,
    material_id: str | None = None,
    vendor_id: str | None = None,
    wbs_element: str | None = None,
    only_open: bool = True,
    horizon_days: int = 30,
) -> dict[str, Any]:
    orders = ctx.sap.get_purchase_orders(
        material_id=material_id,
        vendor_id=vendor_id,
        wbs_element=wbs_element,
        only_open=only_open,
    )
    today = date.today()
    horizon = today + timedelta(days=horizon_days)

    rows: list[dict[str, Any]] = []
    delayed_value = 0.0
    open_value = 0.0
    by_project: dict[str, dict[str, float]] = {}

    for po in orders:
        delay_days = 0
        if po.requested_delivery_date and po.confirmed_delivery_date:
            delay_days = (po.confirmed_delivery_date - po.requested_delivery_date).days
        overdue_days = 0
        if po.confirmed_delivery_date and po.confirmed_delivery_date < today and po.open_qty > 0:
            overdue_days = (today - po.confirmed_delivery_date).days

        if overdue_days > 0:
            risk = "kritik - teyitli tarih gecti"
        elif delay_days >= 21:
            risk = "yuksek - 3 haftadan fazla oteleme"
        elif delay_days > 0:
            risk = "orta - teyitli tarih talepten sonra"
        elif po.confirmed_delivery_date and po.confirmed_delivery_date <= horizon:
            risk = "izlemede - yakin vadeli teslim"
        else:
            risk = "dusuk"

        open_line_value = round(po.net_value * (po.open_qty / po.quantity), 2) if po.quantity else 0.0
        open_value += open_line_value
        if delay_days > 0 or overdue_days > 0:
            delayed_value += open_line_value

        if po.wbs_element:
            bucket = by_project.setdefault(po.wbs_element, {"open_value": 0.0, "delayed_value": 0.0, "po_count": 0})
            bucket["open_value"] += open_line_value
            bucket["po_count"] += 1
            if delay_days > 0 or overdue_days > 0:
                bucket["delayed_value"] += open_line_value

        rows.append(
            {
                "po_id": po.po_id,
                "vendor": f"{po.vendor_id} {po.vendor_name}".strip(),
                "material_id": po.material_id,
                "description": po.description,
                "quantity": po.quantity,
                "delivered": po.delivered_qty,
                "open_qty": po.open_qty,
                "status": po.status,
                "requested_delivery": po.requested_delivery_date.isoformat() if po.requested_delivery_date else None,
                "confirmed_delivery": po.confirmed_delivery_date.isoformat() if po.confirmed_delivery_date else None,
                "delay_days": delay_days,
                "overdue_days": overdue_days,
                "open_value": open_line_value,
                "currency": po.currency,
                "wbs_element": po.wbs_element,
                "risk": risk,
            }
        )

    rows.sort(key=lambda r: (-r["overdue_days"], -r["delay_days"]))
    # Kritik siparislerin tam kaydi zaten 'orders' icinde; burada yalnizca isaret veriyoruz
    critical = [r["po_id"] for r in rows if r["risk"].startswith(("kritik", "yuksek"))]

    return {
        "as_of": today.isoformat(),
        "order_count": len(rows),
        "total_open_value": round(open_value, 2),
        "delayed_open_value": round(delayed_value, 2),
        "delayed_share_pct": round(delayed_value / open_value * 100, 1) if open_value else 0.0,
        "currency": ctx.settings.sap.currency,
        "orders": rows,
        "critical_order_ids": critical,
        "by_project": {
            k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
            for k, v in by_project.items()
        },
    }


# ---------------------------------------------------------------------------
# Malzeme 360 ve tedarikci skoru
# ---------------------------------------------------------------------------
@tool(
    name="sap_material_360",
    group="satinalma",
    domain="master_data",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=1100,
    performance_budget=PerformanceBudget(p95_ms=6000, max_sap_calls=10, max_records=200),
    description=(
        "Bir malzemenin tek gorunumde 360 ozeti: ana veri, tesis/MRP parametreleri, degerleme "
        "fiyati, stok dagilimi, siniflandirma karakteristikleri, tedarik kaynaklari ve acik "
        "siparislerde nerede kullanildigi. Fiyat veya karakteristik okunamadiysa bunu acikca "
        "isaretler; sessizce sifir/bos gostermez. Bir malzeme hakkinda karar verilecekse "
        "ilk basvurulacak tooldur."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "material_id": {"type": "string"},
            "plant": {"type": "string"},
            "detail": DETAIL_SCHEMA,
        },
        "required": ["material_id"],
    },
)
def sap_material_360(
    ctx: ToolContext,
    material_id: str,
    plant: str | None = None,
    detail: str = "standard",
) -> ToolResult | dict[str, Any]:
    level = resolve_detail(detail)
    material = ctx.sap.get_material(material_id, plant=plant)
    if material is None:
        return {
            "error": f"Malzeme {material_id} bulunamadi.",
            "hint": "sap_search_materials ile aciklamada arama yapmayi deneyin.",
        }

    data_gaps: list[str] = []
    estimated_fields: list[str] = []

    # --- Degerleme
    valuation: dict[str, Any] | None = None
    try:
        valuation = ctx.sap.get_valuation(material.material_id, plant=plant)
    except SAPNotSupported:
        data_gaps.append("degerleme servisi yok (fiyat okunamadi)")
    if valuation:
        price = float(valuation.get("moving_avg_price") or 0.0)
    else:
        price = material.moving_avg_price
        if not price:
            data_gaps.append("hareketli ortalama fiyat bulunamadi")

    # --- Siniflandirma
    classification = None
    try:
        classification = ctx.sap.get_material_classification(material.material_id)
    except SAPNotSupported:
        data_gaps.append("siniflandirma servisi yok (teknik karakteristikler okunamadi)")

    # --- Stok
    levels = ctx.sap.get_stock([material.material_id], plant=plant)
    level_row = levels[0] if levels else None

    # --- Tedarik kaynaklari
    records = ctx.sap.get_info_records(material.material_id, plant=plant)
    sources = [
        {
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "net_price": r.net_price,
            "currency": r.currency,
            "lead_time_days": r.planned_delivery_days,
            "min_order_qty": r.min_order_qty,
            "incoterms": r.incoterms,
        }
        for r in records
    ]

    # --- Nerede kullaniliyor (acik siparisler)
    open_orders = ctx.sap.get_purchase_orders(material_id=material.material_id, only_open=True)
    used_in = sorted({po.wbs_element for po in open_orders if po.wbs_element})

    data: dict[str, Any] = {
        "material_id": material.material_id,
        "description": material.description,
        "material_group": material.material_group,
        "type": material.material_type,
        "unit": material.base_unit,
        "plant": material.plant,
        "price": round(price, 2),
        "currency": material.currency,
        "price_source": (valuation or {}).get("source_api") or ("bulunamadi" if not price else "master"),
        "procurement_type": material.procurement_type,
        "lead_time_days": material.planned_delivery_days,
        "min_order_qty": material.min_order_qty,
        "mrp_controller": material.mrp_controller,
        "abc": material.abc_indicator,
        "weight_kg": material.gross_weight_kg,
        "source_count": len(sources),
        "single_source": len(sources) <= 1,
        "open_order_count": len(open_orders),
        "used_in_projects": used_in,
    }

    if level_row is not None:
        data["stock"] = {
            "unrestricted": level_row.unrestricted_qty,
            "reserved": level_row.reserved_qty,
            "unreserved": level_row.unreserved_qty,
            "quality_inspection": level_row.quality_inspection_qty,
            "blocked": level_row.blocked_qty,
            "on_order": level_row.on_order_qty,
            "safety_stock": level_row.safety_stock,
            "below_safety_stock": level_row.below_safety_stock,
            "note": "Stok fotografi; taahhut icin sap_atp_check.",
        }

    if classification is not None:
        characteristics = dict(classification.characteristics)
        if level == "summary":
            characteristics = dict(list(characteristics.items())[:6])
        data["classification"] = {
            "class_name": classification.class_name,
            "characteristics": characteristics,
            "units": classification.units if level == "full" else None,
            "source": classification.source,
        }
        if not characteristics:
            data_gaps.append("malzemeye sinif/karakteristik atanmamis")

    if level != "summary":
        data["sources"] = sources
    if level == "full":
        data["open_orders"] = [
            {
                "po_id": po.po_id,
                "vendor_id": po.vendor_id,
                "open_qty": po.open_qty,
                "confirmed_delivery": (
                    po.confirmed_delivery_date.isoformat() if po.confirmed_delivery_date else None
                ),
                "wbs_element": po.wbs_element,
            }
            for po in open_orders
        ]

    if data_gaps:
        data["data_gaps"] = data_gaps
        estimated_fields.append("price" if "fiyat" in " ".join(data_gaps) else "classification")

    result = ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            "product+stock+inforecord",
            business_object=material.material_id,
            record_count=1,
            estimated_fields=tuple(estimated_fields),
        ),
    )
    for gap in data_gaps:
        result.warn(f"Veri bosluğu: {gap}")
    return result


# ---------------------------------------------------------------------------
@tool(
    name="sap_supplier_score_360",
    group="satinalma",
    domain="procurement_read",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=900,
    performance_budget=PerformanceBudget(p95_ms=30000, max_sap_calls=6, max_records=200),
    description=(
        "Tedarikcinin fiyat, teslim, miktar, kalite ve servis skorlarini SAP operasyonel "
        "degerlendirmesinden (A_SUPPLIEROPLSCORESAV_CDS) toplar ve acik siparislerdeki gercek "
        "termin sapmasi ile karsilastirir. SAP'ta gerceklesen veri yoksa ilgili alanlar "
        "estimated=true olarak isaretlenir; tahmin gercek veri gibi sunulmaz."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "vendor_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Degerlendirilecek tedarikci numaralari.",
            },
            "purchasing_org": {"type": "string"},
        },
        "required": ["vendor_ids"],
    },
)
def sap_supplier_score_360(
    ctx: ToolContext,
    vendor_ids: list[str],
    purchasing_org: str | None = None,
) -> ToolResult | dict[str, Any]:
    if not vendor_ids:
        return {"error": "En az bir tedarikci numarasi gerekli."}

    rows: list[dict[str, Any]] = []
    all_estimated: set[str] = set()

    for vendor_id in vendor_ids:
        vendor = ctx.sap.get_vendor_master(vendor_id)
        if vendor is None:
            rows.append({"vendor_id": vendor_id, "error": "Tedarikci bulunamadi."})
            continue

        try:
            score = ctx.sap.get_supplier_score(vendor_id, purchasing_org=purchasing_org)
        except SAPNotSupported as exc:
            score = None
            all_estimated.add("overall_score")
            unsupported_reason = str(exc)
        else:
            unsupported_reason = ""

        row: dict[str, Any] = {
            "vendor_id": vendor.vendor_id,
            "name": vendor.name,
            "country": vendor.country,
            "blocked": vendor.blocked,
            "certifications": vendor.certifications,
            "single_source_risk": vendor.single_source_risk,
        }
        if score is not None:
            row["scores"] = score.to_summary()
            row["has_real_evaluation_data"] = score.has_real_data
            all_estimated.update(score.estimated_fields)
        else:
            row["scores"] = None
            row["has_real_evaluation_data"] = False
            row["note"] = unsupported_reason or "Degerlendirme verisi yok."

        # Gerceklesen termin sapmasi: acik siparislerden hesaplanir.
        orders = ctx.sap.get_purchase_orders(vendor_id=vendor_id, limit=50)
        deltas = [
            (po.confirmed_delivery_date - po.requested_delivery_date).days
            for po in orders
            if po.confirmed_delivery_date and po.requested_delivery_date
        ]
        row["open_order_count"] = sum(1 for po in orders if po.open_qty > 0)
        if deltas:
            row["measured_delivery_variance"] = {
                "orders_evaluated": len(deltas),
                "avg_delay_days": round(sum(deltas) / len(deltas), 1),
                "worst_delay_days": max(deltas),
                "source": "PO talep-teyit tarih farki (gerceklesen)",
            }
        else:
            row["measured_delivery_variance"] = None
        rows.append(row)

    rows.sort(
        key=lambda r: -(((r.get("scores") or {}).get("overall_score")) or 0),
    )
    data: dict[str, Any] = {
        "purchasing_org": purchasing_org or ctx.settings.sap.purch_org,
        "vendors": rows,
        "ranked_by": "overall_score (varsa)",
    }
    if all_estimated:
        data["estimated_fields"] = sorted(all_estimated)
        data["caution"] = (
            "Isaretli alanlar SAP gerceklesen verisinden gelmiyor. Kaynak secim karari "
            "bu alanlara dayandirilmadan once degerlendirme verisi baglanmali."
        )

    result = ToolResult(
        data=data,
        evidence=ctx.sap_evidence(
            "supplier+supplier_score+purchase_order",
            record_count=len(rows),
            estimated_fields=tuple(sorted(all_estimated)),
        ),
        returned_count=len(rows),
    )
    if all_estimated:
        result.warn("Bazi skor alanlari gercek SAP degerlendirme verisi degil (estimated).")
    return result


# ---------------------------------------------------------------------------
# Satinalma talebi: prepare -> onay -> submit -> verify
# ---------------------------------------------------------------------------
def build_pr_items(ctx: ToolContext, items: list[dict[str, Any]]) -> list[PurchaseRequisitionItem]:
    """Model argumanlarini tipli PR kalemlerine cevirir."""
    out: list[PurchaseRequisitionItem] = []
    for raw in items:
        out.append(
            PurchaseRequisitionItem(
                material_id=str(raw["material_id"]),
                quantity=float(raw["quantity"]),
                unit=raw.get("unit", "ST"),
                delivery_date=_parse_date(raw.get("delivery_date")),
                plant=raw.get("plant") or ctx.settings.sap.plant,
                preferred_vendor=raw.get("preferred_vendor"),
                net_price=raw.get("net_price"),
                currency=ctx.settings.sap.currency,
                cost_center=raw.get("cost_center"),
                wbs_element=raw.get("wbs_element"),
                item_text=raw.get("item_text", ""),
            )
        )
    return out


_PR_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "material_id": {"type": "string"},
        "quantity": {"type": "number"},
        "delivery_date": {"type": "string", "description": "YYYY-MM-DD"},
        "preferred_vendor": {"type": "string", "description": "Sabit tedarikci (EBAN-FLIEF)."},
        "wbs_element": {"type": "string", "description": "Proje WBS elemani (or. R-2026-021-1)."},
        "cost_center": {"type": "string"},
        "item_text": {"type": "string"},
        "plant": {"type": "string"},
        "net_price": {"type": "number"},
    },
    "required": ["material_id", "quantity"],
}


@tool(
    name="sap_pr_prepare",
    group="satinalma",
    domain="procurement_write",
    risk_tier=RiskTier.R2,
    required_scopes=(SCOPE_SAP_PREPARE,),
    # Kalemlerde tesis verilmezse sistem varsayilani kullanilir; policy bu
    # varsayilani da actor kapsamina karsi denetlemeli.
    org_scoped=True,
    result_token_budget=1200,
    description=(
        "Satinalma talebi TASLAGI hazirlar. SAP'a HICBIR SEY YAZMAZ. Fiyatlandirir, minimum "
        "siparis miktari / tedarik suresi / termin / hesap atamasi dogrulamasi yapar, kalem "
        "bazli diff uretir ve onay gerekip gerekmedigini soyler. Onay gerekiyorsa onay task'i "
        "acar ve payload hash'ini dondurur. Yazmak icin ayni argumanlarla sap_pr_submit "
        "cagrilir; argumanlar degisirse onay gecersiz olur."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "items": {"type": "array", "description": "Talep kalemleri.", "items": _PR_ITEM_SCHEMA},
            "header_text": {"type": "string", "description": "Talep basligi / gerekce."},
            "purchase_group": {"type": "string", "description": "Satinalma grubu."},
        },
        "required": ["items"],
    },
)
def sap_pr_prepare(
    ctx: ToolContext,
    items: list[dict[str, Any]],
    header_text: str = "",
    purchase_group: str | None = None,
) -> dict[str, Any]:
    if not items:
        return {"error": "En az bir kalem gerekli."}

    pr_items = build_pr_items(ctx, items)
    draft = ctx.sap.prepare_purchase_requisition(
        pr_items, header_text=header_text, purchase_group=purchase_group
    )

    # Onay hash'i, submit sirasinda policy'nin hesaplayacagi payload ile ayni
    # kanonik gorunumden uretilir; boylece onay hazirlanan isten yurutulen ise tasinir.
    approval_payload = approval_payload_for(
        {"items": items, "header_text": header_text, "purchase_group": purchase_group}
    )
    digest = payload_hash(approval_payload)

    # --- Tenant profili: SAP'in reddedecegi seyi ONCEDEN yakala -------------
    # `$metadata` bir alanin var oldugunu soyler, sirketin onu ZORUNLU
    # yaptigini soylemez. Field selection ve BAdI kurallari ancak bildirilerek
    # ya da SAP reddedildikten sonra ogrenilerek bilinir. Profil bu bilgiyi
    # tasir; burada uygulanmazsa kullanici ayni duvara her seferinde carpar.
    profile = ctx.tenant_profile()
    profile_findings = [
        ValidationFinding(
            severity="error",
            field=name,
            message=(
                f"'{name}' bu sistemde zorunlu ama bos. Tenant profilinde zorunlu "
                f"alan olarak tanimli; SAP yazmayi reddederdi."
            ),
        )
        for name in profile.missing_required(draft.payload)
    ]
    all_findings = [*draft.findings, *profile_findings]
    blocking = [f.message for f in all_findings if f.blocking]

    payload: dict[str, Any] = {
        "draft_id": draft.draft_id,
        "total_value": draft.total_value,
        "currency": draft.currency,
        "item_count": len(draft.items),
        "items": draft.items,
        "diff": draft.diff,
        "findings": [f.model_dump() for f in all_findings],
        "blocking": blocking,
        "document_type": profile.document_type,
        "submittable": draft.is_submittable and not profile_findings,
        "payload_sha256": digest,
        "source_api": draft.source_api,
        "written_to_sap": False,
    }

    threshold = ctx.settings.sap.approval_threshold
    needs_approval = draft.total_value > threshold
    payload["requires_human_approval"] = needs_approval
    payload["approval_threshold"] = threshold

    if needs_approval and ctx.approval_gateway is not None and ctx.actor is not None:
        request = ApprovalRequest(
            tool="sap_pr_submit",
            payload=approval_payload,
            tenant=ctx.actor.tenant,
            requested_by=ctx.actor.subject,
            subject_line=(
                f"PR onayi: {len(draft.items)} kalem, "
                f"{draft.total_value:,.2f} {draft.currency}"
            ),
            diff=draft.diff,
            total_value=draft.total_value,
            currency=draft.currency,
            max_value=round(draft.total_value * 1.02, 2),
        )
        payload["approval_task"] = ctx.approval_gateway.request(request)
        payload["approval_instruction"] = (
            "Yetkili onaylayici bu task'i tamamlayana kadar sap_pr_submit reddedilir. "
            "Onay tamamlandiginda approval_id ile submit cagrilir."
        )
    elif needs_approval:
        payload["approval_instruction"] = (
            "Onay gateway'i yapilandirilmamis; bu tutar icin yazma yapilamaz."
        )
    else:
        payload["approval_instruction"] = (
            f"Tutar onay esiginin ({threshold:,.2f} {draft.currency}) altinda. "
            "sap_pr_submit dogrudan cagrilabilir; idempotency_key vermeyi unutmayin."
        )

    payload["next_step"] = (
        "sap_pr_submit(items=..., header_text=..., purchase_group=..., "
        "idempotency_key='proje:senaryo:pr:v1'"
        + (", approval_id=...)" if needs_approval else ")")
    )
    return payload


@tool(
    name="sap_pr_submit",
    group="satinalma",
    domain="procurement_write",
    risk_tier=RiskTier.R3,
    required_scopes=(SCOPE_PR_WRITE,),
    approval_policy="threshold",
    approve_scope=SCOPE_PR_APPROVE,
    idempotent=True,
    data_classification="confidential",
    # Yazma cagrisinda uzun SAP round-trip beklenebilir; yine de bir tur
    # sonsuza kadar kilitlenmemeli.
    timeout_s=120.0,
    result_token_budget=1000,
    description=(
        "Onaylanmis satinalma talebini SAP'ta OLUSTURUR. Zorunlu protokol: taslak yeniden "
        "hesaplanir, tutar onay kapsamina karsi dogrulanir, idempotency_key ile tek kez "
        "yazilir, sonra SAP'tan geri okunup postcondition dogrulanir. Ayni idempotency_key "
        "ikinci kez cagrildiginda yeni belge OLUSMAZ. Timeout durumunda tekrar cagirmayin; "
        "sap_reconcile_execution kullanin. Onay esigi ustundeki tutarlar gecerli approval_id ister."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "items": {"type": "array", "description": "sap_pr_prepare ile ayni kalemler.", "items": _PR_ITEM_SCHEMA},
            "header_text": {"type": "string"},
            "purchase_group": {"type": "string"},
            "idempotency_key": {
                "type": "string",
                "description": "Deterministik anahtar, or. 'R-2026-021:redüktor-eksigi:pr:v1'.",
            },
            "approval_id": {
                "type": "string",
                "description": "Onay kaydi kimligi (esik ustu tutarlar icin zorunlu).",
            },
        },
        "required": ["items", "idempotency_key"],
    },
)
def sap_pr_submit(
    ctx: ToolContext,
    items: list[dict[str, Any]],
    idempotency_key: str,
    header_text: str = "",
    purchase_group: str | None = None,
    approval_id: str = "",
) -> dict[str, Any]:
    if not items:
        return {"error": "En az bir kalem gerekli."}
    decision = ctx.decision
    if decision is None:
        # Policy gate atlanmis olamaz; savunma amacli fail-closed.
        return {"error": "Policy karari yok; yazma reddedildi.", "denial_code": "NO_POLICY_DECISION"}

    # 1-3. Resolve + Read + Validate: taslak yeniden hesaplanir (model bildirimine guvenilmez).
    pr_items = build_pr_items(ctx, items)
    draft = ctx.sap.prepare_purchase_requisition(
        pr_items, header_text=header_text, purchase_group=purchase_group
    )
    if not draft.is_submittable:
        return {
            "error": "Taslak dogrulamayi gecemedi; SAP'a yazilmadi.",
            "blocking_findings": [f.message for f in draft.blocking_findings],
            "written_to_sap": False,
        }

    # 4-5. Diff + Approve: dogrulanmis tutar onay kapsamina karsi kontrol edilir.
    assert ctx.policy
    violation = ctx.policy.require_approval_for_value(
        decision, value=draft.total_value, currency=draft.currency
    )
    if violation:
        return {
            "error": violation,
            "denial_code": "APPROVAL_SCOPE_EXCEEDED",
            "total_value": draft.total_value,
            "currency": draft.currency,
            "diff": draft.diff,
            "written_to_sap": False,
            "remediation": "Guncel tutar icin yeni onay alip approval_id ile tekrar cagirin.",
        }

    # Ortam kilidi: SAP_DRY_RUN=true iken gercek yazma her durumda engellenir.
    if ctx.settings.sap.dry_run:
        return {
            "write_status": "simulated",
            "written_to_sap": False,
            "total_value": draft.total_value,
            "currency": draft.currency,
            "item_count": len(draft.items),
            "diff": draft.diff,
            "findings": [f.message for f in draft.findings],
            "idempotency_key": idempotency_key,
            "approval_id": approval_id or None,
            "messages": [
                "Ortam ayari SAP_DRY_RUN=true: policy ve onay kontrolleri gecti, "
                "SAP'a yazma simule edildi. Gercek yazma icin SAP_DRY_RUN=false gerekir."
            ],
        }

    # 6-8. Execute + Verify + Audit: idempotency ve read-after-write ile.
    guard = ctx.write_guard()

    def execute() -> tuple[str, dict[str, Any]]:
        result = ctx.sap.submit_purchase_requisition(
            draft,
            external_reference=idempotency_key,
            correlation_id=ctx.execution.correlation_id if ctx.execution else "",
        )
        return (result.requisition_id or ""), result.model_dump()

    def verify(object_id: str) -> Verification:
        record = ctx.sap.read_purchase_requisition(object_id)
        return Verification.compare(
            {"item_count": len(draft.items), "total_value": draft.total_value}, record, tolerance=0.5
        )

    def reconcile() -> tuple[str, dict[str, Any]] | None:
        finder = getattr(ctx.sap, "find_purchase_requisition_by_reference", None)
        if finder is None:
            return None
        try:
            return finder(idempotency_key)
        except SAPNotSupported:
            return None

    outcome = guard.run(
        tool="sap_pr_submit",
        decision=decision,
        payload=draft.payload,
        idempotency_key=idempotency_key,
        execute=execute,
        verify=verify,
        reconcile=reconcile,
    )

    payload = outcome.to_dict()
    payload["written_to_sap"] = outcome.status in {"created", "reconciled"}
    payload["total_value"] = draft.total_value
    payload["currency"] = draft.currency
    payload["approval_id"] = approval_id or None
    if draft.findings:
        payload["findings"] = [f.message for f in draft.findings]
    return payload
