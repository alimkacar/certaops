"""FastAPI servisi (guvenli kanal).

Calistirma:
    uvicorn certaops.api:app --reload --port 8000
veya
    python run_api.py

API guvenlik ve sureklilik kapilari burada uygulanir:
  - Kimlik dogrulama ve actor context (AGENT_AUTH_MODE)
  - Kalici oturum durumu (restart ve coklu worker'a dayanikli)
  - Rate limit ve istek boyutu siniri
  - Yapilandirilmis hata modeli: ham exception metni istemciye donmez
  - Tool preview'larinda PII/secret maskeleme
  - Her istekte correlation ID ve audit izi
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from certaops.providers import messages_from_dicts, messages_to_dicts
from certaops.runtime import SAPAgentRuntime, profile_catalogue

from ..adapters.bpa import ApprovalRequest, build_approval_gateway
from ..cache import get_tool_cache
from ..config import get_settings, setup_logging
from ..contracts import SCOPE_PLATFORM_READ, ActorContext
from ..core import (
    DIRECT_ANSWER_TOOLS,
    ApprovalError,
    ApprovalStore,
    SessionBusy,
    SessionConflict,
    SessionOwnershipError,
    approval_payload_for,
    build_session_store,
    get_audit_ledger,
    get_state_db,
    shortcut_catalogue,
)
from ..observability import TelemetryCollector, log_context, mask_payload, truncate_preview
from ..privacy import RetentionSweeper, StreamSanitizer, sanitize_for_client
from ..sap import get_backend, reset_backend
from ..security_at_rest import maybe_cipher
from ..tools import REGISTRY, load_all_tools, registry_contracts, visible_tool_names
from .auth import AuthenticationError, Authenticator, SharedRateLimiter
from .ui import UI_SECURITY_HEADERS, build_ui_router, mount_assets

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    setup_logging()
    load_all_tools()
    # Audit sifrelemesi aciksa anahtar/kutuphane hatasi ilk istege kadar
    # ertelenmez; ledger startup'ta kurulur ve eksik yapilandirma fail-closed
    # olarak servisin acilmasini durdurur.
    _audit_ledger()
    purged = _session_store.purge_expired()
    log.info(
        "CertaOps API baslatildi | auth=%s | session=%s | expired_purged=%d",
        _settings.security.auth_mode,
        _settings.state.session_backend,
        purged,
    )
    if _settings.security.auth_mode == "none":
        log.warning(
            "AGENT_AUTH_MODE=none: kimlik dogrulama kapali. Uretimde static_token veya oidc kullanin."
        )

    # Saklama temizligi uygulama baslangicina degil periyodik job'a baglidir;
    # uzun sure ayakta kalan bir surecte suresi dolmus oturum ve evidence
    # kayitlari aksi halde diskte kalirdi.
    interval = _settings.privacy.retention_sweep_seconds
    if interval > 0:
        _retention.sweep()  # ilk temizlik hemen
        _retention.start(interval_seconds=interval)
        log.info("Saklama temizligi etkin | her %d saniye", interval)
    else:
        log.warning(
            "AGENT_RETENTION_SWEEP_SECONDS=0: periyodik saklama temizligi kapali."
        )
    try:
        yield
    finally:
        _retention.stop()
        _cache.clear()
        _shutdown_runtimes()


app = FastAPI(
    title="CertaOps API",
    version="0.1.0",
    description=(
        "SAP S/4HANA icin domain-izole multi-agent platformu. Ana veri, planlama, "
        "tedarik zinciri, satinalma, proje finans ve platform agent'lari ortak policy, "
        "onay, idempotency, evidence ve audit cekirdegini kullanir."
    ),
    lifespan=_lifespan,
)

_settings = get_settings()
# Uretim profilinde guvensiz yapilandirma servisi HIC baslatmaz.
# Yanlis bir deployment ayarinin sessizce gercek SAP yazmasi yapmasindansa
# uygulamanin acilmamasi tercih edilir.
_settings.enforce_production_profile()
# Kayit defteri import aninda doldurulur: /tools ve /approvals startup event'ine
# bagli kalmadan dogru sozlesmeyi gormek zorunda.
load_all_tools()
_authenticator: Authenticator | None = None
# Paylasilan sayim: yapilandirilan limit toplam limittir, worker basina degil.
_rate_limiter = SharedRateLimiter(
    _settings.security.rate_limit_per_minute, get_state_db(_settings.state.db_path)
)
# BASARISIZ kimlik dogrulama denemeleri icin AYRI sayac.
#
# Neden ayri: normal limitleyici `tenant:subject` ile anahtarlanir ve ancak
# dogrulama BASARIYLA bittikten sonra calisir. Yani basarisiz denemeler hic
# sayilmiyordu; gecersiz token'la sinirsiz hizda deneme yapilabiliyordu.
# Bu sayac IP ile anahtarlanir (henuz bir kimlik yok) ve dogrulamadan ONCE
# calisir. Limit daha dardir: mesru bir istemcinin dakikada bes kez kimlik
# dogrulamayi kacirmasi icin bir sebep yoktur.
_auth_failure_limiter = SharedRateLimiter(
    max(5, _settings.security.rate_limit_per_minute // 3),
    get_state_db(_settings.state.db_path),
)
_session_store = build_session_store(_settings)
_telemetry = TelemetryCollector()
_agents: dict[str, SAPAgentRuntime] = {}
#: Onbellekteki her runtime'in kuruldugu andaki actor guvenlik parmak izi.
_agent_fingerprints: dict[str, str] = {}
# Periyodik saklama temizligi ve paylasilan okuma cache'i.
# Cache tek ornek olmak zorunda: invalidation ancak tum okuyucular ayni depoyu
# goruyorsa ise yarar.
_cache = get_tool_cache(_settings)
_retention = RetentionSweeper(get_state_db(_settings.state.db_path))


def _auth() -> Authenticator:
    global _authenticator
    if _authenticator is None:
        _authenticator = Authenticator(_settings)
    return _authenticator


def _audit_ledger():
    return get_audit_ledger(
        _settings.state.db_path,
        mirror_path=(
            _settings.state.audit_mirror_path if _settings.state.audit_mirror_enabled else None
        ),
        cipher=maybe_cipher(_settings.privacy.audit_encryption, purpose="audit"),
    )


def _approval_store() -> ApprovalStore:
    return ApprovalStore(
        get_state_db(_settings.state.db_path),
        default_ttl_minutes=_settings.security.approval_ttl_minutes,
    )


# --- Istek/yanit modelleri --------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(description="Kullanici mesaji.", max_length=20_000)
    session_id: str | None = Field(
        default=None, description="Mevcut oturum kimligi. Bos birakilirsa yeni oturum acilir."
    )
    include_tool_calls: bool = Field(
        default=False, description="Yanitta tool cagri detaylarini dondur (maskelenmis)."
    )


class ToolCallOut(BaseModel):
    name: str
    arguments: dict[str, Any]
    is_error: bool
    result_preview: str
    #: Tool sonucunun **yapisal** hali (maskelenmis). `result_preview` bir
    #: operatore gosterilmek uzere kirpilmis metindir; kirpma JSON'u
    #: bozdugu icin arayuz ondan kart/tablo cizemez. Bu alan ayni icerigi
    #: kirpmadan, ama `mask_payload` uzerinden gecirerek verir.
    #:
    #: Yeni bir veri yolu ACMAZ: payload zaten tool katmaninda DLP'den
    #: gecmis, alan politikasi uygulanmis ve token butcesine sigdirilmistir
    #: (bkz. tools.registry.execute_tool). Burada yalniz ek bir maskeleme
    #: katmani vardir - `result_preview` ile ayni gizlilik seviyesi.
    #:
    #: Sonuc JSON degilse (olmamali) None doner; arayuz metne duser.
    result_json: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tool_call_count: int
    iterations: int
    input_tokens: int
    output_tokens: int
    #: Yanit LLM'e hic ugramadan mi uretildi? True ise SAP verisi model
    #: saglayicisina gonderilmedi (bkz. core.direct).
    direct_answer: bool = False
    direct_answer_reason: str = ""
    #: Bu turda kac model cagrisi yapildi. 0 = veri surecin disina cikmadi.
    model_calls: int = 0
    provider: str = ""
    model: str = ""
    active_packs: list[str] = Field(default_factory=list)
    active_agents: list[str] = Field(default_factory=list)
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)
    policy_denials: int = 0
    needs_review: bool = False
    correlation_id: str = ""
    artifacts: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallOut] | None = None


class ApprovalDecision(BaseModel):
    tool: str = Field(description="Onaylanan tool adi, or. sap_pr_submit.")
    payload: dict[str, Any] = Field(
        description="Onaylanan is icerigi (prepare tool'unun dondurdugu argumanlar)."
    )
    comment: str = Field(default="", max_length=500)
    max_value: float | None = Field(
        default=None, description="Onayin gecerli oldugu ust tutar siniri."
    )
    requested_by: str = Field(default="", description="Talebi hazirlayan kullanici.")


class ApprovalResponse(BaseModel):
    approval_id: str
    payload_sha256: str
    expires_at: str
    approvers: list[str]


class ErrorResponse(BaseModel):
    error: str
    code: str
    correlation_id: str = ""


# --- Middleware -------------------------------------------------------------
@app.middleware("http")
async def _guard_middleware(request: Request, call_next):
    """Correlation ID, istek boyutu ve yapilandirilmis hata sarmalayicisi."""
    correlation_id = request.headers.get("X-Correlation-ID") or f"api-{uuid.uuid4().hex[:12]}"
    request.state.correlation_id = correlation_id

    # Log baglami burada baglanir: `call_next` alt gorevi baglami kopyalayarak
    # devralir, dolayisiyla bu istegin urettigi her log satiri correlation
    # ID'yi tasir. Tenant/subject burada bilinmiyor (kimlik dogrulama daha
    # sonra, `current_actor` bagimliliginda calisir ve senkron bagimliliklar
    # ayri threadpool cagrilarinda yasadigi icin oradaki bir bind uc noktaya
    # ulasmaz); o iki alani runtime turu kendi basinda baglar.
    with log_context(correlation_id=correlation_id, channel="api"):
        declared = request.headers.get("content-length")
        limit = _settings.security.max_request_bytes
        if declared and declared.isdigit() and int(declared) > limit:
            return JSONResponse(
                status_code=413,
                content={
                    "error": f"Istek govdesi {limit} bayt sinirini asiyor.",
                    "code": "REQUEST_TOO_LARGE",
                    "correlation_id": correlation_id,
                },
            )

        # Content-Length bulunmayabilir (chunked transfer) veya kotu niyetli bir
        # istemci tarafindan eksik bildirilebilir. Gercek govdeyi de limitli oku;
        # Starlette'in alt katmani icin onbellege koyarak yeniden oynat.
        chunks: list[bytes] = []
        actual_size = 0
        async for chunk in request.stream():
            actual_size += len(chunk)
            if actual_size > limit:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": f"Istek govdesi {limit} bayt sinirini asiyor.",
                        "code": "REQUEST_TOO_LARGE",
                        "correlation_id": correlation_id,
                    },
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)  # type: ignore[attr-defined]

        try:
            response: Response = await call_next(request)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            # Ham exception metni istemciye donmez; yalniz korelasyon kimligi
            # istemciyle paylasilir.
            log.exception("Beklenmeyen hata")
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Beklenmeyen bir hata olustu. Detay sunucu loglarinda.",
                    "code": "INTERNAL_ERROR",
                    "correlation_id": correlation_id,
                },
            )
        response.headers["X-Correlation-ID"] = correlation_id
        if request.url.path.startswith("/ui"):
            # Statik varliklar StaticFiles tarafindan servis edilir ve kendi
            # basliklarini bilmez; CSP'yi tek noktadan burada uygulariz ki
            # arayuzun her yanitinda ayni kisitlar gecerli olsun.
            response.headers.update(UI_SECURITY_HEADERS)
        return response


# --- Bagimliliklar ----------------------------------------------------------
def _client_bucket(request: Request) -> str:
    """Kimlik yokken sayim icin istemci kimligi.

    Ters proxy arkasinda `request.client.host` proxy'nin adresidir; bu yuzden
    guvenilir proxy basligi varsa ilk atlama tercih edilir. Deger yalnizca
    SAYIM icin kullanilir - hicbir yetki karari buna dayanmaz, dolayisiyla
    sahtelenmis bir baslik yetki kazandiramaz, yalnizca kendi kovasini degistirir.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return f"authfail:{forwarded.split(',')[0].strip()[:64]}"
    client = request.client
    return f"authfail:{client.host if client else 'unknown'}"


