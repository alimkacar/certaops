"""Platform, capability kesfi ve guvenlik tool'lari.

Bu tool'lar bootstrap pack'tedir: her turda modele gorunur ve kucuktur.
Amac, modelin "hangi yetenek var, ne oldu, ne dogrulandi" sorularini SAP'a
gereksiz cagri yapmadan ve tahmin uretmeden cevaplayabilmesi.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..adapters.sap import (
    CAPABILITY_MANIFEST,
    SAPError,
    explain_authorization_failure,
    manifest_summary,
    parse_sap_error,
)
from ..contracts import (
    DETAIL_SCHEMA,
    SCOPE_AUDIT_READ,
    SCOPE_PLATFORM_READ,
    SCOPE_SAP_READ,
    SCOPE_SAP_SIMULATE,
    EvidenceAccessDenied,
    RiskTier,
    page_limit,
    resolve_detail,
)
from ..core import agent_catalogue, plan_agents
from .registry import ToolContext, tool


# ---------------------------------------------------------------------------
@tool(
    name="sap_discover_capabilities",
    group="platform",
    domain="platform",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_PLATFORM_READ,),
    result_token_budget=900,
    description=(
        "Bagli SAP sisteminde hangi servislerin aktif oldugunu, OData surumunu, entity/alan "
        "kontratinin beklentiyle uyusup uyusmadigini ve deprecated API durumunu cikarir. "
        "Bir is akisina baslamadan once 'bu sistem bunu yapabilir mi' sorusunu cevaplar. "
        "probe=false ile yalniz yerel manifest dondurulur (SAP'a cagri yapilmaz)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "aliases": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Kontrol edilecek servis alias'lari (product, availability, mrp, purchase_requisition ...). Bos = tumu.",
            },
            "probe": {
                "type": "boolean",
                "description": "true ise hedef sistemin $metadata belgesi okunur ve kontrat dogrulanir.",
                "default": False,
            },
        },
        "required": [],
    },
)
def sap_discover_capabilities(
    ctx: ToolContext,
    aliases: list[str] | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    selected = [a for a in (aliases or []) if a in CAPABILITY_MANIFEST]
    backend_caps = ctx.sap.capabilities()

    # Alias verilmediginde manifest kompakt dondurulur: butun entity set ve doc
    # listesini her cagrida tasimak sema butcesini bosa harcar.
    entries = manifest_summary(selected or None)
    if not selected:
        entries = [
            {
                "alias": entry["alias"],
                "odata": entry["odata"],
                "status": entry["status"],
                "purpose": entry["purpose"][:70],
            }
            for entry in entries
        ]

    payload: dict[str, Any] = {
        "system_alias": ctx.settings.sap.system_alias,
        "backend": backend_caps["backend"],
        "backend_capabilities": backend_caps["supported"],
        "service_manifest": entries,
        "detail_hint": "Bir servisin entity set/alan detayi icin aliases=[...] verin.",
        "preferred_order": "released OData V4 -> released OData V2/SOAP -> released custom (Tier 2)",
    }
    if "connection" in backend_caps:
        payload["connection"] = backend_caps["connection"]

    unsupported = [k for k, v in backend_caps["supported"].items() if not v]
    if unsupported:
        payload["unsupported_capabilities"] = unsupported
        payload["note"] = (
            "Desteklenmeyen yetenekler icin ilgili tool acik hata dondurur; tahmini veri uretilmez."
        )

    if probe:
        prober = getattr(ctx.sap, "probe_capabilities", None)
        if prober is None:
            payload["probe"] = {
                "skipped": True,
                "reason": f"{backend_caps['backend']} backend'i $metadata probe desteklemiyor.",
            }
        else:
            checks = prober(selected or None)
            payload["probe"] = checks
            broken = [c for c in checks if not c.get("contract_ok")]
            if broken:
                payload["contract_gaps"] = [c["alias"] for c in broken]
                payload["remediation"] = (
                    "Kontrat farki olan servisler icin communication arrangement/sürüm "
                    "kontrolu yapilmali. Alan farklari duzeltilmeden ilgili tool'a guvenilmemeli."
                )
    return payload


# ---------------------------------------------------------------------------
@tool(
    name="sap_connection_health",
    group="platform",
    domain="diagnostics",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_PLATFORM_READ,),
    result_token_budget=500,
    description=(
        "SAP baglantisinin sagligini teshis eder: erisilebilirlik, kimlik dogrulama modu, "
        "gecikme ve yapilandirma uyarilari. Guvenlik durumu (dry-run, yazma penceresi, "
        "onay esigi) ile birlikte dondurur. Hata durumunda yetki/baglanti ayrimi yapar."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def sap_connection_health(ctx: ToolContext) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        ping = ctx.sap.ping()
        error: dict[str, Any] | None = None
    except SAPError as exc:
        ping = {"backend": ctx.sap.name, "status": "error"}
        error = exc.as_dict()

    latency_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
    security = ctx.settings.security
    payload: dict[str, Any] = {
        "sap": ping,
        "latency_ms": latency_ms,
        "system_alias": ctx.settings.sap.system_alias,
        "guardrails": {
            "dry_run": ctx.settings.sap.dry_run,
            "approval_threshold": ctx.settings.sap.approval_threshold,
            "currency": ctx.settings.sap.currency,
            "write_window": security.write_window or "sinirsiz",
            "egress_allowlist": list(security.allowed_sap_hosts) or ["tanimlanmadi"],
            "api_auth_mode": security.auth_mode,
        },
        "actor": ctx.actor.to_dict(include_scopes=True) if ctx.actor else None,
    }
    if error:
        payload["error"] = error
    if not security.allowed_sap_hosts:
        payload.setdefault("warnings", []).append(
            "SAP_ALLOWED_HOSTS bos: giden trafik allowlist'i tanimli degil."
        )
    if security.auth_mode == "none":
        payload.setdefault("warnings", []).append(
            "AGENT_AUTH_MODE=none: API kimlik dogrulamasi kapali, uretimde kullanilmamali."
        )
    return payload


# ---------------------------------------------------------------------------
@tool(
    name="sap_explain_authorization_failure",
    group="platform",
    domain="diagnostics",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_PLATFORM_READ,),
    result_token_budget=500,
    description=(
        "Bir SAP 401/403 hatasini rol ve eksik is yetkisi diline cevirir. Hangi yetki "
        "nesnesinin (M_BANF_EKG, M_MATE_WRK vb.) eksik olabilecegini ve nasil talep "
        "edilecegini soyler. YETKI VERMEZ, yalnizca tarif eder."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "http_status": {"type": "integer", "description": "401 veya 403."},
            "sap_message": {"type": "string", "description": "SAP'in dondurdugu hata mesaji."},
            "target_api": {"type": "string", "description": "Cagrilan servis yolu veya alias."},
        },
        "required": ["http_status"],
    },
)
def sap_explain_authorization_failure(
    ctx: ToolContext,
    http_status: int,
    sap_message: str = "",
    target_api: str = "",
) -> dict[str, Any]:
    if http_status not in {401, 403}:
        return {
            "error": "Bu tool yalnizca 401/403 yetkilendirme hatalari icin kullanilir.",
            "received_status": http_status,
        }
    fault = parse_sap_error(
        status_code=http_status,
        body="",
        headers={},
        target_api=target_api or "bilinmiyor",
    )
    fault = type(fault)(
        http_status=fault.http_status,
        code=fault.code,
        message=sap_message or fault.message,
        target_api=fault.target_api,
        correlation_id=ctx.execution.correlation_id if ctx.execution else "",
    )
    return explain_authorization_failure(fault).to_dict()


# ---------------------------------------------------------------------------
@tool(
    name="sap_validate_change",
    group="platform",
    domain="diagnostics",
    risk_tier=RiskTier.R1,
    required_scopes=(SCOPE_SAP_SIMULATE,),
    # Taslagi gercek SAP varsayilanlariyla kurar.
    org_scoped=True,
    result_token_budget=800,
    description=(
        "Mutating bir payload'i YAZMADAN once dogrular: zorunlu alanlar, tarih tutarliligi, "
        "miktar/birim, tutar ve organizasyon kapsami. Yazma tool'unu cagirmadan hangi "
        "bulgularin engelleyici oldugunu gosterir. Sonuc 'onay' degildir; yalnizca on kontroldur."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["purchase_requisition"],
                "description": "Dogrulanacak islem tipi.",
            },
            "payload": {
                "type": "object",
                "description": "Islem govdesi. purchase_requisition icin {items:[...], header_text:...}",
            },
        },
        "required": ["operation", "payload"],
    },
)
def sap_validate_change(
    ctx: ToolContext,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if operation != "purchase_requisition":
        return {"error": f"Desteklenmeyen islem tipi: {operation}", "supported": ["purchase_requisition"]}

    from .procurement import build_pr_items  # dairesel import olmasin diye burada

    items = payload.get("items") or []
    if not items:
        return {"error": "payload.items bos.", "blocking": True}

    try:
        pr_items = build_pr_items(ctx, items)
        draft = ctx.sap.prepare_purchase_requisition(
            pr_items,
            header_text=str(payload.get("header_text", "")),
            purchase_group=payload.get("purchase_group"),
        )
    except SAPError as exc:
        return {"valid": False, "blocking": True, **exc.as_dict()}

    return {
        "operation": operation,
        "valid": draft.is_submittable,
        "total_value": draft.total_value,
        "currency": draft.currency,
        "requires_human_approval": draft.requires_human_approval,
        "findings": [f.model_dump() for f in draft.findings],
        "blocking_findings": [f.message for f in draft.blocking_findings],
        "source_api": draft.source_api,
        "note": "Bu bir simulasyondur; SAP'a hicbir sey yazilmadi.",
    }


# ---------------------------------------------------------------------------
# Audit kaydinin ozetinde korunacak alanlar. Genel butce kirpicisina birakilamaz:
# kirpici listenin BASINI korur, oysa denetimde en yeni kayitlar kritiktir.
_AUDIT_SUMMARY_FIELDS = (
    "seq",
    "recorded_at",
    "event",
    "tool",
    "risk_tier",
    "outcome",
    "execution_id",
    "correlation_id",
    "approval_id",
    "idempotency_key",
    "payload_sha256",
    "duration_ms",
)


def _compact_audit_entry(row: dict[str, Any], *, full: bool) -> dict[str, Any]:
    if full:
        return row
    compact = {k: row[k] for k in _AUDIT_SUMMARY_FIELDS if row.get(k) is not None}
    actor = row.get("actor") or {}
    if actor:
        compact["actor"] = actor.get("subject")
    policy = row.get("policy") or {}
    if policy.get("denial_code"):
        compact["denial_code"] = policy["denial_code"]
    if row.get("detail", {}).get("error"):
        compact["error"] = row["detail"]["error"]
    return compact


@tool(
    name="sap_get_execution_audit",
    group="platform",
    domain="platform",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_AUDIT_READ,),
    result_token_budget=1600,
    description=(
        "Bir agent isleminin plan, policy karari, onay, SAP cagrisi ve verification zincirini "
        "audit defterinden getirir. Hash zinciri dogrulamasi da yapilabilir. Kayitlar "
        "EN YENIDEN eskiye dogru dondurulur ve ozet alanlar korunur; detay icin detail='full'. "
        "Denetim ve olay sonrasi inceleme icin kullanilir."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "execution_id": {"type": "string", "description": "Islem kimligi. Bos ise mevcut islem."},
            "correlation_id": {"type": "string", "description": "Alternatif olarak correlation ID."},
            "limit": {"type": "integer", "description": "Kac kayit dondurulsun (en yeniler).", "default": 15},
            "detail": DETAIL_SCHEMA,
            "verify_chain": {"type": "boolean", "description": "Hash zincirini dogrula.", "default": False},
        },
        "required": [],
    },
)
def sap_get_execution_audit(
    ctx: ToolContext,
    execution_id: str = "",
    correlation_id: str = "",
    limit: int = 15,
    detail: str = "standard",
    verify_chain: bool = False,
) -> dict[str, Any]:
    assert ctx.audit and ctx.execution and ctx.actor
    target = execution_id or (ctx.execution.execution_id if not correlation_id else "")
    level = resolve_detail(detail)

    if correlation_id:
        entries = ctx.audit.by_correlation(correlation_id)
    elif target:
        entries = ctx.audit.chain(target)
    else:
        entries = ctx.audit.recent(limit=max(limit, 50), tenant=ctx.actor.tenant)

    # Tenant sinirini asan kayitlar dondurulmez.
    entries = [
        e for e in entries if not e.get("actor") or e["actor"].get("tenant") == ctx.actor.tenant
    ]
    total = len(entries)
    page_size = page_limit(level, limit, default=15)
    # En yeni kayitlar onceliklidir: kirpma bastan degil sondan yapilir.
    selected = entries[-page_size:]
    compacted = [_compact_audit_entry(row, full=level == "full") for row in selected]
    # Ozette kronoloji tersten okunur ki ilk satir en son olay olsun.
    compacted.reverse()

    payload: dict[str, Any] = {
        "execution_id": target or None,
        "correlation_id": correlation_id or None,
        "entry_count": total,
        "returned": len(compacted),
        "order": "en yeniden eskiye",
        "entries": compacted,
    }
    if total > len(compacted):
        payload["older_entries_omitted"] = total - len(compacted)
        payload["hint"] = "Daha eski kayitlar icin limit degerini artirin."
    if verify_chain:
        payload["chain_verification"] = ctx.audit.verify()
    return payload


# ---------------------------------------------------------------------------
@tool(
    name="sap_reconcile_execution",
    group="platform",
    domain="platform",
    risk_tier=RiskTier.R1,
    required_scopes=(SCOPE_SAP_READ,),
    result_token_budget=700,
    description=(
        "Timeout veya kesinti sonrasi bir yazma isleminin SAP'ta gercekten olusup olusmadigini "
        "business key/idempotency anahtari uzerinden kontrol eder. TEKRAR POST ETMEZ. "
        "Duplicate belge riskini kaldirmanin dogru yolu budur: once oku, sonra karar ver."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "idempotency_key": {"type": "string", "description": "Yazma cagrisinda kullanilan anahtar."},
            "list_pending": {
                "type": "boolean",
                "description": "true ise mutabakat bekleyen tum kayitlar listelenir.",
                "default": False,
            },
        },
        "required": [],
    },
)
def sap_reconcile_execution(
    ctx: ToolContext,
    idempotency_key: str = "",
    list_pending: bool = False,
) -> dict[str, Any]:
    assert ctx.idempotency and ctx.actor
    tenant = ctx.actor.tenant

    if list_pending or not idempotency_key:
        pending = ctx.idempotency.pending(tenant=tenant)
        return {
            "pending_count": len(pending),
            "pending": [r.to_dict() for r in pending],
            "hint": "Her kayit icin idempotency_key vererek mutabakat calistirin.",
        }

    record = ctx.idempotency.get(idempotency_key, tenant=tenant)
    if record is None:
        return {
            "idempotency_key": idempotency_key,
            "status": "not_found",
            "conclusion": "Bu anahtarla baslatilmis bir yazma yok; islem hic denenmemis.",
        }

    payload: dict[str, Any] = {"idempotency_key": idempotency_key, **record.to_dict()}

    if record.is_completed:
        payload["conclusion"] = (
            f"Islem tamamlanmis: {record.business_object_id}. Yeni belge olusturmayin."
        )
        payload["safe_to_retry"] = False
        return payload

    finder = getattr(ctx.sap, "find_purchase_requisition_by_reference", None)
    if finder is None:
        payload["conclusion"] = "Backend referans aramayi desteklemiyor; manuel kontrol gerekiyor."
        return payload

    try:
        found = finder(idempotency_key)
    except SAPError as exc:
        payload["conclusion"] = "SAP'ta arama yapilamadi."
        payload["error"] = exc.as_dict()
        return payload

    if found is None:
        payload["conclusion"] = (
            "SAP'ta bu referansla belge yok. Ayni idempotency_key ile kontrollu tekrar guvenli."
        )
        payload["safe_to_retry"] = True
        return payload

    object_id, record_body = found
    ctx.idempotency.complete(
        idempotency_key, tenant=tenant, business_object_id=object_id, result=record_body
    )
    payload["conclusion"] = (
        f"Belge SAP'ta bulundu ve mutabakat yapildi: {object_id}. Tekrar gondermeyin."
    )
    payload["business_object_id"] = object_id
    payload["safe_to_retry"] = False
    payload["sap_record"] = record_body
    return payload


# ---------------------------------------------------------------------------
@tool(
    name="get_evidence",
    group="platform",
    domain="platform",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_PLATFORM_READ,),
    result_token_budget=2500,
    description=(
        "Bir tool sonucunun butce nedeniyle konusmaya sigmayan tam kaydini evidence "
        "store'dan getirir. evidence_id yalniz kendi tenant'iniz icin gecerlidir. "
        "Sonuc buyukse yine sayfali okunmali."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "evidence_id": {"type": "string", "description": "Tool sonucundaki _meta.evidence_id."},
            "path": {
                "type": "string",
                "description": "Isteniyorsa payload icinde tek bir anahtar (or. 'materials').",
            },
        },
        "required": ["evidence_id"],
    },
)
def get_evidence(ctx: ToolContext, evidence_id: str, path: str = "") -> dict[str, Any]:
    assert ctx.evidence and ctx.actor
    try:
        record = ctx.evidence.get(evidence_id, actor=ctx.actor)
    except EvidenceAccessDenied:
        return {
            "error": "Bu evidence kaydina erisim yetkiniz yok.",
            "denial_code": "EVIDENCE_TENANT_MISMATCH",
        }
    except KeyError:
        return {
            "error": f"Evidence bulunamadi veya suresi gecti: {evidence_id}",
            "denial_code": "EVIDENCE_NOT_FOUND",
        }

    if path:
        payload = record.get("payload") or {}
        if isinstance(payload, dict) and path in payload:
            record = {**record, "payload": {path: payload[path]}}
        else:
            record = {
                **record,
                "payload": {},
                "note": f"'{path}' anahtari kayitta yok.",
            }
    return record


# ---------------------------------------------------------------------------
@tool(
    name="sap_list_agents",
    group="platform",
    domain="platform",
    risk_tier=RiskTier.R0,
    required_scopes=(SCOPE_PLATFORM_READ,),
    result_token_budget=700,
    description=(
        "SAP multi-agent sistemindeki domain agent'larini ve sabit sorumluluk sinirlarini "
        "listeler. Istege bagli bir kullanici mesaji verilirse deterministik orkestratorun "
        "hangi agent zincirini sececegini onizler. Bu tool yetki veya tool kapsami genisletmez."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Istege bagli olarak agent planlamasi onizlenecek kullanici mesaji.",
            },
        },
        "required": [],
    },
)
def sap_list_agents(ctx: ToolContext, message: str = "") -> dict[str, Any]:
    assert ctx.actor
    payload: dict[str, Any] = {
        "architecture": "orchestrator + isolated SAP domain agents",
        "handoff_schema": "sap-agent-handoff/v1",
        "agents": agent_catalogue(),
        "note": "Agent sinirlari sabittir; bir domain agent kendi tool kapsamlarini genisletemez.",
    }
    if message.strip():
        payload["plan_preview"] = plan_agents(message, ctx.actor).to_dict()
    return payload
