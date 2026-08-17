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


def _render_purchase_orders(payload: dict[str, Any]) -> str:
    orders = payload.get("orders") or []
    if not orders:
        return ""
    currency = payload.get("currency", "")
    out = [
        f"**Acik satinalma siparisleri** ({payload.get('order_count', len(orders))} adet, "
        f"toplam {_num(payload.get('total_open_value'), 2)} {currency})"
    ]
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


def _render_project_cost(payload: dict[str, Any]) -> str:
    rows = payload.get("wbs_elements") or payload.get("projects") or []
    if not rows:
        return ""
    currency = payload.get("currency", "")
    out = [f"**Proje maliyet durumu** ({payload.get('as_of', '')})"]
    for row in rows[:25]:
        out.append(
            f"- {row.get('wbs_element', '')} {row.get('description', '')}: "
            f"plan {_num(row.get('plan_cost'), 2)}, fiili {_num(row.get('actual_cost'), 2)}, "
            f"taahhut {_num(row.get('commitment'), 2)} {currency}"
        )
    alerts = payload.get("alerts") or []
    if alerts:
        out.append("\nUyarilar:")
        out.extend(f"- {a if isinstance(a, str) else a.get('message', a)}" for a in alerts[:10])
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


#: Modeli atlayabilecek tool'lar. Bu liste bir GUVENLIK KONTROLUDUR: burada
#: olmayan hicbir tool sonucu dogrudan kullaniciya donmez.
DIRECT_ANSWER_TOOLS: dict[str, DirectAnswerSpec] = {
    "sap_stock_overview": DirectAnswerSpec(
        render=_render_stock, sufficient=_stock_is_sufficient
    ),
    "sap_material_360": DirectAnswerSpec(render=_render_material),
    "sap_track_purchase_orders": DirectAnswerSpec(render=_render_purchase_orders),
    "sap_connection_health": DirectAnswerSpec(render=_render_health),
    "sap_supplier_score_360": DirectAnswerSpec(render=_render_supplier_scores),
    "sap_project_cost_status": DirectAnswerSpec(render=_render_project_cost),
    "sap_discover_capabilities": DirectAnswerSpec(render=_render_capabilities),
}


# --- Niyet kisayollari (LLM cagrilmadan) -------------------------------------
#: SAP malzeme/belge numarasi: buyuk harf, rakam, tire, alt cizgi, egik cizgi.
_CODE = r"[A-Z0-9][A-Z0-9\-_/\.]{2,39}"


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
        ),
        build=lambda m: {"material_ids": [(m.group("code") or m.group("code2")).upper()]},
        description="Tek malzemenin stok fotografi",
    ),
    IntentShortcut(
        name="material",
        tool="sap_material_360",
        pattern=_rx(
            rf"(?:malzeme|material)\s*(?:bilgisi|karti|kartı|360|detayi|detayı)?\s*[:\-]?\s*"
            rf"(?P<code>{_CODE})\s*\??"
            rf"|(?P<code2>{_CODE})\s*(?:malzeme|material)\s*(?:bilgisi|karti|kartı|360)\s*\??"
        ),
        build=lambda m: {"material_id": (m.group("code") or m.group("code2")).upper()},
        description="Malzeme ana verisi ozeti",
    ),
    IntentShortcut(
        name="purchase_orders",
        tool="sap_track_purchase_orders",
        pattern=_rx(
            rf"(?:acik|açık|open)?\s*(?:siparis|sipariş|po)\s*(?:durumu|takibi|listesi|status)?"
            rf"\s*[:\-]?\s*(?P<code>{_CODE})\s*\??"
        ),
        build=lambda m: {"material_id": m.group("code").upper(), "only_open": True},
        description="Bir malzemenin acik siparisleri",
    ),
    IntentShortcut(
        name="health",
        tool="sap_connection_health",
        pattern=_rx(
            r"(?:sap\s*)?(?:baglanti|bağlantı|connection)\s*"
            r"(?:durumu|saglik|sağlık|health|test|kontrol)?\s*\??"
            r"|(?:sistem|system)\s*(?:durumu|health|status)\s*\??"
            r"|health\s*check\s*\??"
        ),
        description="SAP baglanti ve guvenlik duruşu",
    ),
    IntentShortcut(
        name="capabilities",
        tool="sap_discover_capabilities",
        pattern=_rx(
            r"(?:hangi\s+)?(?:sap\s*)?(?:yetenek|yetenekler|capabilities?|servisler?)\s*"
            r"(?:listesi|envanteri|var|nedir)?\s*\??"
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
      - tool "bu sonuc tek basina yeterli degil" diyor,
      - renderer bos metin uretti veya patladi.
    """
    spec = DIRECT_ANSWER_TOOLS.get(tool)
    if spec is None:
        return None
    if not isinstance(payload, dict) or "error" in payload:
        return None
    if payload.get("needs_review") or payload.get("denial_code"):
        return None
    try:
        if not spec.sufficient(payload):
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