def current_actor(request: Request) -> ActorContext:
    """Authorization basligindan dogrulanmis actor uretir."""
    try:
        actor = _auth().resolve(request.headers.get("Authorization"))
    except AuthenticationError as exc:
        # Basarisiz deneme SAYILIR. Onceki halde bu satira gelen istek hicbir
        # sayaca dokunmadan 401 aliyordu: gecersiz token'la kaba kuvvet
        # denemesi sinirsiz hizda yapilabiliyordu.
        allowed, _ = _auth_failure_limiter.check(_client_bucket(request))
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Cok fazla basarisiz kimlik dogrulama denemesi.",
                    "code": "AUTH_RATE_LIMITED",
                    "retry_after_s": _auth_failure_limiter.retry_after(),
                    "correlation_id": getattr(request.state, "correlation_id", ""),
                },
            ) from exc
        raise HTTPException(
            status_code=401,
            detail={
                "error": str(exc),
                "code": exc.code,
                "correlation_id": getattr(request.state, "correlation_id", ""),
            },
        ) from exc

    allowed, _ = _rate_limiter.check(f"{actor.tenant}:{actor.subject}")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Istek limiti asildi.",
                "code": "RATE_LIMITED",
                "retry_after_s": _rate_limiter.retry_after(),
            },
        )
    return actor


