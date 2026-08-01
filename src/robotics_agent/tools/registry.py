"""Tool kayit defteri, risk sozlesmesi ve calistirma katmani.

`ToolSpec` yalnizca sema tasimaz. Her tool risk seviyesi, gereken yetki
kapsamlari, onay politikasi, idempotency davranisi, timeout ve sonuc token
butcesi bildirir. `execute_tool` handler'i cagirmadan once policy gate'ten
gecer; karar audit'e yazilir.

Fail-closed: policy karari uretilemiyorsa tool calismaz. Prompt icerigi bu
davranisi degistiremez.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..adapters.bpa import ApprovalGateway, build_approval_gateway
from ..cache import (
    NO_CACHE,
    CachePolicy,
    SecureCache,
    build_cache_key,
    entry_for,
    get_tool_cache,
)
from ..config import Settings, get_settings
from ..contracts import (
    SCOPE_SAP_READ,
    ActorContext,
    BaseEvidenceStore,
    Evidence,
    EvidenceStore,
    ExecutionContext,
    RiskTier,
    SQLiteEvidenceStore,
    ToolResult,
    enforce_result_budget,
    estimate_tokens,
)
from ..core import (
    ApprovalStore,
    AuditLedger,
    IdempotencyStore,
    OrgDefaults,
    PolicyDecision,
    PolicyDecisionPoint,
    WriteGuard,
    get_audit_ledger,
    get_state_db,
    sha256_of,
)
from ..observability import ToolInvocationMetric, TurnMetrics
from ..privacy import DataClass, DataPolicy, DLPEngine, build_dlp_engine
from ..risk import ImpactProfile, MutationKind, Reversibility
from ..sap import SAPBackend, SAPError, get_backend

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Tool'larin paylastigi calisma baglami.

    Guvenlik bilesenleri (policy, audit, onay, idempotency, evidence) baglama
    dahildir: bir tool bunlari atlamak icin ayri bir yol bulamaz.
    """

    settings: Settings = field(default_factory=get_settings)
    sap: SAPBackend = field(default_factory=get_backend)
    # Uretilen dosyalarin yollari
    artifacts: list[str] = field(default_factory=list)

    actor: ActorContext | None = None
    execution: ExecutionContext | None = None
    policy: PolicyDecisionPoint | None = None
    audit: AuditLedger | None = None
    approvals: ApprovalStore | None = None
    approval_gateway: ApprovalGateway | None = None
    idempotency: IdempotencyStore | None = None
    evidence: BaseEvidenceStore | None = None
    metrics: TurnMetrics | None = None
    # Policy karari, calisan tool'a yukumlulukleri gormesi icin verilir.
    decision: PolicyDecision | None = None
    # Audit'e yazilan model/prompt surumu.
    model: str = ""
    prompt_version: str = ""
    # --- Veri politikasi ve guvenli cache bilesenleri -----------------------
    dlp: DLPEngine | None = None
    cache: SecureCache | None = None
    # `detail=full` istegi icin gereken isleme amaci.
    purpose: str = ""
    # SAP adapter cagri sayaci: performans butcesi contract testi bunu okur.
    sap_call_count: int = 0

    def __post_init__(self) -> None:
        if self.actor is None:
            self.actor = ActorContext.local_operator(
                subject=self.settings.agent.local_subject,
                tenant=self.settings.sap.tenant,
                roles=self.settings.agent.local_roles,
            )
        if self.execution is None:
            self.execution = ExecutionContext(
                actor=self.actor,
                system_alias=self.settings.sap.system_alias,
                dry_run=self.settings.sap.dry_run,
            )
        db = get_state_db(self.settings.state.db_path)
        if self.approvals is None:
            self.approvals = ApprovalStore(
                db, default_ttl_minutes=self.settings.security.approval_ttl_minutes
            )
        if self.approval_gateway is None:
            self.approval_gateway = build_approval_gateway(self.settings, self.approvals)
        if self.idempotency is None:
            self.idempotency = IdempotencyStore(db)
        if self.audit is None:
            self.audit = get_audit_ledger(
                self.settings.state.db_path,
                mirror_path=(
                    self.settings.state.audit_mirror_path
                    if self.settings.state.audit_mirror_enabled
                    else None
                ),
            )
        if self.evidence is None:
            # Worker'lar arasi paylasim gerektiginde SQLite; bellek deposu
            # yalniz memory session backend'inde (test/tek surec) kullanilir.
            evidence_kwargs = {
                "ttl_minutes": self.settings.state.evidence_ttl_minutes,
                "max_entries": self.settings.state.evidence_max_entries,
            }
            self.evidence = (
                EvidenceStore(**evidence_kwargs)
                if self.settings.state.session_backend == "memory"
                else SQLiteEvidenceStore(db, **evidence_kwargs)
            )
        if self.policy is None:
            self.policy = PolicyDecisionPoint(
                approvals=self.approvals,
                write_window=self.settings.security.write_window,
                forced_dry_run=self.settings.sap.dry_run,
                approval_threshold=self.settings.sap.approval_threshold,
                org_defaults=OrgDefaults.from_settings(self.settings),
                risk_mode=self.settings.risk.scoring_mode,
            )
        if self.dlp is None:
            self.dlp = build_dlp_engine(self.settings)
        if self.cache is None:
            self.cache = get_tool_cache(self.settings)

    # --- Kolaylik yardimcilari ---------------------------------------------
    def write_guard(self) -> WriteGuard:
        assert self.audit and self.idempotency and self.execution  # __post_init__ garantisi
        return WriteGuard(
            audit=self.audit,
            idempotency=self.idempotency,
            approvals=self.approvals,
            execution=self.execution,
        )

    def store_evidence(self, payload: Any, *, tool: str, evidence: Evidence) -> str:
        assert self.evidence and self.actor
        return self.evidence.put(payload, actor=self.actor, tool=tool, evidence=evidence)

    def sap_evidence(
        self,
        source_api: str,
        *,
        record_count: int = 0,
        business_object: str = "",
        etag: str = "",
        estimated_fields: tuple[str, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> Evidence:
        return Evidence(
            source_system=self.settings.sap.system_alias,
            source_api=source_api,
            business_object=business_object,
            etag=etag,
            record_count=record_count,
            estimated_fields=estimated_fields,
            correlation_id=self.execution.correlation_id if self.execution else "",
            notes=notes,
        )


@dataclass(frozen=True)
class PerformanceBudget:
    """Tool'un CI tarafindan dogrulanan olculebilir performans sozlesmesi.

    Bu degerler CI kapisinin olctugu esiklerdir: bir tool bildirdigi SAP cagri
    sayisini asarsa contract testi kirmizi doner. "Yavas ama calisiyor" bir
    tool, bildirmedigi surece kabul edilmez.
    """

    p95_ms: int = 4000
    max_sap_calls: int = 3
    max_records: int = 200
    max_result_tokens: int = 1200

    def to_dict(self) -> dict[str, Any]:
        return {
            "p95_ms": self.p95_ms,
            "max_sap_calls": self.max_sap_calls,
            "max_records": self.max_records,
            "max_result_tokens": self.max_result_tokens,
        }


# Bildirilen risk seviyesinden turetilen varsayilan etki profili.
# Acikca bildirmeyen eski tool'lar da runtime skorlamaya girer; varsayilan
# **muhafazakardir**: yazma seviyesi bir tool otomatik olarak `write` sayilir.
_DEFAULT_PROFILES: dict[RiskTier, ImpactProfile] = {
    RiskTier.R0: ImpactProfile(mutation=MutationKind.READ, reversible=Reversibility.EASY),
    RiskTier.R1: ImpactProfile(mutation=MutationKind.COMPUTE, reversible=Reversibility.EASY),
    RiskTier.R2: ImpactProfile(mutation=MutationKind.DRAFT, reversible=Reversibility.EASY),
    RiskTier.R3: ImpactProfile(
        mutation=MutationKind.WRITE, reversible=Reversibility.COMPENSATING
    ),
    RiskTier.R4: ImpactProfile(
        mutation=MutationKind.BULK_WRITE, reversible=Reversibility.IRREVERSIBLE
    ),
}

# Eski tek degerli `data_classification` etiketinin D0-D3 karsiligi.
_LEGACY_CLASS_MAP: dict[str, DataClass] = {
    "public": DataClass.D0,
    "internal": DataClass.D1,
    "confidential": DataClass.D2,
    "restricted": DataClass.D3,
}


@dataclass(frozen=True)
class ToolSpec:
    """Bir tool'un tam sozlesmesi."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]]
    group: str = "genel"
    # Iki asamali routing icin domain etiketi (bkz. core.router.PACKS)
    domain: str = "platform"
    version: str = "1.0.0"
    risk_tier: RiskTier = RiskTier.R0
    required_scopes: tuple[str, ...] = ()
    # none | threshold | always | dual
    approval_policy: str = "none"
    approve_scope: str = "sap.pr.approve"
    idempotent: bool = False
    timeout_s: float = 60.0
    # Eski tek degerli etiket; `data_policy` bildirilmediginde ona cevrilir.
    data_classification: str = "internal"
    result_token_budget: int = 1200
    # None -> SAP okuma/yazma kapsami isteyen tool'lar icin otomatik True.
    # Tool, arguman verilmediginde sistem varsayilani tesis/sirket kodunu
    # kullaniyorsa policy bu varsayilanlari da actor kapsamina karsi denetler.
    org_scoped: bool | None = None

    # --- Veri, etki, cache ve performans sozlesmeleri -----------------------
    data_policy: DataPolicy | None = None
    impact_profile: ImpactProfile | None = None
    cache_policy: CachePolicy = NO_CACHE
    performance_budget: PerformanceBudget | None = None

    def __post_init__(self) -> None:
        # Bildirilmeyen sozlesmeler guvenli varsayilanlara duser. `frozen=True`
        # oldugu icin object.__setattr__ kullanilir.
        if self.data_policy is None:
            legacy = _LEGACY_CLASS_MAP.get(self.data_classification, DataClass.D1)
            object.__setattr__(self, "data_policy", DataPolicy(default_class=legacy))
        if self.impact_profile is None:
            object.__setattr__(
                self, "impact_profile", _DEFAULT_PROFILES.get(self.risk_tier, ImpactProfile())
            )
        if self.performance_budget is None:
            object.__setattr__(
                self,
                "performance_budget",
                PerformanceBudget(max_result_tokens=self.result_token_budget),
            )

    # Geriye donuk uyumluluk: eski kod `writes` alanini okuyor.
    @property
    def writes(self) -> bool:
        return self.risk_tier.is_mutating

    @property
    def max_data_class(self) -> DataClass:
        assert self.data_policy is not None  # __post_init__ garantisi
        return self.data_policy.max_declared_class

    @property
    def applies_org_defaults(self) -> bool:
        """Bu tool sistem varsayilani organizasyon degerlerine dokunuyor mu?

        Acikca bildirilmemisse SAP verisine erisen (sap.read / *.write kapsami
        isteyen) her tool org-scoped kabul edilir. Fail-closed varsayilan:
        yanlislikla atlanan bir tool kapsam kontrolunden kacamaz.
        """
        if self.org_scoped is not None:
            return self.org_scoped
        return any(
            scope == SCOPE_SAP_READ or scope.endswith(".write")
            for scope in self.required_scopes
        )

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_contract(self) -> dict[str, Any]:
        assert self.data_policy and self.impact_profile and self.performance_budget
        return {
            "tool": self.name,
            "version": self.version,
            "domain": self.domain,
            "risk_tier": self.risk_tier.value,
            "risk_label": self.risk_tier.label,
            "required_scopes": list(self.required_scopes),
            "approval_policy": self.approval_policy,
            "idempotent": self.idempotent,
            "timeout_s": self.timeout_s,
            "data_classification": self.data_classification,
            "result_token_budget": self.result_token_budget,
            "org_scoped": self.applies_org_defaults,
            # Veri, etki, cache ve performans sozlesmeleri.
            # `max_data_class` bir **tavandir**: tool'un dondurebilecegi en
            # yuksek sinif. Tek bir cagrinin fiilen tasidigi sinif daha dusuk
            # olabilir ve cache karari o gercek sinifa gore verilir.
            "max_data_class": self.max_data_class.value,
            "data_policy": self.data_policy.to_dict(),
            "impact_profile": self.impact_profile.to_dict(),
            "cache_policy": self.cache_policy.to_dict(),
            "performance_budget": self.performance_budget.to_dict(),
        }


REGISTRY: dict[str, ToolSpec] = {}

_VALID_APPROVAL_POLICIES = {"none", "threshold", "always", "dual"}


def tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    group: str = "genel",
    domain: str = "platform",
    risk_tier: RiskTier = RiskTier.R0,
    required_scopes: tuple[str, ...] = (),
    approval_policy: str = "none",
    approve_scope: str = "sap.pr.approve",
    idempotent: bool = False,
    timeout_s: float = 60.0,
    version: str = "1.0.0",
    data_classification: str = "internal",
    result_token_budget: int = 1200,
    org_scoped: bool | None = None,
    data_policy: DataPolicy | None = None,
    impact_profile: ImpactProfile | None = None,
    cache_policy: CachePolicy = NO_CACHE,
    performance_budget: PerformanceBudget | None = None,
) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    """Bir fonksiyonu Claude tool'u olarak kaydeder.

    Risk sozlesmesi zorunludur: R3/R4 bir tool onay politikasi ve yazma kapsami
    bildirmek zorundadir; aksi halde kayit sirasinda hata verir.

    Dort tamamlayici sozlesme de kayit sirasinda denetlenir:
      `data_policy`        alan bazli D0-D3 siniflandirmasi
      `impact_profile`     runtime etki skorunun statik girdisi
      `cache_policy`       TTL, anahtar boyutlari ve sinif tavani
      `performance_budget` p95, SAP cagri sayisi ve token sinirlari

    Bunlar bildirilmezse guvenli varsayilanlar uygulanir; **tutarsiz**
    bildirilirse kayit sirasinda hata verilir (or. bir R3 tool'un kendini
    `mutation='read'` ilan etmesi).
    """

    def decorator(func: Callable[..., dict]) -> Callable[..., dict]:
        if name in REGISTRY:
            raise ValueError(f"Tool adi zaten kayitli: {name}")
        if approval_policy not in _VALID_APPROVAL_POLICIES:
            raise ValueError(
                f"{name}: approval_policy '{approval_policy}' gecersiz "
                f"({', '.join(sorted(_VALID_APPROVAL_POLICIES))})."
            )
        if risk_tier.requires_approval:
            if approval_policy == "none":
                raise ValueError(
                    f"{name}: {risk_tier.value} bir tool approval_policy='none' olamaz."
                )
            if not required_scopes:
                raise ValueError(f"{name}: mutating tool required_scopes bildirmek zorunda.")
            if not idempotent:
                raise ValueError(
                    f"{name}: mutating tool idempotent=True ve idempotency_key destegi ister."
                )
        if risk_tier is RiskTier.R4 and approval_policy != "dual":
            raise ValueError(f"{name}: R4 tool cift onay (approval_policy='dual') gerektirir.")

        if impact_profile is not None:
            problems = impact_profile.validate(risk_tier_level=risk_tier.level)
            if problems:
                raise ValueError(f"{name}: " + "; ".join(problems))
        if data_policy is not None:
            problems = data_policy.validate()
            if problems:
                raise ValueError(f"{name}: " + "; ".join(problems))
        if cache_policy.enabled and risk_tier.is_mutating:
            raise ValueError(
                f"{name}: mutating tool cache'lenemez (cache_policy.ttl_seconds > 0)."
            )
        if cache_policy.enabled and cache_policy.max_class is DataClass.D3:
            raise ValueError(f"{name}: D3 veri cache'lenemez.")

        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
            group=group,
            domain=domain,
            version=version,
            risk_tier=risk_tier,
            required_scopes=tuple(required_scopes),
            approval_policy=approval_policy,
            approve_scope=approve_scope,
            idempotent=idempotent,
            timeout_s=timeout_s,
            data_classification=data_classification,
            result_token_budget=result_token_budget,
            org_scoped=org_scoped,
            data_policy=data_policy,
            impact_profile=impact_profile,
            cache_policy=cache_policy,
            performance_budget=performance_budget,
        )
        return func

    return decorator


def anthropic_tool_definitions(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Modele gonderilecek sema listesi. `names` verilmezse tum tool'lar."""
    if names is None:
        return [spec.to_anthropic() for spec in REGISTRY.values()]
    return [REGISTRY[n].to_anthropic() for n in names if n in REGISTRY]


def visible_tool_names(domains: frozenset[str], actor: ActorContext) -> list[str]:
    """Domain pack'e giren **ve** actor'un yetkili oldugu tool adlari.

    Kullanicinin yetkisi olmayan mutating tool modele hic gosterilmez. Bu hem
    token maliyetini hem saldiri yuzeyini azaltir.
    """
    out: list[str] = []
    for spec in REGISTRY.values():
        if spec.domain not in domains:
            continue
        if spec.required_scopes and actor.missing_scopes(spec.required_scopes):
            continue
        out.append(spec.name)
    return out


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def strip_empty(value: Any) -> Any:
    """null ve bos metin alanlarini ayiklar.

    JSON'da "field": null veya "field": "" hicbir bilgi tasimaz ama token harcar.
    Bos LISTELER bilerek korunur: "shortages": [] modele "kontrol edildi, eksik yok"
    bilgisini verir; alanin hic olmamasi ise "hesaplanmadi" anlamina gelebilir.
    Sayisal sifir ve False de korunur.
    """
    if isinstance(value, dict):
        cleaned = {k: strip_empty(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v is not None and v != ""}
    if isinstance(value, list):
        return [strip_empty(v) for v in value if v is not None]
    return value


def _serialize(result: Any) -> dict[str, Any]:
    """Tool sonucunu JSON-guvenli sozluge cevirir."""
    payload = result.to_payload() if isinstance(result, ToolResult) else result
    return json.loads(json.dumps(payload, default=_json_default))


class ToolTimeout(RuntimeError):
    """Tool sozlesmesindeki `timeout_s` asildi."""


def _run_with_timeout(
    spec: ToolSpec, arguments: dict[str, Any], ctx: ToolContext
) -> Any:
    """Handler'i sozlesmesinde bildirilen timeout siniri altinda calistirir.

    Python'da calisan bir thread guvenli sekilde oldurulemez; bu nedenle
    timeout **cagiriciyi serbest birakir**, arka plandaki is bitene kadar
    surer. Bu, sonsuz bekleyen bir SAP cagrisinin turu kilitlemesini engeller;
    gercek iptal icin adapter seviyesinde HTTP timeout'lari (SAP_TIMEOUT)
    birincil savunmadir.

    Mutating tool'larda timeout, yazmanin yapilmadigi anlamina GELMEZ: sonuc
    `needs_review` olarak isaretlenir ve mutabakat yolu gosterilir.
    """
    timeout = float(spec.timeout_s or 0)
    if timeout <= 0:
        return spec.handler(ctx=ctx, **arguments)

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = spec.handler(ctx=ctx, **arguments)
        except BaseException as exc:  # noqa: BLE001 - ana thread'e tasinir
            box["error"] = exc

    worker = threading.Thread(
        target=_target, name=f"tool-{spec.name}", daemon=True
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise ToolTimeout(
            f"'{spec.name}' {timeout:g} saniyelik sozlesme limitini asti."
        )
    if "error" in box:
        raise box["error"]
    return box.get("result")


def execute_tool(
    name: str, arguments: dict[str, Any], ctx: ToolContext
) -> tuple[str, bool]:
    """Tool'u policy gate arkasinda calistirir. (json_sonuc, hata_mi) doner.

    Yurutme boru hatti:

        policy gate -> cache lookup -> handler -> alan politikasi + DLP
        -> token butcesi -> cache store -> audit

    DLP **butceden once** calisir: kirpma sirasinda hangi alanin dustugune
    bakilmaksizin, modele giden her alan gizlilik kararindan gecmis olur.
    """
    spec = REGISTRY.get(name)
    started = time.perf_counter()
    assert ctx.execution and ctx.policy and ctx.audit  # ToolContext.__post_init__

    # --- 1. Policy gate (handler'dan once, her zaman) ----------------------
    decision = ctx.policy.evaluate(spec, arguments, ctx.execution, tool_name=name)
    ctx.execution.record_tool(name)
    ctx.audit.append(
        "tool.policy_decision",
        execution=ctx.execution,
        tool=name,
        risk_tier=decision.risk_tier.value,
        outcome=decision.outcome.value,
        policy=decision.to_dict(),
        payload_sha256=sha256_of(arguments),
        approval_id=decision.approval_id,
        model=ctx.model,
        prompt_version=ctx.prompt_version,
    )

    if not decision.allowed or spec is None:
        payload = json.dumps(decision.as_error(), ensure_ascii=False)
        _record_metric(
            ctx, spec, name, outcome="denied", started=started, result=payload,
            denial_code=decision.denial_code,
        )
        log.warning("tool reddedildi | %s | %s", name, decision.denial_code)
        return payload, True

    ctx.decision = decision

    # --- 2. Cache (yalniz salt okunur tool'lar) ----------------------------
    cache_key = _cache_key_for(spec, arguments, ctx)
    if cache_key is not None and ctx.cache is not None:
        hit = ctx.cache.get(cache_key)
        if hit is not None:
            payload_dict = dict(hit.payload)
            meta = payload_dict.setdefault("_meta", {})
            # Model verinin tazeligini gormeden termin/tutar taahhudu vermemeli.
            meta.update(hit.freshness())
            payload = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":"))
            _record_metric(
                ctx, spec, name, outcome="ok", started=started, result=payload,
                detail=str(arguments.get("detail", "standard")), cache_hit=True,
            )
            log.info("tool ok  | %-32s | cache hit (%.0fs)", name, hit.age_seconds)
            ctx.decision = None
            return payload, False

    # --- 3. Handler (sozlesmedeki timeout siniri altinda) ------------------
    try:
        result = _run_with_timeout(spec, arguments, ctx)
    except ToolTimeout as exc:
        mutating = spec.risk_tier.is_mutating
        body: dict[str, Any] = {
            "error": str(exc),
            "denial_code": "TOOL_TIMEOUT",
            "timeout_s": spec.timeout_s,
        }
        if mutating:
            # Yazma tool'unda timeout "yazilmadi" demek degildir.
            body["needs_review"] = True
            body["remediation"] = (
                "Tekrar gondermeyin. sap_reconcile_execution ile ayni idempotency_key "
                "uzerinden islemin SAP'ta olusup olusmadigini dogrulayin."
            )
        payload = json.dumps(body, ensure_ascii=False)
        ctx.audit.append(
            "tool.timeout",
            execution=ctx.execution,
            tool=name,
            risk_tier=spec.risk_tier.value,
            outcome="needs_review" if mutating else "error",
            detail={"timeout_s": spec.timeout_s},
        )
        _record_metric(ctx, spec, name, outcome="error", started=started, result=payload)
        log.warning("tool timeout | %s | %.1fs", name, spec.timeout_s)
        return payload, True
    except SAPError as exc:
        payload = json.dumps(exc.as_dict(), ensure_ascii=False)
        ctx.audit.append(
            "tool.sap_error",
            execution=ctx.execution,
            tool=name,
            risk_tier=spec.risk_tier.value,
            outcome="error",
            sap_messages=[str(exc)],
            detail={"sap_code": exc.code},
        )
        _record_metric(ctx, spec, name, outcome="error", started=started, result=payload)
        log.warning("tool SAP hatasi | %s | %s", name, exc)
        return payload, True
    except TypeError as exc:
        payload = json.dumps(
            {"error": f"Parametre hatasi: {exc}", "expected_schema": spec.input_schema},
            ensure_ascii=False,
        )
        _record_metric(ctx, spec, name, outcome="error", started=started, result=payload)
        log.warning("tool parametre hatasi | %s | %s", name, exc)
        return payload, True
    except Exception as exc:  # noqa: BLE001 - model hatayi gorup duzeltebilmeli
        payload = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        ctx.audit.append(
            "tool.failed",
            execution=ctx.execution,
            tool=name,
            risk_tier=spec.risk_tier.value,
            outcome="error",
            detail={"error": f"{type(exc).__name__}: {exc}"},
        )
        _record_metric(ctx, spec, name, outcome="error", started=started, result=payload)
        log.exception("tool hatasi | %s", name)
        return payload, True
    finally:
        ctx.decision = None

    # --- 4. Alan politikasi ve DLP (modele verilmeden ONCE) -----------------
    serialized = strip_empty(_serialize(result))
    dlp_summary: dict[str, Any] | None = None
    # Sonucta fiilen gorulen en yuksek veri sinifi; cache karari buna bakar.
    observed_class = spec.max_data_class
    if isinstance(serialized, dict) and ctx.dlp is not None and ctx.actor is not None:
        assert spec.data_policy is not None  # ToolSpec.__post_init__
        dlp_result = ctx.dlp.apply(
            serialized,
            actor=ctx.actor,
            sink="model",
            policy=spec.data_policy,
            detail=str(arguments.get("detail", "standard")),
            purpose=ctx.purpose,
        )
        dlp_summary = dlp_result.summary()
        if dlp_result.denied:
            body = {
                "error": dlp_result.denied_reason,
                "denial_code": "DATA_POLICY_DENIED",
                "tool": name,
                "remediation": (
                    "Gereken veri erisim kapsami ve purpose_code olmadan bu alanlar "
                    "dondurulemez. Yetkili bir kullaniciyla veya daha dar bir detay "
                    "seviyesiyle tekrar deneyin."
                ),
            }
            payload = json.dumps(body, ensure_ascii=False)
            ctx.audit.append(
                "tool.data_policy_denied",
                execution=ctx.execution,
                tool=name,
                risk_tier=decision.risk_tier.value,
                outcome="denied",
                detail={"dlp": dlp_summary},
            )
            _record_metric(
                ctx, spec, name, outcome="denied", started=started, result=payload,
                denial_code="DATA_POLICY_DENIED",
            )
            log.warning("tool veri politikasi reddi | %s", name)
            return payload, True
        serialized = dlp_result.payload
        observed_class = dlp_result.observed_max_class

    is_error = isinstance(serialized, dict) and "error" in serialized

    # --- 5. Sonuc butcesi ve evidence ---------------------------------------
    evidence_id = None
    outcome = enforce_result_budget(
        serialized if isinstance(serialized, dict) else {"result": serialized},
        max_tokens=spec.result_token_budget,
    )
    if outcome.trimmed and ctx.evidence is not None:
        # Tam payload konusmaya girmez; erisim kontrollu store'a tasinir.
        evidence_id = ctx.store_evidence(
            serialized,
            tool=name,
            evidence=ctx.sap_evidence(
                source_api=f"tool:{name}",
                record_count=outcome.dropped_items,
                notes=("Butce nedeniyle kirpildi; tam kayit evidence store'da.",),
            ),
        )
        outcome = enforce_result_budget(
            serialized if isinstance(serialized, dict) else {"result": serialized},
            max_tokens=spec.result_token_budget,
            evidence_id=evidence_id,
        )

    payload = json.dumps(outcome.payload, ensure_ascii=False, separators=(",", ":"))
    duration_ms = (time.perf_counter() - started) * 1000

    # --- 6. Cache'e yaz (hatasiz, salt okunur, sinifi uygun sonuc) ----------
    cacheable = (
        cache_key is not None
        and ctx.cache is not None
        and not is_error
        and not outcome.trimmed
        and spec.cache_policy.allows(observed_class)
    )
    if cacheable:
        ctx.cache.set(
            cache_key,
            entry_for(
                outcome.payload,
                tool=name,
                data_class=observed_class,
                ttl_seconds=spec.cache_policy.ttl_seconds,
                tags=_cache_tags(spec, arguments),
            ),
        )

    ctx.audit.append(
        "tool.completed",
        execution=ctx.execution,
        tool=name,
        risk_tier=decision.risk_tier.value,
        outcome="error" if is_error else "ok",
        detail={
            "result_tokens": outcome.final_tokens,
            "trimmed": outcome.trimmed,
            "evidence_id": evidence_id,
            "sap_calls": ctx.sap_call_count,
            "data_class": spec.max_data_class.value,
            "dlp": dlp_summary,
        },
        duration_ms=duration_ms,
        model=ctx.model,
        prompt_version=ctx.prompt_version,
    )
    _record_metric(
        ctx, spec, name, outcome="error" if is_error else "ok", started=started, result=payload,
        trimmed=outcome.trimmed, dropped_items=outcome.dropped_items,
        detail=str(arguments.get("detail", "standard")),
        dlp_findings=(dlp_summary or {}).get("findings", 0),
    )
    log.info(
        "tool %s | %-32s | %6.0f ms | %5d tok%s",
        "err" if is_error else "ok ",
        name,
        duration_ms,
        outcome.final_tokens,
        " (kirpildi)" if outcome.trimmed else "",
    )
    return payload, is_error


def _cache_key_for(spec: ToolSpec, arguments: dict[str, Any], ctx: ToolContext):
    """Bu cagri cache'lenebilir mi? Evetse anahtari uretir.

    Cache **yalnizca** su kosullarin tamaminda acilir:
      - tool `cache_policy.ttl_seconds > 0` bildirmis,
      - tool mutating degil,
      - sonucun veri sinifi tool'un bildirdigi tavani asmiyor (D3 asla),
      - global cache acik.
    """
    policy: CachePolicy = spec.cache_policy
    if not policy.enabled or spec.risk_tier.is_mutating:
        return None
    if ctx.cache is None or not ctx.cache.enabled or ctx.actor is None:
        return None
    # Sinif kapisi burada degil **yazma aninda** uygulanir: tool'un bildirdigi
    # tavan, sonucun gercekten tasidigi sinifla ayni degildir. Bir tool
    # `supplier_iban: D3` bildirebilir ama donen kayitta o alan hic bulunmayabilir;
    # tavana bakip cache'i tumden kapatmak, dogru siniflandirmayi cezalandirirdi.
    cfg = ctx.settings.sap
    return build_cache_key(
        tool=spec.name,
        tool_version=spec.version,
        actor=ctx.actor,
        system_alias=cfg.system_alias,
        arguments=arguments,
        detail=str(arguments.get("detail", "standard")),
        policy=policy,
        org_defaults={
            "company_code": cfg.company_code,
            "plant": cfg.plant,
            "purchasing_org": cfg.purch_org,
        },
    )


def _cache_tags(spec: ToolSpec, arguments: dict[str, Any]) -> frozenset[str]:
    """Yazma sonrasi cache gecersiz kilma icin is nesnesi etiketleri."""
    tags: set[str] = set()
    for field_name in spec.cache_policy.invalidated_by:
        value = arguments.get(field_name)
        items = value if isinstance(value, list | tuple | set) else [value]
        for item in items:
            if item not in (None, ""):
                tags.add(f"{field_name}:{item}")
    return frozenset(tags)


def invalidate_cache_for(ctx: ToolContext, tags: dict[str, Any]) -> int:
    """Yazma tool'larinin cagirdigi cache temizligi.

    Ornek: `sap_pr_submit` basarili olunca `{"material_id": [...]}` verir ve
    o malzemeye bagli okuma cache'leri duser. Temizlik **tenant sinirlidir**.
    """
    if ctx.cache is None or ctx.actor is None:
        return 0
    resolved: set[str] = set()
    for key, value in tags.items():
        items = value if isinstance(value, list | tuple | set) else [value]
        for item in items:
            if item not in (None, ""):
                resolved.add(f"{key}:{item}")
    return ctx.cache.invalidate_tags(ctx.actor.tenant, frozenset(resolved))


def _record_metric(
    ctx: ToolContext,
    spec: ToolSpec | None,
    name: str,
    *,
    outcome: str,
    started: float,
    result: str,
    trimmed: bool = False,
    dropped_items: int = 0,
    detail: str = "standard",
    denial_code: str = "",
    cache_hit: bool = False,
    dlp_findings: int = 0,
) -> None:
    if ctx.metrics is None:
        return
    ctx.metrics.record_tool(
        ToolInvocationMetric(
            tool=name,
            domain=spec.domain if spec else "unknown",
            risk_tier=spec.risk_tier.value if spec else "R0",
            outcome=outcome,
            duration_ms=(time.perf_counter() - started) * 1000,
            result_tokens=estimate_tokens(result),
            trimmed=trimmed,
            dropped_items=dropped_items,
            detail=detail,
            denial_code=denial_code,
            cache_hit=cache_hit,
            sap_calls=ctx.sap_call_count,
            dlp_findings=dlp_findings,
        )
    )


def load_all_tools() -> None:
    """Tum tool modullerini import ederek kayit defterini doldurur."""
    from . import (  # noqa: F401
        p2p_read,
        planning,
        platform,
        procurement,
        reporting,
    )


def registry_summary() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "group": s.group,
            "domain": s.domain,
            "risk": s.risk_tier.value,
            "scopes": ",".join(s.required_scopes),
            "approval": s.approval_policy,
            "description": s.description.split("\n")[0][:110],
        }
        for s in REGISTRY.values()
    ]


def registry_contracts() -> list[dict[str, Any]]:
    return [spec.to_contract() for spec in REGISTRY.values()]
