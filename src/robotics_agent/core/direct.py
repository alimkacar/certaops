"""Modeli atlayan dogrudan yanit yolu (deterministik cikti).

Sorun
-----
Bir kullanici "HD-GEAR-CSF25-100 stok durumu" diye sordugunda klasik akis
sudur: (1) soru modele gider, (2) model tool cagirir, (3) **tool sonucu
modele geri gonderilir**, (4) model onu cumleye cevirir. Ucuncu adimda SAP
verisinin tamami LLM API'sine cikar. Ama bu ornekte modelin katkisi
bicimlendirmeden ibarettir: karar yok, sentez yok, yorum yok.

Bu modul o durumu tanir ve iki kademeli kisayol saglar:

``shortcut``        Soru deterministik olarak tek bir salt-okunur tool'a
                    esleniyorsa model **hic cagrilmaz**. SAP verisi surecin
                    disina hic cikmaz; gizlilik acisindan en guclu hal budur.

``self_contained``  Model tool'u zaten cagirdiysa ve sonuc kendi kendine
                    yeterliyse, sonuc modele GERI GONDERILMEZ; yanit yerel
                    olarak uretilir. Bir LLM round-trip'i ve tum tool
                    payload'inin ikinci kez API'ye gitmesi ortadan kalkar.

Guvenlik duruşu
---------------
Bu bir optimizasyon oldugu kadar bir **yetki yuzeyi**dir; o yuzden dar ve
acik tutulur:

* ``DIRECT_ANSWER_TOOLS`` bir **allowlist**tir. Listede olmayan hicbir tool
  modeli atlayamaz. Mutating (R2+) tool'lar listeye alinamaz - kayit
  sirasinda kontrol edilir.
* Kisayol yolu policy gate'i, DLP'yi, org kapsam kontrolunu ve denetim
  defterini **atlamaz**: tool yine `execute_tool` uzerinden calisir.
* Uretilen metin `sanitize_for_client` kapisindan gecer (cagiran taraf
  uygular). Tool payload'i `sink="model"` icin temizlenmisti; istemci sink'i
  daha katidir ve ikinci kez uygulanir.
* Karar verilemeyen her durumda **normal LLM akisina duser** (fail-open to
  the safe path): eslesme kismiysa, sonuc hataliysa, `needs_review` varsa
  veya renderer patlarsa kisayol kullanilmaz.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "DIRECT_ANSWER_TOOLS",
    "SHORTCUTS",
    "DirectAnswer",
    "DirectAnswerSpec",
    "IntentShortcut",
    "ShortcutMatch",
    "direct_answer_for",
    "match_shortcut",
    "shortcut_catalogue",
]


@dataclass(frozen=True)
class DirectAnswer:
    """Modele ugramadan uretilmis yanit."""

    text: str
    tool: str
    #: "shortcut" (LLM hic cagrilmadi) | "self_contained" (son tur atlandi)
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "reason": self.reason, "chars": len(self.text)}


def _always(_: dict[str, Any]) -> bool:
    return True


@dataclass(frozen=True)
class DirectAnswerSpec:
    """Bir tool sonucunu deterministik metne ceviren sozlesme.

    ``sufficient`` tool'a "bu sonuc tek basina cevap mi?" diye sorar. Ornegin
    stok sorgusunda eksik kalem varsa model devreye girip alternatif
    onermelidir; o durumda kisayol kullanilmaz.
    """

    render: Callable[[dict[str, Any]], str]
    sufficient: Callable[[dict[str, Any]], bool] = _always
    #: Kullanicinin sorusundan dogrudan (LLM'siz) tetiklenebilir mi?
    allow_shortcut: bool = True
    #: Model bu tool'u daha genis bir isin parcasi olarak sectiginde sonucu
    #: modele geri vermeden tur bitirilebilir mi? Liste araclarinda genellikle
    #: hayir: veri tek basina dogru olsa bile kullanicinin istedigi sentez
    #: tamamlanmamis olabilir.
    allow_self_contained: bool = True


# --- Yardimcilar -------------------------------------------------------------
def _num(value: Any, digits: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{number:,.{digits}f}".replace(",", " ")
    return formatted


def _lines(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _meta_note(payload: dict[str, Any]) -> str:
    """Cache tazeligi ve kirpma notu - model olmadan da kullaniciya gosterilir."""
    meta = payload.get("_meta")
    if not isinstance(meta, dict):
        return ""
    notes: list[str] = []
    age = meta.get("age_seconds")
    if isinstance(age, int | float) and age > 0:
        notes.append(f"onbellekten ({int(age)} sn once okundu)")
    if meta.get("evidence_id"):
        notes.append(f"tam kayit: {meta['evidence_id']}")
    return f"\n_({'; '.join(notes)})_" if notes else ""


# --- Renderer'lar ------------------------------------------------------------
def _render_stock(payload: dict[str, Any]) -> str:
    rows = payload.get("materials") or []
    if not rows:
        return ""
    out = [f"**Stok durumu** ({payload.get('checked_on', '')})"]
    for row in rows:
        flags = row.get("risk_flags") or []
        out.append(
            f"\n- **{row.get('material_id', '')}** - {row.get('description', '')}\n"
            f"  - Tesis {row.get('plant', '')}: serbest {_num(row.get('unrestricted'))} "
            f"{row.get('unit', '')}, rezerve {_num(row.get('reserved'))}, "
            f"kullanilabilir {_num(row.get('unreserved'))}\n"
            f"  - Yolda: {_num(row.get('on_order'))} | emniyet stogu: "
            f"{_num(row.get('safety_stock'))} | tedarik suresi: "
            f"{row.get('best_lead_time_days', '?')} gun"
            + (f"\n  - Dikkat: {'; '.join(flags)}" if flags else "")
        )
    missing = payload.get("not_found") or []
    if missing:
        out.append(f"\nBulunamayan malzeme: {', '.join(missing)}")
    out.append(f"\n{payload.get('basis', '')}")
    return _lines(*out) + _meta_note(payload)


def _stock_is_sufficient(payload: dict[str, Any]) -> bool:
    """Eksik varsa model devreye girsin: alternatif/aksiyon onerisi gerekir."""
    return not payload.get("shortages") and int(payload.get("shortage_count") or 0) == 0


def _render_material(payload: dict[str, Any]) -> str:
    if not payload.get("material_id"):
        return ""
    out = [
        f"**{payload.get('material_id', '')}** - {payload.get('description', '')}",
        f"- Tur: {payload.get('type', '?')} | grup: {payload.get('material_group', '?')} "
        f"| birim: {payload.get('unit', '?')} | tesis: {payload.get('plant', '?')}",
        f"- Fiyat: {_num(payload.get('price'), 2)} {payload.get('currency', '')} "
        f"(kaynak: {payload.get('price_source', '?')})",
        f"- Tedarik: {payload.get('procurement_type', '?')}, "
        f"{payload.get('lead_time_days', '?')} gun, min siparis "
        f"{_num(payload.get('min_order_qty'))}",
    ]
    stock = payload.get("stock") or {}
    if stock:
        out.append(
            f"- Stok: serbest {_num(stock.get('unrestricted'))}, "
            f"rezerve {_num(stock.get('reserved'))}, yolda {_num(stock.get('on_order'))}"
        )
    sources = payload.get("sources") or []
    if sources:
        out.append(f"- Kaynaklar ({payload.get('source_count', len(sources))}):")
        for src in sources[:5]:
            out.append(
                f"  - {src.get('vendor_id', '')} {src.get('vendor_name', '')}: "
                f"{_num(src.get('net_price'), 2)} {src.get('currency', '')}, "
                f"{src.get('lead_time_days', '?')} gun"
            )
    if payload.get("single_source"):
        out.append("- Dikkat: tek kaynak (alternatif tedarikci yok).")
    return _lines(*out) + _meta_note(payload)


def _render_material_search(payload: dict[str, Any]) -> str:
    """Malzeme arama sonucu zaten bir listedir; model yorumu gerektirmez."""
    rows = payload.get("materials") or []
    if not rows:
        return "**Malzeme aramasi**\n- Eslesen malzeme bulunamadi." + _meta_note(payload)
    out = [f"**Malzeme aramasi** ({payload.get('result_count', len(rows))} sonuc)"]
    for row in rows[:20]:
        price = row.get("price")
        price_text = (
            f"{_num(price, 2)} {row.get('currency', '')}" if price is not None else "okunamadi"
        )
        out.append(
            f"- **{row.get('material_id', '')}** - {row.get('description', '')}\n"
            f"  - Grup {row.get('material_group', '?')} | tur {row.get('type', '?')} "
            f"| birim {row.get('unit', '?')}\n"
            f"  - Fiyat {price_text} | tedarik {row.get('lead_time_days', '?')} gun"
        )
    return _lines(*out) + _meta_note(payload)


def _render_purchase_orders(payload: dict[str, Any]) -> str:
    orders = payload.get("orders") or []
    if not orders:
        return ""
    count = payload.get("order_count", len(orders))
    # Karisik para biriminde tek toplam YOKTUR (kur uydurulmaz); toplamlar
    # para birimi basina gelir ve ozet de oyle yazilir.
    by_currency = payload.get("open_value_by_currency") or {}
    if payload.get("total_open_value") is not None:
        toplam = f"toplam {_num(payload.get('total_open_value'), 2)} {payload.get('currency', '')}"
    elif by_currency:
        toplam = "toplam " + " + ".join(
            f"{_num(value, 2)} {code}" for code, value in by_currency.items()
        )
    else:
        toplam = "tutar okunamadi"
    out = [f"**Acik satinalma siparisleri** ({count} adet, {toplam})"]
    for order in orders[:20]:
        delay = order.get("delay_days") or 0
        marker = f" - {delay} gun oteleme" if delay else ""
        out.append(
            f"\n- **{order.get('po_id', '')}** {order.get('material_id', '')} "
            f"({order.get('description', '')})\n"
            f"  - Tedarikci: {order.get('vendor', '')}\n"
            f"  - Miktar {_num(order.get('quantity'))}, teslim "
            f"{_num(order.get('delivered'))}, acik {_num(order.get('open_qty'))}\n"
            f"  - Talep {order.get('requested_delivery', '?')} -> teyit "
            f"{order.get('confirmed_delivery', '?')}{marker}"
        )
    return _lines(*out) + _meta_note(payload)


def _render_health(payload: dict[str, Any]) -> str:
    sap = payload.get("sap") or {}
    guard = payload.get("guardrails") or {}
    actor = payload.get("actor") or {}
    return _lines(
        f"**SAP baglantisi: {sap.get('status', '?')}** "
        f"(backend `{sap.get('backend', '?')}`, sistem {payload.get('system_alias', '?')})",
        f"- Gecikme: {payload.get('latency_ms', '?')} ms",
        f"- Yazma modu: {'simulasyon (dry-run)' if guard.get('dry_run') else 'GERCEK YAZMA'}"
        f" | onay esigi: {_num(guard.get('approval_threshold'), 2)} {guard.get('currency', '')}",
        f"- Egress allowlist: {', '.join(guard.get('egress_allowlist') or ['tanimlanmadi'])}",
        f"- API kimlik dogrulama: {guard.get('api_auth_mode', '?')}",
        f"- Kullanici: {actor.get('subject', '?')} ({', '.join(actor.get('roles') or [])})",
    ) + _meta_note(payload)


def _render_supplier_scores(payload: dict[str, Any]) -> str:
    rows = payload.get("vendors") or payload.get("suppliers") or []
    if not rows:
        return ""
    out = [
        "**Tedarikci degerlendirme skorlari** "
        f"(satinalma org. {payload.get('purchasing_org', '?')})"
    ]
    for row in rows:
        estimated = row.get("estimated_fields") or []
        out.append(
            f"- {row.get('vendor_id', '')} {row.get('vendor_name', '')}: "
            f"genel {row.get('overall_score', '-')}, teslim {row.get('delivery_score', '-')}, "
            f"kalite {row.get('quality_score', '-')}"
            + (f" (olculemeyen alan: {', '.join(estimated)})" if estimated else "")
        )
    return _lines(*out) + _meta_note(payload)


def _render_capabilities(payload: dict[str, Any]) -> str:
    services = payload.get("service_manifest") or payload.get("services") or []
    supported = (payload.get("backend_capabilities") or {}).get("supported") or {}
    if not services and not supported:
        return ""
    out = [
        f"**SAP yetenek envanteri** (sistem {payload.get('system_alias', '?')}, "
        f"backend `{payload.get('backend', '?')}`)"
    ]
    if supported:
        available = sorted(k for k, v in supported.items() if v)
        missing = sorted(k for k, v in supported.items() if not v)
        out.append(f"\n- Desteklenen: {', '.join(available) or 'yok'}")
        if missing:
            out.append(f"- Desteklenmeyen: {', '.join(missing)}")
    if services:
        out.append("\nServisler:")
        for svc in services[:40]:
            note = ""
            if svc.get("available") is False:
                note = " - ERISILEMIYOR"
            elif svc.get("contract_ok") is False:
                note = " - SOZLESME FARKLI"
            service = svc.get("service") or svc.get("purpose") or ""
            out.append(
                f"- `{svc.get('alias', '')}` ({svc.get('odata', '')}, "
                f"{svc.get('status', '')}){note}"
                + (f" - {service}" if service else "")
            )
    if payload.get("preferred_order"):
        out.append(f"\nTercih sirasi: {payload['preferred_order']}")
    return _lines(*out) + _meta_note(payload)


def _render_purchase_order_360(payload: dict[str, Any]) -> str:
    """Tek siparisin 360 ozeti.

    Bu tool'un ciktisi zaten deterministik olarak hesaplanmis: gecikme,
    GR/IR farki, bloke faturalar ve `interpretation` metni kodda uretiliyor.
    Modelin katkisi bicimlendirmeden ibaret oldugu icin sonucun ikinci kez
    LLM'e gonderilmesi hem gecikme hem gizlilik maliyeti demekti.
    """
    po_id = payload.get("po_id")
    if not po_id or payload.get("error"):
        return ""
    currency = payload.get("currency", "")
    out = [
        f"**Satinalma siparisi {po_id}** "
        f"({payload.get('item_count', 0)} kalem, "
        f"{payload.get('open_item_count', 0)} acik)",
        f"- Toplam {_num(payload.get('total_value'), 2)} {currency}"
        f" | acik {_num(payload.get('open_value'), 2)} {currency}"
        f" | teslim %{payload.get('delivered_pct', 0)}",
        f"- Mal kabul: {payload.get('goods_receipt_count', 0)} kayit"
        f" | fatura: {payload.get('invoice_count', 0)}",
    ]
    if payload.get("gr_ir_gap_qty"):
        out.append(
            f"- GR/IR farki: {_num(payload.get('gr_ir_gap_qty'))} adet"
            + (
                f" ({_num(payload.get('gr_ir_gap_value'), 2)} {currency})"
                if payload.get("gr_ir_gap_value")
                else ""
            )
        )
    if payload.get("max_delay_days"):
        out.append(f"- En buyuk oteleme: {payload['max_delay_days']} gun")
    if payload.get("blocked_invoices"):
        out.append(f"- Bloke fatura: {', '.join(payload['blocked_invoices'])}")
    for warning in payload.get("warnings") or []:
        out.append(f"- Dikkat: {warning}")
    if payload.get("interpretation"):
        out.append(f"\n{payload['interpretation']}")
    return _lines(*out) + _meta_note(payload)


def _render_supplier_invoice_status(payload: dict[str, Any]) -> str:
    """Tek PO veya tek fatura durumunu LLM kotasindan bagimsiz cevapla."""
    query = payload.get("query") or {}
    if not isinstance(query, dict):
        return ""

    invoice_id = query.get("invoice_id")
    if invoice_id:
        rows = payload.get("invoices") or []
        if not rows:
            return (
                f"**Tedarikci faturasi {invoice_id} bulunamadi.**\n"
                f"{payload.get('interpretation', '')}"
            ).strip() + _meta_note(payload)
        row = rows[0]
        out = [
            f"**Tedarikci faturasi {row.get('invoice_id', invoice_id)}** "
            f"(durum: {row.get('status', '?')})",
            f"- Tedarikci: {row.get('vendor_id', '?')} {row.get('vendor_name', '')}",
            f"- Tutar: {_num(row.get('gross_amount'), 2)} {row.get('currency', '')}",
            f"- Kayit: {row.get('posting_date', '?')} | vade: {row.get('due_date', '?')}",
        ]
        if row.get("payment_block"):
            out.append(
                f"- Odeme blokaji: {row['payment_block']}"
                + (
                    f" ({', '.join(row.get('block_reasons') or [])})"
                    if row.get("block_reasons")
                    else ""
                )
            )
        if row.get("days_overdue"):
            out.append(f"- Vadesi {row['days_overdue']} gun gecmis.")
        if row.get("po_ids"):
            out.append(f"- Siparisler: {', '.join(row['po_ids'])}")
        if payload.get("interpretation"):
            out.append(f"\n{payload['interpretation']}")
        return _lines(*out) + _meta_note(payload)

    if not query.get("po_id"):
        return ""

    issued = bool(payload.get("invoice_issued"))
    parked_count = int(payload.get("parked_count") or 0)
    if issued:
        heading = "**Evet - bu siparis icin aktif fatura var.**"
    elif parked_count:
        heading = "**Henuz degil - fatura park halinde, muhasebelesmemis.**"
    else:
        heading = "**Hayir - bu siparis icin aktif fatura bulunamadi.**"

    out = [
        heading,
        f"- Aktif fatura: {payload.get('invoice_count', 0)}"
        f" | park: {parked_count}"
        f" | iptal: {payload.get('cancelled_count', 0)}",
    ]
    if payload.get("blocked_count"):
        out.append(f"- Odeme blokajli: {payload['blocked_count']}")
    if payload.get("overdue_count"):
        out.append(f"- Vadesi gecmis aktif fatura: {payload['overdue_count']}")
    totals = payload.get("total_gross_by_currency") or {}
    if totals:
        out.append(
            "- Aktif fatura toplami: "
            + " + ".join(f"{_num(amount, 2)} {currency}" for currency, amount in totals.items())
        )
    if payload.get("interpretation"):
        out.append(f"\n{payload['interpretation']}")
    return _lines(*out) + _meta_note(payload)


def _render_document_flow(payload: dict[str, Any]) -> str:
    """Belge zinciri: PR -> PO -> mal kabul -> fatura -> odeme."""
    stages = payload.get("stages") or []
    if not stages:
        return ""
    out = [
        f"**Belge akisi: {payload.get('document_id', '')}** "
        f"({payload.get('resolved_type', 'bilinmiyor')})"
    ]
    for stage in stages:
        documents = stage.get("documents") or []
        shown = ", ".join(documents[:5])
        if len(documents) > 5:
            shown += f" (+{len(documents) - 5})"
        out.append(f"- {stage.get('stage', stage.get('type', ''))}: {stage.get('count', 0)} - {shown}")
    out.append(
        "\nZincir "
        + ("odemeye kadar tamamlanmis." if payload.get("chain_complete") else "henuz tamamlanmamis.")
    )
    if payload.get("interpretation"):
        out.append(payload["interpretation"])
    return _lines(*out) + _meta_note(payload)


def _render_invoice_block(payload: dict[str, Any]) -> str:
    """Fatura blokaj aciklamasi.

    Bu tool cevabi ZATEN uretiyor: `interpretation` ve `next_steps` kodda,
    deterministik olarak hesaplaniyor. Modele geri gondermek yalnizca ayni
    metni yeniden yazdirmakti.
    """
    if not payload.get("invoice_id") or payload.get("error"):
        return ""
    if payload.get("blocked") is False:
        return _lines(
            f"**Fatura {payload['invoice_id']}** bloke degil "
            f"(durum: {payload.get('status', '?')})."
        ) + _meta_note(payload)

    out = [
        f"**Fatura {payload['invoice_id']} bloke** "
        f"({_num(payload.get('gross_amount'), 2)} {payload.get('currency', '')}, "
        f"tedarikci {payload.get('vendor_id', '?')})",
        f"- Blokaj sayisi: {payload.get('block_count', 0)}"
        f" | nedenler: {', '.join(payload.get('block_reasons') or ['bilinmiyor'])}",
    ]
    if payload.get("payment_block"):
        out.append(f"- Odeme blokaj anahtari: {payload['payment_block']}")
    for finding in (payload.get("findings") or [])[:6]:
        variance = finding.get("variance_pct")
        out.append(
            f"- Kalem {finding.get('item_no', '')}: {finding.get('reason', '')}"
            + (f" (sapma %{variance})" if variance is not None else "")
            + (f"\n  {finding['resolution']}" if finding.get("resolution") else "")
        )
    if payload.get("interpretation"):
        out.append(f"\n{payload['interpretation']}")
    for step in (payload.get("next_steps") or [])[:4]:
        out.append(f"- Sonraki adim: {step}")
    return _lines(*out) + _meta_note(payload)


#: Modeli atlayabilecek tool'lar. Bu liste bir GUVENLIK KONTROLUDUR: burada
#: olmayan hicbir tool sonucu dogrudan kullaniciya donmez.
DIRECT_ANSWER_TOOLS: dict[str, DirectAnswerSpec] = {
    "sap_stock_overview": DirectAnswerSpec(
        render=_render_stock, sufficient=_stock_is_sufficient
    ),
    "sap_material_360": DirectAnswerSpec(render=_render_material),
    "sap_search_materials": DirectAnswerSpec(render=_render_material_search),
    "sap_track_purchase_orders": DirectAnswerSpec(
        render=_render_purchase_orders,
        # "MAT-1 acik siparisleri" kisayolu icin yerel liste yeterlidir.
        # Modelin sectigi genel liste ise tedarikci risk karsilastirmasi gibi
        # daha genis bir isin yalniz ilk adimi olabilir; erken bitirilmez.
        allow_self_contained=False,
    ),
    "sap_connection_health": DirectAnswerSpec(render=_render_health),
    "sap_supplier_score_360": DirectAnswerSpec(render=_render_supplier_scores),
    "sap_discover_capabilities": DirectAnswerSpec(render=_render_capabilities),
    # Bu ucu de cevabi ZATEN kodda uretiyor (`interpretation`, `next_steps`,
    # hesaplanmis gecikme/sapma). Sonucu modele geri gondermek ayni metni
    # yeniden yazdirmakti: bir LLM round-trip'i ve tum SAP payload'inin
    # ikinci kez saglayiciya cikmasi. Ikisi de gereksiz.
    "sap_purchase_order_360": DirectAnswerSpec(render=_render_purchase_order_360),
    "sap_supplier_invoice_status": DirectAnswerSpec(
        render=_render_supplier_invoice_status,
        # Tek PO ve tek fatura kesin filtrelerdir. Tedarikci/liste sorgulari
        # daha genis yorum isteyebilecegi icin model yolunda kalir.
        sufficient=lambda p: bool(
            (p.get("query") or {}).get("po_id")
            or (p.get("query") or {}).get("invoice_id")
        ),
    ),
    "sap_document_flow": DirectAnswerSpec(render=_render_document_flow),
    "sap_invoice_block_explain": DirectAnswerSpec(
        render=_render_invoice_block,
        # Bloke OLMAYAN fatura icin kisayol yeterlidir; bloke olan ve hicbir
        # kalem bulgusu cikmayan durumda model devreye girsin: neden
        # okunamadiginda kullaniciya yol gostermek yorum ister.
        sufficient=lambda p: bool(p.get("blocked") is False or p.get("findings")),
    ),
}


# --- Niyet kisayollari (LLM cagrilmadan) -------------------------------------
#: SAP malzeme/belge numarasi: buyuk harf, rakam, tire, alt cizgi, egik cizgi.
#: API Hub gibi sistemlerde ``21`` gibi iki karakterli malzeme kimlikleri de
#: vardir. Onceki alt sinir uc karakterdi; bu nedenle gecerli bir SAP kimligi
#: daha regex'e girmeden modele dusuyor ve model kotasina bagimli kaliyordu.
_CODE = r"[A-Z0-9][A-Z0-9\-_/\.]{1,39}"


def _code(match: re.Match[str], *groups: str) -> str:
    """Alternatif regex gruplarindan ilk SAP kimligini normallestir."""
    for group in groups:
        value = match.groupdict().get(group)
        if value:
            return value.upper()
    return ""


@dataclass(frozen=True)
class IntentShortcut:
    """Deterministik soru -> tool eslemesi.

    `pattern` mesajin TAMAMIYLA eslesmek zorundadir (`fullmatch`). Kismi
    eslesme kabul edilseydi "stok durumu nedir, ayrica alternatif tedarikci
    oner" gibi bir soru yalniz ilk yarisiyla cevaplanirdi.
    """

    name: str
    tool: str
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str]], dict[str, Any]] = field(
        default=lambda _m: {}  # noqa: ARG005
    )
    description: str = ""


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


SHORTCUTS: tuple[IntentShortcut, ...] = (
    IntentShortcut(
        name="stock",
        tool="sap_stock_overview",
        pattern=_rx(
            rf"(?:stok|stock)\s*(?:durumu|seviyesi|level)?\s*[:\-]?\s*(?P<code>{_CODE})\s*\??"
            rf"|(?P<code2>{_CODE})\s*(?:icin|için|of)?\s*(?:stok|stock)"
            r"\s*(?:durumu|seviyesi|level|nedir|ne kadar)?\s*\??"
            rf"|(?P<code3>{_CODE})\s*(?:numarali|numaralı|nolu|no'lu)?\s*"
            r"(?:malzemenin|malzeme|materialin|material)\s*"
            r"(?:stok|stock)\s*(?:durumu|durumunu|seviyesi|seviyesini)?\s*"
            r"(?:goster|göster|getir|nedir|ne kadar)?\s*\??"
        ),
        build=lambda m: {"material_ids": [_code(m, "code", "code2", "code3")]},
        description="Tek malzemenin stok fotografi",
    ),
    IntentShortcut(
        name="material_search",
        tool="sap_search_materials",
        pattern=_rx(
            rf"(?P<query>{_CODE})\s+ile\s+(?:baslayan|başlayan)\s+"
            r"(?:malzemeleri|malzemeler|materials?)\s*"
            r"(?:ara|bul|listele|goster|göster|getir)\s*\??"
            rf"|(?P<query2>{_CODE})\s+(?:malzemelerini|malzemeleri|materials?)\s*"
            r"(?:ara|bul|listele|goster|göster|getir)\s*\??"
        ),
        build=lambda m: {"query": _code(m, "query", "query2")},
        description="Kod/aciklama metniyle malzeme ara",
    ),
    IntentShortcut(
        name="material",
        tool="sap_material_360",
        pattern=_rx(
            rf"(?:malzeme|material)\s*(?:bilgisi|karti|kartı|360|detayi|detayı)?\s*[:\-]?\s*"
            rf"(?P<code>{_CODE})\s*\??"
            rf"|(?P<code2>{_CODE})\s*(?:malzeme|material)\s*(?:bilgisi|karti|kartı|360)\s*\??"
            rf"|(?P<code3>{_CODE})\s*(?:numarali|numaralı|nolu|no'lu)?\s*"
            r"(?:malzemenin|malzeme|materialin|material)\s*"
            r"(?:detayi|detayı|detaylari|detayları|detaylarini|detaylarını|"
            r"bilgisi|bilgilerini|karti|kartı|360)\s*"
            r"(?:goster|göster|getir|listele|nedir)?\s*\??"
        ),
        build=lambda m: {"material_id": _code(m, "code", "code2", "code3")},
        description="Malzeme ana verisi ozeti",
    ),
    IntentShortcut(
        name="purchase_order_invoice_status",
        tool="sap_supplier_invoice_status",
        pattern=_rx(
            rf"(?P<code>{_CODE})\s*(?:numarali|numaralı)?\s*"
            r"(?:(?:satinalma|satın\s+alma)\s+)?"
            r"(?:siparisinin|siparişinin|siparisin|siparişin|siparis|sipariş|po(?:['’]nun)?)\s+"
            r"(?:faturasi|faturası)\s+"
            r"(?:kesildi\s+mi|kesilmis\s+mi|kesilmiş\s+mi|var\s+mi|var\s+mı|durumu(?:\s+nedir)?)\s*\??"
        ),
        build=lambda m: {"po_id": _code(m, "code")},
        description="Tek siparis icin aktif fatura var/yok durumu",
    ),
    IntentShortcut(
        name="supplier_invoice_status",
        tool="sap_supplier_invoice_status",
        pattern=_rx(
            rf"(?P<code>{_CODE})\s*(?:numarali|numaralı|nolu|no'lu)?\s*"
            r"(?:tedarikci|tedarikçi|supplier)?\s*"
            r"(?:faturasinin|faturasının|fatura|invoice)\s*"
            r"(?:durumu|durumunu|statusu|statüsü|status)\s*"
            r"(?:nedir|goster|göster|getir)?\s*\??"
        ),
        build=lambda m: {"invoice_id": _code(m, "code")},
        description="Tek tedarikci faturasinin durumu",
    ),
    IntentShortcut(
        name="purchase_order_status",
        tool="sap_purchase_order_360",
        pattern=_rx(
            rf"(?P<code>{_CODE})\s*(?:numarali|numaralı|nolu|no'lu)?\s*"
            r"(?:(?:satinalma|satın\s+alma)\s+)?"
            r"(?:siparisinin|siparişinin|siparisin|siparişin|siparis|sipariş|po(?:['’]nun)?)\s*"
            r"(?:durumu|detayi|detayı|detaylari|detayları|ozeti|özeti|360|nerede)\s*"
            r"(?:nedir|goster|göster|getir)?\s*\??"
        ),
        build=lambda m: {"po_id": _code(m, "code")},
        description="Tek satinalma siparisinin 360 durumu",
    ),
    IntentShortcut(
        name="purchase_orders",
        tool="sap_track_purchase_orders",
        pattern=_rx(
            rf"(?:acik|açık|open)?\s*(?:siparis|sipariş|po)\s*(?:durumu|takibi|listesi|status)?"
            rf"\s*[:\-]?\s*(?P<code>{_CODE})\s*\??"
        ),
        build=lambda m: {"material_id": _code(m, "code"), "only_open": True},
        description="Bir malzemenin acik siparisleri",
    ),
    IntentShortcut(
        name="health",
        tool="sap_connection_health",
        pattern=_rx(
            r"(?:sap\s*)?(?:baglantisi|bağlantısı|baglanti|bağlantı|connection)\s*"
            r"(?:durumu|sagligi|sağlığı|saglik|sağlık|saglikli|sağlıklı|"
            r"health|test|kontrol)?\s*(?:mi|mı|mu|mü|nedir)?\s*\??"
            r"|(?:sistem|system)\s*(?:durumu|health|status)\s*\??"
            r"|health\s*check\s*\??"
        ),
        description="SAP baglanti ve guvenlik duruşu",
    ),
    IntentShortcut(
        name="capabilities",
        tool="sap_discover_capabilities",
        pattern=_rx(
            r"(?:hangi\s+)?(?:sap\s*)?(?:sisteminin\s+)?"
            r"(?:destekledigi|desteklediği|sundugu|sunduğu|aktif\s+olan\s+)?\s*"
            r"(?:yetenek|yetenekler|yetenekleri|capabilities?|servis|servisler|servisleri)"
            r"(?:\s+ve\s+(?:yetenek|yetenekler|yetenekleri|servis|servisler|servisleri))?\s*"
            r"(?:listesi|envanteri|var|nedir|listele|goster|göster|getir)?\s*\??"
        ),
        description="Hedef sistemde hangi SAP servisleri kullanilabilir",
    ),
)


@dataclass(frozen=True)
class ShortcutMatch:
    shortcut: IntentShortcut
    arguments: dict[str, Any]

    @property
    def tool(self) -> str:
        return self.shortcut.tool


def match_shortcut(message: str) -> ShortcutMatch | None:
    """Mesaj deterministik olarak tek bir salt-okunur tool'a esleniyor mu?

    Eslesme yoksa `None` doner ve normal LLM akisi calisir. Bu fonksiyon
    **asla** tahmin etmez: `fullmatch` disinda hicbir sey kisayol saymaz.
    """
    text = " ".join((message or "").split())
    # Kullanicilar test listesinden kopyalarken "1." / "2)" gibi sira
    # numarasini da mesaja dahil edebiliyor. Bu sunum on eki niyetin parcasi
    # degildir; guvenli tek-soru eslesmesinden once atilir.
    text = re.sub(r"^(?:\d{1,3}[\.)]\s+|[-*]\s+)", "", text)
    # Kullanici cumleyi nokta/unlemle bitirdiginde niyet degismez. Regex'lerin
    # kimlik grubunun son noktayi SAP kodunun parcasi sanmasini da engeller.
    text = text.rstrip(" .,!;:")
    if not text or len(text) > 120:
        # Uzun mesaj = birden fazla istek olma ihtimali yuksek; modele birak.
        return None
    for shortcut in SHORTCUTS:
        found = shortcut.pattern.fullmatch(text)
        if found is None:
            continue
        try:
            arguments = shortcut.build(found)
        except Exception:  # noqa: BLE001 - kisayol hatasi akisi kesmemeli
            log.debug("Kisayol argumani uretilemedi: %s", shortcut.name)
            return None
        spec = DIRECT_ANSWER_TOOLS.get(shortcut.tool)
        if spec is None or not spec.allow_shortcut:
            return None
        return ShortcutMatch(shortcut=shortcut, arguments=arguments)
    return None


def direct_answer_for(
    tool: str, payload: dict[str, Any], *, reason: str
) -> DirectAnswer | None:
    """Tool sonucunu deterministik metne cevirir; uygun degilse `None`.

    Reddedilme sebepleri (hepsi fail-open, yani model devreye girer):
      - tool allowlist'te degil,
      - sonuc hata veya `needs_review` tasiyor,
      - modelin sectigi tool "bu sonuc tek basina yeterli degil" diyor,
      - renderer bos metin uretti veya patladi.
    """
    spec = DIRECT_ANSWER_TOOLS.get(tool)
    if spec is None:
        return None
    if reason == "self_contained" and not spec.allow_self_contained:
        return None
    if not isinstance(payload, dict) or "error" in payload:
        return None
    if payload.get("needs_review") or payload.get("denial_code"):
        return None
    try:
        # Kesin shortcut kaliplari kullanicinin yalnizca ham durumu
        # istedigini kanitlar. Ornegin "21 ... stok durumunu goster" eksik
        # stok dondurse bile yerel renderer soruyu eksiksiz cevaplar; alternatif
        # tedarikci istenmedikce modele ihtiyac yoktur. `self_contained`
        # yolunda ise model daha genis bir istegi yurutuyor olabilir, bu
        # nedenle tool'un yeterlilik kosulu aynen korunur.
        if reason != "shortcut" and not spec.sufficient(payload):
            return None
        text = spec.render(payload).strip()
    except Exception:  # noqa: BLE001 - render hatasi modele dusmeyi engellemez
        log.warning("Dogrudan yanit uretilemedi (%s); model akisina dusuluyor", tool)
        return None
    if not text:
        return None
    return DirectAnswer(text=text, tool=tool, reason=reason)


def shortcut_catalogue() -> list[dict[str, str]]:
    """Teshis: hangi sorular modeli hic cagirmadan cevaplanabilir."""
    return [
        {"name": s.name, "tool": s.tool, "description": s.description}
        for s in SHORTCUTS
    ]