def _agent_for(actor: ActorContext, session_id: str) -> SAPAgentRuntime:
    """Oturuma bagli agent.

    Cache anahtari `(tenant, subject, session_id)`: baska bir kullanici ayni
    session ID'yi kullansa bile ilk kullanicinin agent'i ve yetkileri yeniden
    kullanilmaz.

    Anahtara ek olarak actor'un **guvenlik parmak izi** tutulur. Roller ya da
    organizasyon kapsami degistiyse onbellekteki ornek kapatilir ve yenisi
    kurulur: yetkisi dusurulmus bir kullanici eski genis yetkili runtime'i
    kullanamaz.
    """
    key = f"{actor.tenant}:{actor.subject}:{session_id}"
    fingerprint = actor.security_fingerprint()
    cached = _agents.get(key)
    if cached is not None and _agent_fingerprints.get(key) != fingerprint:
        with suppress(Exception):
            cached.close()
        _agents.pop(key, None)
        _agent_fingerprints.pop(key, None)
        log.info("actor guvenlik baglami degisti; runtime yeniden kuruluyor (%s)", key)
    agent = _agents.get(key)
    if agent is None:
        try:
            agent = SAPAgentRuntime(
                _settings, actor=actor, channel="api", telemetry=_telemetry
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail={"error": str(exc), "code": "AGENT_UNAVAILABLE"}
            ) from exc
        _agents[key] = agent
        _agent_fingerprints[key] = fingerprint
        if len(_agents) > _settings.state.max_sessions:
            evicted = next(iter(_agents))
            stale = _agents.pop(evicted, None)
            _agent_fingerprints.pop(evicted, None)
            # Kapasite eviction'i saglayici baglantisini de birakmali; aksi
            # halde uzun sureli bir surecte soketler sizar.
            if stale is not None:
                with suppress(Exception):
                    stale.close()
    return agent


