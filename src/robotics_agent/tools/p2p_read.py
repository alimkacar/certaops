"""Salt-okunur procure-to-pay gorunurluk tool'lari.

Dort salt-okunur tool, bir soruyu cevaplar: **"Bu is nerede takildi?"**

    sap_document_flow             PR -> PO -> mal kabul -> fatura -> odeme zinciri
    sap_purchase_order_360        PO kalem, teslimat plani, GR ve fatura durumu
    sap_supplier_invoice_status   Faturanin muhasebe ve odeme durumu
    sap_invoice_block_explain     Tolerans blokajinin sayisal aciklamasi

Ortak kurallar:

  1. **Uydurma bag yok.** Her belge bagi `linked_by` alaninda hangi SAP
     referans alanindan kuruldugunu tasir. Bag kurulamiyorsa dugum donmez.
  2. **Hesap kodda.** Tolerans asimi, GR/IR farki ve gecikme gunu deterministik
     olarak burada hesaplanir; model toplam veya yuzde hesaplamaz.
  3. **Varsayilan dar.** Cevap `summary` seviyesinde ozet + karar; tam kalem
     listesi `detail` yukseltilerek veya evidence handle'i ile alinir.
  4. **Kisisel veri maskeli.** Onaylayan/islemci adlari D2'dir ve merkezi DLP
     tarafindan varsayilan olarak maskelenir.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..adapters.sap import SAPError, SAPNotSupported
from ..adapters.sap.concurrency import gather_named
from ..cache import CachePolicy
from ..contracts import (
    DETAIL_SCHEMA,
    SCOPE_SAP_READ,
    RiskTier,
    ToolResult,
    page_limit,
    resolve_detail,
)
from ..privacy import DataClass, DataPolicy
from ..risk import READ_ONLY, ImpactProfile, MutationKind, Reversibility
from .registry import PerformanceBudget, ToolContext, tool

# Belge zinciri sirasi: cikti her zaman is akisi sirasinda gosterilir.
_FLOW_ORDER = {
    "purchase_requisition": 0,
    "purchase_order": 1,
    "goods_receipt": 2,
    "supplier_invoice": 3,
    "payment": 4,
}

_DOC_LABELS = {
    "purchase_requisition": "Satinalma talebi",
    "purchase_order": "Satinalma siparisi",
    "goods_receipt": "Mal kabul",
    "supplier_invoice": "Tedarikci faturasi",
    "payment": "Odeme",
}

# P2P tool'larinin ortak veri sozlesmesi. Tutar ve tedarikci kimligi D2'dir;
# tedarikci iletisim bilgisi ve banka verisi bu tool'larin cikisinda hic
# bulunmaz ama siniflandirmasi yine de bildirilir (fail-closed).
_P2P_DATA_POLICY = DataPolicy(
    default_class=DataClass.D1,
    fields={
        "net_price": DataClass.D2,
        "net_value": DataClass.D2,
        "gross_amount": DataClass.D2,
        "amount": DataClass.D2,
        "requested_by": DataClass.D2,
        "vendor_name": DataClass.D2,
        "supplier_iban": DataClass.D3,
        "tax_number": DataClass.D3,
    },
    export_scope="sap.export.confidential",
    purpose="procurement_operations",
    data_owner="satinalma_ve_muhasebe",
)

# Salt okunur P2P sorgularinda kisa TTL: SAP durumu dakikalar icinde degisir,
# ama ayni sohbet turunda tekrar eden cagrilar SAP'yi ikinci kez yormaz.
_P2P_CACHE = CachePolicy(
    ttl_seconds=60,
    vary_by=("tenant", "subject", "company_code", "plant", "purchasing_org"),
    max_class=DataClass.D2,
    subject_bound=True,
    invalidated_by=("po_id", "document_id", "invoice_id"),
)


def _today() -> date:
    return date.today()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# ---------------------------------------------------------------------------
@tool(
    name="sap_document_flow",
    group="p2p",
    domain="p2p_flow",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=1200,
    data_policy=_P2P_DATA_POLICY,
    impact_profile=READ_ONLY,
    cache_policy=_P2P_CACHE,
    performance_budget=PerformanceBudget(p95_ms=4000, max_sap_calls=6, max_records=200),
    description=(
        "Bir belge numarasindan (PR, PO, malzeme belgesi veya fatura) tum P2P zincirini "
        "cikarir: talep -> siparis -> mal kabul -> fatura -> odeme. Her bag SAP referans "
        "alaniyla (EKPO-BANFN, MSEG-EBELN, RSEG-EBELN) kanitlanir; kurulamayan bag "
        "dondurulmez. 'Bu siparisin faturasi kesildi mi', 'bu fatura hangi talepten geldi' "
        "sorularinin tek cagrilik cevabi."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "PR, PO, malzeme belgesi veya fatura numarasi."},
            "document_type": {
                "type": "string",
                "enum": [
                    "auto", "purchase_requisition", "purchase_order",
                    "goods_receipt", "supplier_invoice",
                ],
                "default": "auto",
            },
            "detail": DETAIL_SCHEMA,
        },
        "required": ["document_id"],
    },
)
def sap_document_flow(
    ctx: ToolContext,
    document_id: str,
    document_type: str = "auto",
    detail: str = "standard",
) -> ToolResult | dict[str, Any]:
    level = resolve_detail(detail)
    try:
        nodes = ctx.sap.get_document_flow(document_id, document_type=document_type)
    except SAPNotSupported as exc:
        return {"error": str(exc), "remediation": exc.hint, "denial_code": "CAPABILITY_NOT_SUPPORTED"}
    except SAPError as exc:
        return {"error": str(exc), "sap_code": exc.code}

    if not nodes:
        return {
            "document_id": document_id,
            "chain": [],
            "interpretation": (
                f"'{document_id}' icin SAP'ta izlenebilir bir belge bagi bulunamadi. "
                "Numara yanlis olabilir veya belge henuz bir siparise donusmemis olabilir."
            ),
            "next_steps": [
                "Belge numarasini ve tipini dogrulayin.",
            ],
        }

    nodes.sort(key=lambda n: (_FLOW_ORDER.get(n.document_type, 9), n.document_id, n.item_no))
    by_type: dict[str, list[Any]] = {}
    for node in nodes:
        by_type.setdefault(node.document_type, []).append(node)

    stages = [
        {
            "stage": _DOC_LABELS.get(kind, kind),
            "type": kind,
            "count": len(items),
            "documents": sorted({n.document_id for n in items}),
        }
        for kind, items in sorted(by_type.items(), key=lambda kv: _FLOW_ORDER.get(kv[0], 9))
    ]

    # Girdi belgesinin **kendi** tipi: zincirin ilk dugumu degil. Fatura
    # numarasiyla sorgulanan bir zincir PR ile baslar; `resolved_type` yine de
    # "supplier_invoice" olmali, aksi halde cikti kullaniciyi yaniltir.
    resolved_type = (
        document_type
        if document_type != "auto"
        else next(
            (n.document_type for n in nodes if n.document_id == document_id.strip()),
            "unknown",
        )
    )

    data: dict[str, Any] = {
        "document_id": document_id,
        "resolved_type": resolved_type,
        "stages": stages,
        "chain_complete": "payment" in by_type,
        "interpretation": _flow_interpretation(by_type),
    }
    if level != "summary":
        limit = page_limit(level, None, default=25)
        data["chain"] = [
            {
                "type": node.document_type,
                "document_id": node.document_id,
                "item_no": node.item_no or None,
                "date": _iso(node.document_date),
                "status": node.status or None,
                "quantity": node.quantity,
                "unit": node.unit or None,
                "amount": node.amount,
                "currency": node.currency or None,
                # Bagin kaniti: hangi SAP alani bu dugumu oncekine baglar.
                "linked_by": node.linked_by,
                "predecessor": node.predecessor_id or None,
                "notes": node.notes or None,
            }
            for node in nodes[:limit]
        ]
        if len(nodes) > limit:
            data["chain_truncated"] = len(nodes) - limit

    result = ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            "document_flow",
            business_object=document_id,
            record_count=len(nodes),
            notes=("Her belge bagi SAP referans alanindan kuruldu; cikarim yapilmadi.",),
        ),
        returned_count=len(nodes),
    )
    if "payment" not in by_type and "supplier_invoice" in by_type:
        result.warn("Fatura mevcut ama odeme belgesi yok; odeme bloke veya vadesi gelmemis olabilir.")
    return result


def _flow_interpretation(by_type: dict[str, list[Any]]) -> str:
    if "payment" in by_type:
        return "Zincir tamamlanmis: siparis teslim alinmis, faturalanmis ve odenmis."
    if "supplier_invoice" in by_type:
        blocked = [n for n in by_type["supplier_invoice"] if n.status == "blocked"]
        if blocked:
            return (
                "Fatura kesilmis ancak bloke durumda. Nedeni icin "
                "sap_invoice_block_explain kullanin."
            )
        return "Fatura kayitli, odeme henuz gorunmuyor."
    if "goods_receipt" in by_type:
        return "Mal kabul yapilmis, tedarikci faturasi henuz girilmemis (GR/IR farki olusur)."
    if "purchase_order" in by_type:
        return "Siparis acilmis, henuz mal kabul kaydi yok."
    return "Zincir talep asamasinda; siparise donusmemis."


# ---------------------------------------------------------------------------
@tool(
    name="sap_purchase_order_360",
    group="p2p",
    domain="p2p_flow",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=1200,
    data_policy=_P2P_DATA_POLICY,
    impact_profile=READ_ONLY,
    cache_policy=_P2P_CACHE,
    performance_budget=PerformanceBudget(p95_ms=6000, max_sap_calls=6, max_records=200),
    description=(
        "Tek bir satinalma siparisinin tam durumu: kalemler, teslimat plani satirlari, "
        "teyit edilen tarihler ve gecikme gunu, yapilan mal kabuller, faturalanan miktar ve "
        "GR/IR (mal kabul-fatura) farki. Acik miktar ve gecikme deterministik olarak "
        "hesaplanir. 'Siparis nerede' sorusunun cevabi."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "po_id": {"type": "string", "description": "Satinalma siparisi numarasi (EKKO-EBELN)."},
            "detail": DETAIL_SCHEMA,
        },
        "required": ["po_id"],
    },
)
def sap_purchase_order_360(
    ctx: ToolContext, po_id: str, detail: str = "standard"
) -> ToolResult | dict[str, Any]:
    level = resolve_detail(detail)
    # Yetenekler bagimsizdir; dort okumanin tamami ayni hata korumasindan
    # gecmelidir.
    try:
        items = ctx.sap.get_purchase_order_items(po_id)
        if not items:
            return {
                "po_id": po_id,
                "error": f"Satinalma siparisi {po_id} bulunamadi veya kalem icermiyor.",
                "sap_code": "EKPO_NOT_FOUND",
            }
        # Kalemler dondukten SONRA kalan uc okuma birbirinden bagimsizdir ve
        # paralel gider. Kalem okumasi bilerek disarida: PO yoksa erken donup
        # uc gereksiz cagriyi hic yapmiyoruz.
        rest = gather_named(
            {
                "schedule": lambda: ctx.sap.get_schedule_lines(po_id),
                "receipts": lambda: ctx.sap.get_goods_receipts(po_id=po_id),
                "invoices": lambda: ctx.sap.get_supplier_invoices(po_id=po_id),
            },
            max_workers=ctx.settings.risk.max_parallel_reads,
        )
        schedule = rest["schedule"]
        receipts = rest["receipts"]
        invoices = rest["invoices"]
    except SAPNotSupported as exc:
        return {"error": str(exc), "remediation": exc.hint, "denial_code": "CAPABILITY_NOT_SUPPORTED"}
    except SAPError as exc:
        return {"error": str(exc), "sap_code": exc.code}

    # Released PO API kismi teslim/fatura miktarini kalemde yayinlamaz. Bunlar
    # ayni kosuda okunan MSEG/RSEG referanslarindan netlestirilir; "complete"
    # bayragindan miktar tahmin edilmez.
    delivered_by_item: dict[str, float] = {}
    for receipt in receipts:
        if not receipt.po_item:
            continue
        # `signed_quantity` isareti hareket tipinden turetir: iptal edilmis
        # asil satir + , onu goturen ters kayit - . Toplam SAP aritmetigiyle
        # ayni sonucu verir.
        delivered_by_item[receipt.po_item] = round(
            delivered_by_item.get(receipt.po_item, 0.0) + receipt.signed_quantity,
            3,
        )
    invoiced_by_item: dict[str, float] = {}
    for invoice in invoices:
        for key, quantity in invoice.po_item_quantities.items():
            item_po, _, item_no = key.partition("/")
            if item_po != po_id or not item_no:
                continue
            invoiced_by_item[item_no] = round(
                invoiced_by_item.get(item_no, 0.0) + quantity,
                3,
            )
    for item in items:
        if item.item_no in delivered_by_item:
            item.delivered_qty = max(0.0, delivered_by_item[item.item_no])
        if item.item_no in invoiced_by_item:
            item.invoiced_qty = max(0.0, invoiced_by_item[item.item_no])

    # --- Deterministik hesaplar: LLM toplam hesaplamaz ----------------------
    total_value = round(sum(i.net_value for i in items), 2)
    open_value = round(
        sum(i.net_price * i.open_qty for i in items), 2
    )
    gr_ir_gap_qty = round(sum(i.uninvoiced_qty for i in items), 3)
    gr_ir_gap_value = round(sum(i.net_price * i.uninvoiced_qty for i in items), 2)
    max_delay = max((line.delay_days for line in schedule), default=0)
    open_items = [i for i in items if not i.fully_delivered]
    currency = items[0].currency
    reversals = [gr for gr in receipts if gr.is_reversal]

    data: dict[str, Any] = {
        "po_id": po_id,
        "item_count": len(items),
        "open_item_count": len(open_items),
        "total_value": total_value,
        "open_value": open_value,
        "currency": currency,
        "delivered_pct": round(
            sum(i.delivered_qty for i in items) / sum(i.quantity for i in items) * 100, 1
        )
        if sum(i.quantity for i in items)
        else 0.0,
        "invoice_count": len(invoices),
        "goods_receipt_count": len(receipts),
        "gr_ir_gap_qty": gr_ir_gap_qty or None,
        "gr_ir_gap_value": gr_ir_gap_value or None,
        "max_delay_days": max_delay or None,
        "blocked_invoices": [inv.invoice_id for inv in invoices if inv.is_blocked],
    }
    data["interpretation"] = _po_interpretation(
        open_items=open_items, max_delay=max_delay, gap=gr_ir_gap_qty, invoices=invoices
    )

    if level != "summary":
        limit = page_limit(level, None, default=20)
        data["items"] = [
            {
                "item_no": i.item_no,
                "material_id": i.material_id,
                "description": i.description,
                "quantity": i.quantity,
                "unit": i.unit,
                "delivered_qty": i.delivered_qty,
                "open_qty": i.open_qty or None,
                "invoiced_qty": i.invoiced_qty,
                "uninvoiced_qty": i.uninvoiced_qty or None,
                "net_price": i.net_price,
                "net_value": i.net_value,
                "wbs_element": i.wbs_element,
            }
            for i in items[:limit]
        ]
        data["schedule_lines"] = [
            {
                "item_no": line.item_no,
                "line": line.schedule_line,
                "requested_date": _iso(line.requested_date),
                "confirmed_date": _iso(line.confirmed_date),
                "delay_days": line.delay_days or None,
                "quantity": line.quantity,
                "delivered_qty": line.delivered_qty,
            }
            for line in schedule[:limit]
        ]
    if level == "full":
        data["goods_receipts"] = [
            {
                "material_document": gr.material_document,
                "posting_date": _iso(gr.posting_date),
                "movement_type": gr.movement_type,
                "quantity": gr.quantity,
                "reversal": gr.is_reversal,
                "batch": gr.batch or None,
            }
            for gr in receipts
        ]
        data["invoices"] = [
            {
                "invoice_id": inv.invoice_id,
                "posting_date": _iso(inv.posting_date),
                "gross_amount": inv.gross_amount,
                "status": inv.status,
                "payment_block": inv.payment_block or None,
            }
            for inv in invoices
        ]

    result = ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            "purchase_order, schedule_lines, material_document, supplier_invoice",
            business_object=po_id,
            record_count=len(items) + len(schedule) + len(receipts) + len(invoices),
            notes=("Acik miktar, gecikme ve GR/IR farki kodda hesaplandi.",),
        ),
        returned_count=len(items),
    )
    if max_delay:
        result.warn(f"Teslimat plani {max_delay} gun gecikmeli teyit edilmis.")
    if gr_ir_gap_qty:
        result.warn(
            f"GR/IR farki: {gr_ir_gap_qty:g} birim teslim alinmis ama faturalanmamis "
            f"({gr_ir_gap_value:,.2f} {currency})."
        )
    if reversals:
        result.warn(
            f"{len(reversals)} adet iptal (102/122) hareketi var; net teslim miktarini "
            "degerlendirirken dikkate alin."
        )
    return result


def _po_interpretation(*, open_items, max_delay: int, gap: float, invoices) -> str:
    parts: list[str] = []
    if not open_items:
        parts.append("Tum kalemler teslim alinmis.")
    else:
        parts.append(f"{len(open_items)} kalemde acik miktar var.")
    if max_delay:
        parts.append(f"En buyuk teyit gecikmesi {max_delay} gun.")
    if gap:
        parts.append("Mal kabul yapilmis ancak faturalanmamis miktar mevcut (GR/IR farki).")
    blocked = [inv for inv in invoices if inv.is_blocked]
    if blocked:
        parts.append(
            f"{len(blocked)} fatura bloke; nedeni icin sap_invoice_block_explain kullanin."
        )
    return " ".join(parts)
@tool(
    name="sap_supplier_invoice_status",
    group="p2p",
    domain="p2p_finance",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=1100,
    data_policy=_P2P_DATA_POLICY,
    impact_profile=READ_ONLY,
    cache_policy=_P2P_CACHE,
    performance_budget=PerformanceBudget(p95_ms=4000, max_sap_calls=3, max_records=100),
    description=(
        "Tedarikci faturalarinin muhasebe ve odeme durumu: kayitli/bloke/odenmis, odeme "
        "blokaj anahtari, vade ve gecikme gunu, muhasebe belgesi ve referans siparis. "
        "Fatura numarasi, siparis numarasi veya tedarikci ile sorgulanabilir; yalniz bloke "
        "faturalari listelemek icin only_blocked kullanin."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "invoice_id": {"type": "string"},
            "po_id": {"type": "string"},
            "vendor_id": {"type": "string"},
            "only_blocked": {"type": "boolean", "default": False},
            "limit": {"type": "integer"},
            "detail": DETAIL_SCHEMA,
        },
    },
)
def sap_supplier_invoice_status(
    ctx: ToolContext,
    invoice_id: str = "",
    po_id: str = "",
    vendor_id: str = "",
    only_blocked: bool = False,
    limit: int | None = None,
    detail: str = "standard",
) -> ToolResult | dict[str, Any]:
    if not any((invoice_id, po_id, vendor_id, only_blocked)):
        return {
            "error": "En az bir filtre gerekli: invoice_id, po_id, vendor_id veya only_blocked.",
            "denial_code": "FILTER_REQUIRED",
        }

    level = resolve_detail(detail)
    page = page_limit(level, limit, default=20)
    try:
        invoices = ctx.sap.get_supplier_invoices(
            invoice_id=invoice_id, po_id=po_id, vendor_id=vendor_id,
            only_blocked=only_blocked, limit=page,
        )
    except SAPNotSupported as exc:
        return {"error": str(exc), "remediation": exc.hint, "denial_code": "CAPABILITY_NOT_SUPPORTED"}
    except SAPError as exc:
        return {"error": str(exc), "sap_code": exc.code}

    if not invoices:
        return {
            "query": {"invoice_id": invoice_id, "po_id": po_id, "vendor_id": vendor_id},
            "invoices": [],
            "invoice_count": 0,
            "returned_invoice_count": 0,
            "parked_count": 0,
            "cancelled_count": 0,
            "invoice_record_found": False,
            "invoice_issued": False,
            "interpretation": (
                "Bu siparis icin fatura kaydi bulunamadi."
                if po_id
                else "Verilen filtreye uyan tedarikci faturasi bulunamadi."
            ),
        }

    today = _today()
    # Iptal belgeler borc/fatura adedine, park belgeler de
    # muhasebelesmis fatura adedine katilmaz. Ham kayitlar asagidaki tabloda
    # gorunur kalir; operasyonel karar alanlari yalniz etkin belgelerden
    # hesaplanir.
    issued = [inv for inv in invoices if inv.status in {"posted", "blocked", "paid"}]
    parked = [inv for inv in invoices if inv.status == "parked"]
    cancelled = [inv for inv in invoices if inv.status == "cancelled"]
    blocked = [inv for inv in issued if inv.is_blocked]
    overdue = [inv for inv in issued if inv.days_overdue(today=today) > 0]
    total_gross_by_currency = _gross_by_currency(issued)
    blocked_gross_by_currency = _gross_by_currency(blocked)

    rows = [
        {
            "invoice_id": inv.invoice_id,
            "vendor_id": inv.vendor_id,
            "vendor_name": inv.vendor_name,
            "status": inv.status,
            "gross_amount": inv.gross_amount,
            "currency": inv.currency,
            "posting_date": _iso(inv.posting_date),
            "due_date": _iso(inv.due_date),
            "days_overdue": inv.days_overdue(today=today) or None,
            "payment_block": inv.payment_block or None,
            "block_reasons": sorted({b.block_reason for b in inv.blocks}) or None,
            "accounting_document": inv.accounting_document or None,
            "po_ids": inv.po_ids,
            "paid_on": _iso(inv.paid_on),
        }
        for inv in invoices
    ]

    data: dict[str, Any] = {
        "query": {"invoice_id": invoice_id, "po_id": po_id, "vendor_id": vendor_id},
        "invoice_count": len(issued),
        "returned_invoice_count": len(invoices),
        "parked_count": len(parked),
        "cancelled_count": len(cancelled),
        "invoice_record_found": bool(issued or parked),
        "invoice_issued": bool(issued),
        "blocked_count": len(blocked),
        "overdue_count": len(overdue),
        # Farkli para birimleri kur bilgisi olmadan toplanamaz. Bu iki alan
        # her zaman para birimi bazindadir; tekil toplamlar asagida yalniz
        # gercekten tek para birimi varsa geriye donuk uyumluluk icin eklenir.
        "total_gross_by_currency": total_gross_by_currency,
        "blocked_gross_by_currency": blocked_gross_by_currency or None,
        "currencies": sorted(total_gross_by_currency),
        "invoices": rows if level != "summary" else rows[: page_limit("summary", None, default=20)],
    }
    if len(total_gross_by_currency) == 1:
        currency, total_gross = next(iter(total_gross_by_currency.items()))
        data["currency"] = currency
        data["total_gross"] = total_gross
        data["blocked_gross"] = blocked_gross_by_currency.get(currency) or None
    data["interpretation"] = (
        _po_invoice_interpretation(issued, parked, cancelled)
        if po_id
        # "incelendi" ham filtre sonucunu anlatir; yalniz muhasebelesmis
        # faturalarin sayisini degil. Tek bir iptal fatura kimligiyle sorgu
        # yapildiginda `issued` bos olsa da bir kayit gercekten incelenmistir.
        else _invoice_interpretation(invoices, blocked, overdue, blocked_gross_by_currency)
    )
    if blocked:
        data["next_steps"] = [
            "Blokaj nedenini sayisal olarak gormek icin sap_invoice_block_explain calistirin.",
        ]

    result = ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            "supplier_invoice",
            business_object=invoice_id or po_id or vendor_id,
            record_count=len(invoices),
            notes=("Vade gecikmesi kayit tarihine gore kodda hesaplandi.",),
        ),
        returned_count=len(invoices),
    )
    if blocked:
        result.warn(f"{len(blocked)} fatura odeme icin bloke.")
    if overdue:
        result.warn(f"{len(overdue)} fatura vadesi gecmis.")
    return result


def _gross_by_currency(invoices) -> dict[str, float]:
    """Kur uydurmadan fatura tutarlarini belge para biriminde toplar."""
    totals: dict[str, float] = {}
    for invoice in invoices:
        currency = str(invoice.currency or "UNSPECIFIED").strip().upper()
        totals[currency] = round(totals.get(currency, 0.0) + invoice.gross_amount, 2)
    return dict(sorted(totals.items()))


def _invoice_interpretation(invoices, blocked, overdue, blocked_totals) -> str:
    if not blocked and not overdue:
        return f"{len(invoices)} fatura incelendi; blokaj veya vade gecikmesi yok."
    parts = []
    if blocked:
        amounts = "; ".join(
            f"{amount:,.2f} {currency}" for currency, amount in blocked_totals.items()
        )
        parts.append(f"{len(blocked)} fatura bloke ({amounts}).")
    if overdue:
        parts.append(f"{len(overdue)} fatura vadesi gecmis.")
    return " ".join(parts)


def _po_invoice_interpretation(issued, parked, cancelled) -> str:
    """Tek PO icin model gerektirmeyen, dogrudan evet/hayir karari."""
    if issued:
        statuses: dict[str, int] = {}
        for invoice in issued:
            statuses[invoice.status] = statuses.get(invoice.status, 0) + 1
        status_text = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
        answer = f"Evet, bu siparis icin {len(issued)} aktif fatura bulundu ({status_text})."
        if cancelled:
            answer += f" Ayrica {len(cancelled)} iptal edilmis fatura kaydi var."
        return answer
    if parked:
        answer = (
            f"Bu siparis icin {len(parked)} park edilmis fatura kaydi var; "
            "henuz muhasebelesmis aktif fatura yok."
        )
        if cancelled:
            answer += f" Ayrica {len(cancelled)} iptal edilmis kayit var."
        return answer
    if cancelled:
        return (
            "Hayir, bu siparis icin aktif fatura bulunamadi; "
            f"yalnizca {len(cancelled)} iptal edilmis fatura kaydi var."
        )
    return "Hayir, bu siparis icin fatura kaydi bulunamadi."


# ---------------------------------------------------------------------------
@tool(
    name="sap_invoice_block_explain",
    group="p2p",
    domain="p2p_finance",
    risk_tier=RiskTier.R1,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=1100,
    data_policy=_P2P_DATA_POLICY,
    impact_profile=ImpactProfile(
        mutation=MutationKind.COMPUTE, reversible=Reversibility.EASY
    ),
    cache_policy=CachePolicy(
        ttl_seconds=45, max_class=DataClass.D2, invalidated_by=("invoice_id",)
    ),
    performance_budget=PerformanceBudget(p95_ms=4000, max_sap_calls=4, max_records=100),
    description=(
        "Bloke bir tedarikci faturasinin nedenini sayisal olarak acikliar: hangi kalemde, "
        "fiyat mi miktar mi tarih mi, beklenen ve gercek deger ne, sapma mutlak/yuzde olarak "
        "ne kadar ve hangi SAP tolerans anahtari (OMR6) asilmis. Sapma ve tolerans "
        "karsilastirmasi kodda hesaplanir; cozum icin somut adim onerir."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "invoice_id": {"type": "string", "description": "Tedarikci fatura numarasi (RBKP-BELNR)."},
            "detail": DETAIL_SCHEMA,
        },
        "required": ["invoice_id"],
    },
)
def sap_invoice_block_explain(
    ctx: ToolContext, invoice_id: str, detail: str = "standard"
) -> ToolResult | dict[str, Any]:
    level = resolve_detail(detail)
    try:
        invoices = ctx.sap.get_supplier_invoices(invoice_id=invoice_id, limit=1)
    except SAPNotSupported as exc:
        return {"error": str(exc), "remediation": exc.hint, "denial_code": "CAPABILITY_NOT_SUPPORTED"}
    except SAPError as exc:
        return {"error": str(exc), "sap_code": exc.code}

    if not invoices:
        return {
            "invoice_id": invoice_id,
            "error": f"Tedarikci faturasi {invoice_id} bulunamadi.",
            "sap_code": "RBKP_NOT_FOUND",
        }

    invoice = invoices[0]
    if not invoice.is_blocked:
        return {
            "invoice_id": invoice.invoice_id,
            "blocked": False,
            "status": invoice.status,
            "interpretation": (
                f"Fatura {invoice.invoice_id} bloke degil (durum: {invoice.status}). "
                "Blokaj aciklamasi gerekmiyor."
            ),
        }

    # --- Deterministik sapma hesabi: LLM tolerans hesaplamaz ----------------
    findings: list[dict[str, Any]] = []
    for block in invoice.blocks:
        expected = block.expected_value
        actual = block.actual_value
        variance_abs = (
            round(actual - expected, 4) if expected is not None and actual is not None else None
        )
        variance_pct = (
            round((actual - expected) / expected * 100, 2)
            if expected not in (None, 0) and actual is not None
            else None
        )
        exceeded = _exceeded_limits(block, variance_abs, variance_pct)
        findings.append(
            {
                "item_no": block.item_no or None,
                "reason": block.block_reason,
                "tolerance_key": block.tolerance_key or None,
                "expected": expected,
                "actual": actual,
                "variance_abs": variance_abs,
                "variance_pct": variance_pct,
                "tolerance_limit_abs": block.tolerance_limit_abs,
                "tolerance_limit_pct": block.tolerance_limit_pct,
                "exceeded_limits": exceeded or None,
                "po_id": block.po_id or None,
                "po_item": block.po_item or None,
                "description": block.description or None,
                "resolution": _resolution_for(block.block_reason),
            }
        )

    reasons = sorted({f["reason"] for f in findings})
    data: dict[str, Any] = {
        "invoice_id": invoice.invoice_id,
        "blocked": True,
        "status": invoice.status,
        "payment_block": invoice.payment_block or None,
        "gross_amount": invoice.gross_amount,
        "currency": invoice.currency,
        "vendor_id": invoice.vendor_id,
        "po_ids": invoice.po_ids,
        "block_count": len(findings),
        "block_reasons": reasons,
        "findings": findings if level != "summary" else findings[:3],
        "assumptions": [
            "Tolerans limitleri SAP OMR6 yapilandirmasindan okunur; burada yeniden tanimlanmaz.",
            "Sapma yuzdesi (gercek - beklenen) / beklenen formuluyle kodda hesaplandi.",
            "Blokaj kaldirma islemi bu tool'un kapsaminda degildir; yalniz aciklama uretir.",
        ],
        "interpretation": _block_interpretation(invoice, reasons),
        "next_steps": _block_next_steps(reasons, invoice),
    }

    result = ToolResult(
        data=data,
        detail=level,
        evidence=ctx.sap_evidence(
            "supplier_invoice, invoice_blocks",
            business_object=invoice.invoice_id,
            record_count=len(findings),
            notes=("Sapma ve tolerans karsilastirmasi deterministik kodda yapildi.",),
        ),
        returned_count=len(findings),
    )
    result.warn(
        "Blokaj kaldirma finansal etkili bir islemdir ve bu salt-okunur tool ile yapilmaz."
    )
    return result


def _exceeded_limits(block, variance_abs: float | None, variance_pct: float | None) -> list[str]:
    """Hangi tolerans siniri asilmis? Sinir tanimlanmamissa iddia edilmez."""
    exceeded: list[str] = []
    if (
        block.tolerance_limit_abs is not None
        and variance_abs is not None
        and abs(variance_abs) > block.tolerance_limit_abs
    ):
        exceeded.append(f"mutlak (>{block.tolerance_limit_abs:g})")
    if (
        block.tolerance_limit_pct is not None
        and variance_pct is not None
        and abs(variance_pct) > block.tolerance_limit_pct
    ):
        exceeded.append(f"yuzde (>{block.tolerance_limit_pct:g}%)")
    return exceeded


def _resolution_for(reason: str) -> str:
    return {
        "price": (
            "Tedarikci ile fiyat farkini teyit edin; hakliysa siparis fiyatini duzeltin, "
            "degilse tedarikciden duzeltilmis fatura isteyin."
        ),
        "quantity": (
            "Mal kabul miktarini ve iptal kayitlarini kontrol edin; eksik mal kabul varsa "
            "once GR kaydini tamamlayin."
        ),
        "date": "Teslim/fatura tarihini siparis teslimat plani ile karsilastirin.",
        "order_price_unit": "Siparis fiyat birimi ile fatura birimini esitleyin.",
        "quality": "Kalite kaydini ve muayene sonucunu kontrol edin.",
        "amount": "Toplam tutar farkini kalem bazinda ayristirin.",
        "manual": "Blokaji koyan kullanicidan gerekce isteyin.",
        # Neden kaynak API'dan okunamadiginda uydurulmus bir yonlendirme
        # vermek, kullaniciyi yanlis islemin ustune yollar. Dogru cevap,
        # nedenin nereden okunacagini soylemektir.
        "unknown": (
            "Blokaj nedeni bu kaynaktan okunamiyor. MIRO/MRBR uzerinden faturanin "
            "kalem bazli blokaj gerekcelerini ve OMR6 tolerans asimlarini kontrol edin."
        ),
    }.get(reason, "Blokaj nedenini ilgili satinalma ve muhasebe sorumlusuyla degerlendirin.")


def _block_interpretation(invoice, reasons: list[str]) -> str:
    labels = {
        "price": "fiyat farki",
        "quantity": "miktar farki",
        "date": "tarih sapmasi",
        "quality": "kalite kaydi",
        "manual": "manuel blokaj",
        "amount": "tutar farki",
        "order_price_unit": "fiyat birimi uyusmazligi",
        "unknown": "nedeni bu kaynaktan okunamayan blokaj",
    }
    listed = ", ".join(labels.get(r, r) for r in reasons)
    block_note = (
        f" Odeme blokaj anahtari '{invoice.payment_block}'." if invoice.payment_block else ""
    )
    return (
        f"Fatura {invoice.invoice_id} su nedenle bloke: {listed}.{block_note} "
        "Sapma degerleri ve asilan tolerans sinirlari asagida kalem bazinda listelendi."
    )


def _block_next_steps(reasons: list[str], invoice) -> list[str]:
    steps: list[str] = []
    if invoice.po_ids:
        steps.append(
            f"sap_purchase_order_360 ile {invoice.po_ids[0]} siparisinin mal kabul ve "
            "faturalanan miktarini karsilastirin."
        )
    steps.append(
        f"{invoice.invoice_id} faturasinin blokaj incelemesinin "
        "kimde bekledigini gorun."
    )
    if "quantity" in reasons:
        steps.append("sap_document_flow ile iptal (102/122) mal kabul kaydi olup olmadigini dogrulayin.")
    return steps
