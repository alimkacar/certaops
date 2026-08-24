"""SAP proje finans ve SAP kaynakli raporlama tool'lari."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from ..contracts import SCOPE_REPORT_WRITE, SCOPE_SAP_READ, RiskTier
from .registry import ToolContext, tool


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return cleaned or "sap_raporu"


@tool(
    name="sap_project_cost_status",
    group="proje_finans",
    domain="project_finance",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=1400,
    description=(
        "SAP PS/CO kaynagindan WBS bazinda plan, fiili ve acik taahhut tutarlarini getirir; "
        "tamamlanma yuzdesiyle EAC/ETC ve CPI hesaplar, portfoy ozeti ve asim uyarilari uretir. "
        "SAP'ta ilerleme yuzdesi guncel degilse bunu metodoloji riski olarak aciklar."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "wbs_element": {"type": "string", "description": "WBS veya on eki."},
            "fiscal_year": {"type": "integer"},
            "overrun_alert_pct": {
                "type": "number",
                "description": "Tahmini asim uyari esigi (%).",
                "default": 5.0,
            },
        },
        "required": [],
    },
)
def sap_project_cost_status(
    ctx: ToolContext,
    wbs_element: str | None = None,
    fiscal_year: int | None = None,
    overrun_alert_pct: float = 5.0,
) -> dict[str, Any]:
    costs = ctx.sap.get_project_costs(wbs_element=wbs_element, fiscal_year=fiscal_year)
    if not costs:
        return {"error": "Verilen filtreye uyan WBS elemani bulunamadi.", "filter": wbs_element}

    rows: list[dict[str, Any]] = []
    alerts: list[str] = []
    total_plan = total_actual = total_commitment = total_eac = 0.0
    for cost in costs:
        progress = max(1.0, cost.completion_pct) / 100
        eac = max(
            cost.actual_cost / progress + cost.commitment * (1 - progress),
            cost.actual_cost + cost.commitment,
        )
        etc = max(0.0, eac - cost.actual_cost)
        cpi = (cost.plan_cost * progress) / cost.actual_cost if cost.actual_cost else None
        overrun = (eac / cost.plan_cost - 1) * 100 if cost.plan_cost else 0.0
        total_plan += cost.plan_cost
        total_actual += cost.actual_cost
        total_commitment += cost.commitment
        total_eac += eac
        if overrun > overrun_alert_pct:
            alerts.append(
                f"{cost.wbs_element}: EAC planin %{overrun:.1f} uzerinde "
                f"({eac:,.0f} / {cost.plan_cost:,.0f} {cost.currency})."
            )
        rows.append(
            {
                "wbs_element": cost.wbs_element,
                "description": cost.description,
                "completion_pct": cost.completion_pct,
                "plan": cost.plan_cost,
                "actual": cost.actual_cost,
                "commitment": cost.commitment,
                "remaining_budget": cost.remaining_budget,
                "eac": round(eac, 2),
                "etc": round(etc, 2),
                "cpi": round(cpi, 3) if cpi else None,
                "forecast_variance_pct": round(overrun, 1),
                "status": "asim riski" if overrun > overrun_alert_pct else "plan dahilinde",
            }
        )
    rows.sort(key=lambda row: -row["forecast_variance_pct"])
    return {
        "as_of": date.today().isoformat(),
        "source_system": ctx.settings.sap.system_alias,
        "source_api": "project_cost",
        "currency": ctx.settings.sap.currency,
        "wbs_count": len(rows),
        "portfolio_summary": {
            "total_plan": round(total_plan, 2),
            "total_actual": round(total_actual, 2),
            "total_commitment": round(total_commitment, 2),
            "total_eac": round(total_eac, 2),
            "forecast_variance": round(total_eac - total_plan, 2),
            "forecast_variance_pct": (
                round((total_eac / total_plan - 1) * 100, 1) if total_plan else 0.0
            ),
        },
        "wbs_elements": rows,
        "alerts": alerts,
        "method_note": (
            "EAC = fiili/tamamlanma orani + kalan taahhut. SAP PS ilerleme yuzdesi "
            "guncel degilse tahmin yaniltici olabilir."
        ),
    }


@tool(
    name="sap_generate_report",
    group="raporlama",
    domain="reporting",
    risk_tier=RiskTier.R2,
    required_scopes=(SCOPE_REPORT_WRITE,),
    org_scoped=False,
    result_token_budget=700,
    description=(
        "SAP tool sonuclarini kaynak referanslariyla Excel veya Markdown raporuna donusturur. "
        "Ana veri, ATP/MRP, tedarikci, satinalma, audit veya WBS maliyet tablolarini yonetim "
        "ozetiyle paketler; yeni fiyat, BOM ya da muhendislik tahmini uretmez."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "format": {"type": "string", "enum": ["xlsx", "markdown", "both"], "default": "xlsx"},
            "executive_summary": {"type": "string"},
            "source_references": {
                "type": "array",
                "items": {"type": "string"},
                "description": "SAP API, business object veya evidence ID referanslari.",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string"},
                        "body": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["heading"],
                },
            },
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "rows": {"type": "array", "items": {"type": "array", "items": {}}},
                    },
                    "required": ["name", "columns", "rows"],
                },
            },
            "filename": {"type": "string"},
        },
        "required": ["title"],
    },
)
def sap_generate_report(
    ctx: ToolContext,
    title: str,
    format: str = "xlsx",
    executive_summary: str = "",
    source_references: list[str] | None = None,
    sections: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    sections = sections or []
    tables = tables or []
    sources = source_references or []

    # Rapor dosyasi sistemden CIKAR: bu bir `export` hedefidir, `model` degil.
    # Ekranda gorulebilen bir deger, indirilebilir bir dosyada ayni kurala tabi
    # olmayabilir - toplu disa aktarma ek kapsam ister. Bu kapi eskiden hic
    # calismiyordu; `sink="export"` politikasi tanimliydi ama cagrilmiyordu.
    if ctx.dlp is not None and ctx.actor is not None:
        decision = ctx.dlp.apply(
            {
                "executive_summary": executive_summary,
                "sections": sections,
                "tables": tables,
            },
            actor=ctx.actor,
            sink="export",
            detail=str(ctx.purpose and "full" or "standard"),
            purpose=ctx.purpose,
        )
        if decision.denied:
            return {
                "error": decision.denied_reason
                or "Rapor icerigi disa aktarma politikasi tarafindan reddedildi.",
                "denial_code": "EXPORT_POLICY_DENIED",
                "remediation": (
                    "Bu rapor disa aktarim yetkisi gerektiren veri iceriyor. Daha dar "
                    "bir icerikle deneyin veya gerekli disa aktarim kapsamina sahip bir "
                    "kullaniciyla calisin."
                ),
            }
        cleaned = decision.payload
        if isinstance(cleaned, dict):
            executive_summary = cleaned.get("executive_summary", executive_summary)
            sections = cleaned.get("sections", sections)
            tables = cleaned.get("tables", tables)

    out_dir: Path = ctx.settings.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(filename or f"sap_{title}_{date.today().isoformat()}")
    created: list[str] = []

    if format in {"xlsx", "both"}:
        try:
            import xlsxwriter
        except ImportError:
            return {"error": "Excel cikti icin XlsxWriter kurulmalidir."}
        path = out_dir / f"{stem}.xlsx"
        # SAP/metin verisi "=", "+", "-" veya "@" ile baslayabilir. Excel'e
        # aktarilan guvenilmeyen metni formul ya da URL olarak yorumlatmayiz.
        workbook = xlsxwriter.Workbook(
            str(path), {"strings_to_formulas": False, "strings_to_urls": False}
        )
        heading = workbook.add_format({"bold": True, "bg_color": "#0A6ED1", "font_color": "white"})
        cell = workbook.add_format({"border": 1, "text_wrap": True, "valign": "top"})
        number = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
        summary = workbook.add_worksheet("Ozet")
        summary.set_column(0, 0, 24)
        summary.set_column(1, 1, 90)
        summary.write(0, 0, "Baslik", heading)
        summary.write(0, 1, title)
        summary.write(1, 0, "SAP sistemi", heading)
        summary.write(1, 1, ctx.settings.sap.system_alias)
        summary.write(2, 0, "Kaynaklar", heading)
        summary.write(2, 1, "\n".join(sources) or "Tool audit/evidence kayitlari")
        row_index = 4
        if executive_summary:
            summary.write(row_index, 0, "Yonetim ozeti", heading)
            summary.write(row_index, 1, executive_summary, cell)
            row_index += 2
        for section in sections:
            summary.write(row_index, 0, section.get("heading", ""), heading)
            body = section.get("body", "")
            bullets = "\n".join(f"- {x}" for x in section.get("bullets", []) or [])
            summary.write(row_index, 1, "\n".join(x for x in (body, bullets) if x), cell)
            row_index += 1

        used = {"Ozet"}
        for table in tables:
            base = _safe_filename(str(table.get("name", "SAP")))[:26] or "SAP"
            sheet_name = base
            counter = 2
            while sheet_name in used:
                sheet_name = f"{base[:24]}_{counter}"
                counter += 1
            used.add(sheet_name)
            sheet = workbook.add_worksheet(sheet_name)
            columns = [str(value) for value in table.get("columns", [])]
            for column_index, column in enumerate(columns):
                sheet.write(0, column_index, column, heading)
                sheet.set_column(column_index, column_index, 20)
            for data_index, data_row in enumerate(table.get("rows", []), start=1):
                for column_index, value in enumerate(data_row):
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        sheet.write_number(data_index, column_index, value, number)
                    else:
                        sheet.write(data_index, column_index, "" if value is None else str(value), cell)
            sheet.freeze_panes(1, 0)
        workbook.close()
        created.append(str(path))

    if format in {"markdown", "both"}:
        path = out_dir / f"{stem}.md"
        parts = [
            f"# {title}",
            "",
            f"- SAP sistemi: {ctx.settings.sap.system_alias}",
            f"- Olusturulma: {date.today().isoformat()}",
            f"- Kaynaklar: {', '.join(sources) or 'tool audit/evidence kayitlari'}",
            "",
        ]
        if executive_summary:
            parts.extend(["## Yonetim ozeti", "", executive_summary, ""])
        for section in sections:
            parts.extend([f"## {section.get('heading', '')}", "", section.get("body", ""), ""])
            parts.extend(f"- {item}" for item in section.get("bullets", []) or [])
            parts.append("")
        for table in tables:
            columns = [str(value) for value in table.get("columns", [])]
            parts.extend([f"## {table.get('name', 'SAP')}", ""])
            if columns:
                parts.extend([
                    "| " + " | ".join(columns) + " |",
                    "| " + " | ".join("---" for _ in columns) + " |",
                ])
                for data_row in table.get("rows", []):
                    values = [str(value).replace("|", "\\|") for value in data_row]
                    values += [""] * (len(columns) - len(values))
                    parts.append("| " + " | ".join(values[: len(columns)]) + " |")
            parts.append("")
        path.write_text("\n".join(parts), encoding="utf-8")
        created.append(str(path))

    for artifact in created:
        if artifact not in ctx.artifacts:
            ctx.artifacts.append(artifact)
    return {
        "created_files": created,
        "source_system": ctx.settings.sap.system_alias,
        "source_references": sources,
        "written_to_sap": False,
        "note": "Rapor yerel cikti dizinine yazildi; SAP business object degistirilmedi.",
    }