def _evict_runtime(actor: ActorContext, session_id: str) -> None:
    """Bir oturumun onbelleklenmis runtime'ini kapatir ve dusurur."""
    key = f"{actor.tenant}:{actor.subject}:{session_id}"
    agent = _agents.pop(key, None)
    _agent_fingerprints.pop(key, None)
    if agent is not None:
        with suppress(Exception):
            agent.close()


def _shutdown_runtimes() -> None:
    """Tum runtime'lari ve health icin kullanilan paylasilan backend'i kapatir."""
    for agent in list(_agents.values()):
        with suppress(Exception):
            agent.close()
    _agents.clear()
    _agent_fingerprints.clear()
    reset_backend()


# --- Uc noktalar ------------------------------------------------------------
def _health_viewer(request: Request) -> ActorContext | None:
    """`/health` icin ISTEGE BAGLI kimlik.

    Kimlik yoksa hata verilmez - liveness probe'unun token tasimasi
    beklenemez. Kimlik varsa ve gecerliyse ayrintili duru s raporu acilir.
    Gecersiz bir token sessizce "kimliksiz" sayilir: `/health` bir kimlik
    dogrulama ucu degildir ve buradan token denemesi yapilamamalidir.
    """
    header = request.headers.get("Authorization")
    if not header:
        return None
    try:
        return _auth().resolve(header)
    except AuthenticationError:
        # Gecersiz token yine de SAYILIR; aksi halde /health kaba kuvvet icin
        # limitlenmemis bir kapi olurdu.
        _auth_failure_limiter.check(_client_bucket(request))
        return None


@app.get("/health", summary="Servis, model ve SAP baglanti durumu")
def health(request: Request) -> dict[str, Any]:
    viewer = _health_viewer(request)
    # Kimliksiz cagirana YALNIZ canlilik bilgisi verilir.
    #
    # Onceki hali herkese tam bir guvenlik durusu dokumu aciyordu: auth modu,
    # kapatilmis tool listesi, DLP modu, saklama politikasi, read_only/dry_run
    # bayraklari, audit zincir durumu ve kullanilan model. Bunlarin toplami,
    # saldirgana hicbir sey denemeden once HANGI KONTROLLERIN KAPALI oldugunu
    # soyler. Canlilik probe'unun ihtiyaci ise tek bir alandir.
    if viewer is None or not viewer.has_scope(SCOPE_PLATFORM_READ):
        return {"status": "ok"}

    ledger = _audit_ledger()
    posture = _settings.posture()
    payload: dict[str, Any] = {
        "status": "ok",
        # Verinin kaynagini gizlemeyen tek alan. Simulasyon modunda calisan bir
        # servisin cevaplari makul gorunur ama gercek degildir; bunu health
        # ciktisinda acikca soylemek, sessizce dogru gibi davranmaktan iyidir.
        "mode": "simulation" if _settings.sap.backend == "mock" else "live",
        "app_env": _settings.app_env,
        # Saglayici, model ve backend gorunur; API ANAHTARI ASLA GORUNMEZ.
        "model": _settings.model.describe(),
        "auth_mode": _settings.security.auth_mode,
        "session_backend": _settings.state.session_backend,
        "approval_gateway": _settings.security.approval_gateway,
        "read_only": _settings.sap.read_only,
        "dry_run": _settings.sap.dry_run,
        "registered_tools": len(REGISTRY),
        "architecture": "certaops-single-runtime",
        # Bir runtime ORNEGI actor'un guvenlik baglamina baglidir; iki
        # kullanici ayni ornegi paylasmaz. Bunu health'te acikca soylemek,
        # "tek runtime" ifadesinin paylasilan durum sanilmasini onler.
        "runtime_scope": "per_authenticated_session_security_context",
        # Mimari gercek: TEK bir runtime sinifi vardir (eski cok-agent
        # tasariminin aksine). Canli ornek sayisi `runtime_cache.cached`.
        "runtime_count": 1,
        "runtime_cache": {"cached": len(_agents), "max": _settings.state.max_sessions},
        "domains": len(profile_catalogue()),
        # Tam zincir taramasi buyuk defterlerde pahalidir; /health son 200 kaydi
        # dogrular. Tam dogrulama sap_get_execution_audit ile yapilir.
        "audit_head": ledger.verify(limit=200),
        "audit_checkpoint": ledger.checkpoint(),
        # Operatorun kapattigi tool'lar: modele gosterilmez ve cagrilirsa
        # reddedilir. Bir olay sirasinda "neyi kapattik" sorusunun cevabi.
        "disabled_tools": list(_settings.security.disabled_tools),
        # SAP KENDI denetim kaydinda islemi insana atfedebiliyor mu?
        # technical_user ise SAP yalniz servis hesabini gorur; atfedilebilirlik
        # bizim audit zincirimizle SINIRLIDIR.
        "sap_attribution": (
            get_backend(_settings).connection.describe().get("sap_attribution", "unknown")
            if _settings.sap.backend in {"odata", "ecc"} else "simulation"
        ),
        "production_ready": posture["production_ready"],
        # Gizlilik, risk ve cache durusu operator icin gorunur olur. Deger
        # degil, yalnizca yapilandirma ve sayim raporlanir.
        "privacy": {
            "dlp_mode": posture["dlp_mode"],
            "strict_unknown_fields": posture["strict_unknown_fields"],
            "risk_scoring_mode": posture["risk_scoring_mode"],
            "retention_sweep_seconds": _settings.privacy.retention_sweep_seconds,
            "retention_policy": _retention.policy_report(),
        },
        "cache": {
            "backend": posture["cache_backend"],
            "entries": _cache.size(),
            **_cache.stats.to_dict(),
        },
        # Hangi sorular modele hic gitmeden cevaplanabilir. Bu bir gizlilik
        # kontroludur: listedeki tool'lar disinda hicbir sonuc LLM atlanarak
        # kullaniciya donmez.
        "direct_answers": {
            "enabled": _settings.agent.direct_answers_enabled,
            "tools": sorted(DIRECT_ANSWER_TOOLS),
            "shortcuts": shortcut_catalogue(),
        },
    }
    if posture["production_blockers"]:
        # `status` servisin **su anki** saglik durumudur; uretim hazirligiyla
        # karistirilmaz. Gelistirmede simulasyon backend'iyle calisan bir servis
        # saglikldir, yalnizca uretime hazir degildir. Bu ikisi ayri alanlarda
        # raporlanir; staging'de ise uyari gercek bir dagitim hatasidir.
        payload["warnings"] = posture["production_blockers"]
        if _settings.app_env == "staging":
            payload["status"] = "degraded"
    return payload


@app.get("/tools", summary="Kayitli toollarin risk sozlesmesi")
def tools(actor: ActorContext = Depends(current_actor)) -> dict[str, Any]:
    contracts = registry_contracts()
    domains = frozenset(str(c["domain"]) for c in contracts)
    visible_names = frozenset(visible_tool_names(domains, actor, settings=_settings))
    visible = [c for c in contracts if c["tool"] in visible_names]
    return {
        "registered": len(contracts),
        "visible_to_actor": len(visible),
        "read_only": _settings.sap.read_only,
        "actor": actor.to_dict(include_scopes=True),
        "tools": visible,
    }


@app.get("/agents", summary="SAP domain yetenek katalogu (deprecated yol adi)")
def agents(actor: ActorContext = Depends(current_actor)) -> dict[str, Any]:
    """Domain yetenek gorunumu.

    Yol adi geriye donuk uyumluluk icin korunuyor, ama icerik artik ayri
    calisan agent'lar VARMIS gibi davranmiyor: tek runtime ve domain
    profilleri raporlanir.
    """
    return {
        "architecture": "certaops-single-runtime",
        "deprecated": "Ayri calisan agent'lar kaldirildi; bunlar domain profilleridir.",
        "actor": actor.to_dict(include_scopes=True),
        "domains": profile_catalogue(),
        # Eski istemciler icin ayni liste `agents` adiyla da doner.
        "agents": profile_catalogue(),
    }


def _structured_preview(result: str) -> dict[str, Any] | None:
    """Tool sonucunu arayuzun cizebilecegi maskelenmis sozluge cevirir.

    `result` `execute_tool`in dondurdugu JSON metnidir; DLP, alan politikasi
    ve token butcesi bu metin uretilmeden ONCE uygulanmistir. Buradaki tek is
    yapiyi bozmadan maskelemek.

    JSON olmayan veya sozluk olmayan sonuc icin None doner: arayuz o durumda
    `result_preview` metnini gosterir.
    """
    try:
        parsed = json.loads(result)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not _settings.security.mask_tool_previews:
        return parsed
    masked = mask_payload(parsed)
    return masked if isinstance(masked, dict) else None


def _tool_call_out(call: Any) -> ToolCallOut:
    """Bir tool cagrisini kanal ciktisina cevirir (maskeleme dahil)."""
    masking = _settings.security.mask_tool_previews
    return ToolCallOut(
        name=call.name,
        arguments=mask_payload(call.arguments) if masking else call.arguments,
        is_error=call.is_error,
        result_preview=(
            truncate_preview(call.result) if masking else call.result[:1500]
        ),
        result_json=_structured_preview(call.result),
    )


@app.post("/chat/stream", summary="Agent ile konus (parca parca yanit)")
def chat_stream(
    request: ChatRequest,
    http_request: Request,
    actor: ActorContext = Depends(current_actor),
) -> Any:
    """Model metnini uretildikce yollar (SSE).

    Ne DEGISTIRIR: toplam sure aynidir - is yine ayni. Degisen sey ilk
    karakterin ekranda gorunme suresi; uzun bir yanitta kullanici bos ekrana
    degil olusan metne bakar.

    Guvenlik: akan metin **parca parca** temizlenir. Naif yaklasim (her
    parcayi tek basina `sanitize_text`ten gecirmek) saglam degildir - bir
    sirri iki parcaya bolen sinir hicbir parcada desene uymaz.
    `StreamSanitizer` bunun icin bir kuyruk bekletir; garanti tek seferlik
    yolla ayni kalir.

    Tool cagrilari ve oturum kaydi akista DEGISMEZ: her ikisi de tur
    bittikten sonra, tek seferlik yolla ayni sirada islenir. Yani yarim bir
    metin parcasi hicbir SAP yazmasini tetikleyemez.
    """
    if not _settings.ui.stream_enabled:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Akisli yanit kapali (AGENT_UI_STREAM=false).",
                "code": "STREAM_DISABLED",
            },
        )
    if not request.message.strip():
        raise HTTPException(
            status_code=400, detail={"error": "message bos olamaz.", "code": "EMPTY_MESSAGE"}
        )

    try:
        lease = _session_store._acquire_turn(  # noqa: SLF001
            request.session_id, actor=actor
        )
    except SessionBusy as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc), "code": "SESSION_BUSY"}) from exc
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=403, detail={"error": str(exc), "code": "SESSION_NOT_OWNED"}
        ) from exc

    record = lease.record
    agent = _agent_for(actor, record.session_id)
    agent.messages = messages_from_dicts(record.messages)
    if record.active_packs:
        agent.active_packs = list(record.active_packs)

    def _events():
        sanitizer = StreamSanitizer(actor=actor, settings=_settings)
        chunks: list[str] = []

        def _on_text(piece: str) -> None:
            chunks.append(piece)

        try:
            turn = agent.chat(request.message, on_text=_on_text)
        except Exception as exc:  # noqa: BLE001
            _session_store._release_turn(lease)  # noqa: SLF001
            log.exception("chat stream hatasi")
            yield _sse("error", {"error": "Agent turu tamamlanamadi.", "code": type(exc).__name__})
            return

        # Parcalar temizlenip sirayla yollanir. Saglayici parcalari zaten
        # `chunks` icinde biriktirdi; buradaki dongu onlari sinir-guvenli
        # sekilde yayimlar.
        for piece in chunks:
            emitted = sanitizer.feed(piece)
            if emitted:
                yield _sse("text", {"delta": emitted})
        tail = sanitizer.flush()
        if tail:
            yield _sse("text", {"delta": tail})

        record.messages = messages_to_dicts(agent.messages)
        record.active_packs = list(agent.active_packs)
        record.turn_count += 1
        try:
            _session_store._commit_turn(lease)  # noqa: SLF001
        except SessionConflict as exc:
            _session_store._release_turn(lease)  # noqa: SLF001
            yield _sse("error", {"error": str(exc), "code": "SESSION_CONFLICT"})
            return

        yield _sse(
            "done",
            {
                "session_id": record.session_id,
                # Tam metin yine TEK SEFERLIK kapidan gecer: akan parcalar bir
                # kolayliktir, kanonik yanit budur.
                "reply": sanitize_for_client(turn.text, actor=actor, settings=_settings),
                "tool_call_count": len(turn.tool_calls),
                "iterations": turn.iterations,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "direct_answer": turn.direct_answer,
                "needs_review": turn.needs_review,
                "correlation_id": turn.correlation_id,
            },
        )

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/chat", response_model=ChatResponse, summary="Agent ile konus")
def chat(
    request: ChatRequest,
    http_request: Request,
    actor: ActorContext = Depends(current_actor),
) -> ChatResponse:
    if not request.message.strip():
        raise HTTPException(
            status_code=400, detail={"error": "message bos olamaz.", "code": "EMPTY_MESSAGE"}
        )

    # Tur lease'i MODEL VE TOOL CALISMADAN ONCE alinir. Ayni oturuma paralel
    # ikinci bir istek gelirse burada reddedilir; boylece iki tur ayni
    # transcript uzerine yazmaz ve ikinci bir SAP yazmasi hic baslamaz.
    try:
        lease = _session_store._acquire_turn(  # noqa: SLF001
            request.session_id, actor=actor
        )
    except SessionBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": str(exc), "code": "SESSION_BUSY"},
        ) from exc
    except SessionOwnershipError as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": str(exc), "code": "SESSION_NOT_OWNED"},
        ) from exc
    record = lease.record
    agent = _agent_for(actor, record.session_id)
    # Kalici depodan gecmisi geri yukle: restart ve baska worker sonrasi tutarlilik.
    # Kayit JSON'dur; `ModelMessage`e geri cevrilmeden saglayiciya verilemez.
    agent.messages = messages_from_dicts(record.messages)
    if record.active_packs:
        agent.active_packs = list(record.active_packs)

    try:
        turn = agent.chat(request.message)
    except Exception as exc:  # noqa: BLE001
        _session_store._release_turn(lease)  # noqa: SLF001
        log.exception("chat hatasi")
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Agent turu tamamlanamadi.",
                "code": type(exc).__name__,
                "correlation_id": getattr(http_request.state, "correlation_id", ""),
            },
        ) from exc

    record.messages = messages_to_dicts(agent.messages)
    record.active_packs = list(agent.active_packs)
    record.turn_count += 1
    try:
        _session_store._commit_turn(lease)  # noqa: SLF001
    except SessionConflict as exc:
        _session_store._release_turn(lease)  # noqa: SLF001
        # Ayni oturum baska bir istekte guncellenmis. Turun sonucu kaybolmasin
        # diye kullaniciya acik bir cakisma bildirilir.
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(exc),
                "code": "SESSION_CONFLICT",
                "reply": turn.text,
                "correlation_id": turn.correlation_id,
            },
        ) from exc

    tool_calls = None
    if request.include_tool_calls:
        tool_calls = [_tool_call_out(call) for call in turn.tool_calls]

    # OWASP LLM05 (zero-trust): model ciktisi dogrulanmadan asagi akise
    # verilmez. Model normalde D3 gormez, ama prompt injection veya model
    # hatasi sonucu cevabina hassas veri yazabilir. Son kapi burasidir.
    return ChatResponse(
        session_id=record.session_id,
        reply=sanitize_for_client(turn.text, actor=actor, settings=_settings),
        tool_call_count=len(turn.tool_calls),
        iterations=turn.iterations,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        direct_answer=turn.direct_answer,
        direct_answer_reason=turn.direct_answer_reason,
        model_calls=turn.model_calls,
        # Saglayici/model bilgisi raporlamadir, sozlesmenin zorunlu parcasi
        # degil: alternatif bir runtime uygulamasi bunlari bildirmeyebilir.
        provider=getattr(turn, "provider", ""),
        model=getattr(turn, "model", ""),
        active_packs=turn.active_packs,
        active_agents=turn.active_agents,
        agent_trace=turn.agent_trace,
        policy_denials=turn.policy_denials,
        needs_review=turn.needs_review,
        correlation_id=turn.correlation_id,
        artifacts=turn.artifacts,
        tool_calls=tool_calls,
    )


@app.post("/approvals", response_model=ApprovalResponse, summary="Bir islemi onayla")
def create_approval(
    decision: ApprovalDecision,
    actor: ActorContext = Depends(current_actor),
) -> ApprovalResponse:
    """Yetkili onaylayicinin onay kaydi uretmesi.

    Bu uc nokta bilerek agent'in erisemedigi bir yerdedir: model kendi islemini
    onaylayamaz. Onay yalnizca ilgili approve kapsamina sahip bir actor
    tarafindan verilebilir.
    """
    spec = REGISTRY.get(decision.tool)
    if spec is None:
        raise HTTPException(
            status_code=404, detail={"error": f"Bilinmeyen tool: {decision.tool}", "code": "UNKNOWN_TOOL"}
        )
    if not spec.risk_tier.requires_approval:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"{decision.tool} onay gerektirmiyor (risk {spec.risk_tier.value}).",
                "code": "APPROVAL_NOT_APPLICABLE",
            },
        )
    if not actor.has_scope(spec.approve_scope):
        raise HTTPException(
            status_code=403,
            detail={
                "error": f"Onay icin '{spec.approve_scope}' kapsami gerekiyor.",
                "code": "MISSING_APPROVE_SCOPE",
            },
        )
    if decision.requested_by and decision.requested_by == actor.subject:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Gorevler ayrimi (SoD): talebi hazirlayan kisi onaylayamaz.",
                "code": "SOD_VIOLATION",
            },
        )

    if _settings.security.approval_gateway != "local":
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "Onay gecidi 'bpa' modunda: onay SAP Build Process Automation "
                    "workflow'u uzerinden verilir, bu uc noktadan degil."
                ),
                "code": "APPROVAL_GATEWAY_IS_BPA",
            },
        )
    gateway = build_approval_gateway(_settings, _approval_store())
    canonical = approval_payload_for(decision.payload)
    request = ApprovalRequest(
        tool=decision.tool,
        payload=canonical,
        tenant=actor.tenant,
        requested_by=decision.requested_by,
        subject_line=f"{decision.tool} onayi",
        diff=[],
        max_value=decision.max_value,
    )
    task = gateway.request(request)
    try:
        record = gateway.complete(
            task_id=task["task_id"], approvers=[actor], request=request, comment=decision.comment
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from exc

    _audit_ledger().append(
        "approval.granted",
        actor=actor,
        tool=decision.tool,
        risk_tier=spec.risk_tier.value,
        outcome="ok",
        approval_id=record.approval_id,
        payload_sha256=record.payload_sha256,
        detail={"comment": decision.comment[:200], "max_value": decision.max_value},
    )
    return ApprovalResponse(
        approval_id=record.approval_id,
        payload_sha256=record.payload_sha256,
        expires_at=record.expires_at.isoformat(),
        approvers=[a.subject for a in record.approvers],
    )


@app.get("/approvals/{approval_id}", summary="Onay kaydini goruntule")
def get_approval(approval_id: str, actor: ActorContext = Depends(current_actor)) -> dict[str, Any]:
    record = _approval_store().get(approval_id)
    if record is None or record.tenant != actor.tenant:
        raise HTTPException(
            status_code=404, detail={"error": "Onay bulunamadi.", "code": "APPROVAL_NOT_FOUND"}
        )
    return record.to_dict()


@app.get("/telemetry", summary="Token ve policy telemetrisi")
def telemetry(actor: ActorContext = Depends(current_actor)) -> dict[str, Any]:
    if not actor.has_scope("audit.read"):
        raise HTTPException(
            status_code=403,
            detail={"error": "'audit.read' kapsami gerekiyor.", "code": "MISSING_SCOPE"},
        )
    return {
        "snapshot": _telemetry.snapshot(),
        "budgets": {
            "schema_tokens_per_turn": _settings.budget.schema_tokens_per_turn,
            "single_result_tokens": _settings.budget.single_result_tokens,
            "turn_result_tokens": _settings.budget.turn_result_tokens,
        },
    }


@app.delete("/sessions/{session_id}", summary="Oturumu sil")
def delete_session(
    session_id: str, actor: ActorContext = Depends(current_actor)
) -> dict[str, str]:
    if not _session_store.delete(session_id, actor=actor):
        raise HTTPException(
            status_code=404, detail={"error": "Oturum bulunamadi.", "code": "SESSION_NOT_FOUND"}
        )
    _agents.pop(f"{actor.tenant}:{actor.subject}:{session_id}", None)
    return {"deleted": session_id}


@app.get("/sessions", summary="Acik oturumlar")
def list_sessions(actor: ActorContext = Depends(current_actor)) -> dict[str, Any]:
    records = _session_store.list(actor=actor)
    return {"count": len(records), "sessions": [r.to_summary() for r in records]}


# --- Operator arayuzu -------------------------------------------------------
# Arayuz opsiyoneldir ve kapaliyken TEK BIR uc nokta bile kayitli olmaz:
# kapatilmis bir ozelligin yolu 403 degil 404 dondurmelidir, aksi halde
# varligi disaridan anlasilir. `/logs` ve `/audit/recent` de bu blogun
# icindedir; arayuz kapaliysa canli log HTTP uzerinden hic sunulmaz.
if _settings.ui.enabled:
    app.include_router(
        build_ui_router(
            settings=_settings,
            actor_dependency=current_actor,
            audit_ledger=_audit_ledger,
        )
    )
    mount_assets(app)
    log.info("Operator arayuzu /ui adresinde acik (app_env=%s)", _settings.app_env)
