"""Operator arayuzunun sunucu tarafi: statik dosyalar ve okuma uclari.

Arayuz API ile **ayni origin**den servis edilir. Bu bir kolaylik degil,
guvenlik karari: ayri bir origin CORS acmayi ve token'i baska bir kaynaga
gondermeyi gerektirirdi. Ayni origin'de arayuz yeni bir saldiri yuzeyi
acmaz - her istek zaten var olan `Authorization` kapisindan ve ayni kapsam
kontrollerinden gecer.

Arayuz **hicbir yeni yetki tanimlamaz**. `/logs` ve `/audit/recent` uclari
`audit.read` kapsami ister; bir kullanicinin arayuzden gorebilecegi sey
`curl` ile gorebilecegiyle birebir aynidir.

Router bir fabrika fonksiyonuyla kurulur (`build_ui_router`): boylece bu
modul `channels.api`yi import etmez ve dairesel bagimlilik olusmaz.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from certaops.mcp_diagnostics import MCPProbeError, probe_stdio, status_snapshot

from ..config import Settings, get_settings
from ..contracts import SCOPE_AUDIT_READ, SCOPE_PLATFORM_READ, ActorContext
from ..observability import get_log_buffer
from ..privacy import build_dlp_engine
from ..runtime_config import ConfigRefused, ConfigService, SettingError

log = logging.getLogger(__name__)

#: Statik varliklar wheel'in icine paket verisi olarak girer.
WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"
ASSETS_DIR = WEBUI_DIR / "assets"
INDEX_FILE = WEBUI_DIR / "index.html"

#: Arayuz yanitlarina eklenen guvenlik basliklari.
#:
#: CSP bilerek dardir: `default-src 'self'` disaridan script/stil/font
#: yuklenmesini engeller. Arayuzde CDN, analytics ve harici yazi tipi YOKTUR;
#: bir SAP operator ekraninda ucuncu tarafa giden tek bir istek bile kabul
#: edilemez. `connect-src 'self'` ayni sekilde arayuzun okudugu veriyi baska
#: bir origin'e gondermesini imkansiz kilar.
UI_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Arayuz kimlik dogrulamali veri gosterir; ara katmanlar onbelleklememeli.
    "Cache-Control": "no-store",
}

_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _require_audit_read(actor: ActorContext) -> None:
    if not actor.has_scope(SCOPE_AUDIT_READ):
        raise HTTPException(
            status_code=403,
            detail={"error": f"'{SCOPE_AUDIT_READ}' kapsami gerekiyor.", "code": "MISSING_SCOPE"},
        )


def _require_platform_read(actor: ActorContext) -> None:
    if not actor.has_scope(SCOPE_PLATFORM_READ):
        raise HTTPException(
            status_code=403,
            detail={"error": f"'{SCOPE_PLATFORM_READ}' kapsami gerekiyor.", "code": "MISSING_SCOPE"},
        )


class SettingChange(BaseModel):
    """Tek bir ayar degisikligi onerisi."""

    key: str = Field(min_length=1, max_length=64)
    value: Any = None
    reason: str = Field(default="", max_length=500)


class ApprovalAction(BaseModel):
    """Bekleyen bir degisiklige yapilan islem."""

    change_id: str = Field(min_length=1, max_length=64)


def build_ui_router(
    *,
    settings: Settings,
    actor_dependency: Callable[..., ActorContext],
    audit_ledger: Callable[[], Any],
) -> APIRouter:
    """Arayuzun statik kabugunu ve okuma uclarini tasiyan router.

    `actor_dependency` ve `audit_ledger` disaridan verilir: bu modul kanal
    katmaninin global durumunu bilmez, dolayisiyla testte sahte bir actor ile
    tek basina kurulabilir.

    Statik varliklar `/ui/assets` altina baglanir, veri uclari `/ui/config`
    gibi ayri yollarda durur. Tek bir `/ui` mount'u kullanilsaydi hangi yolun
    once eslesecegi route kayit sirasina bagli olurdu; yollari ayirmak bu
    kirilganligi tumden ortadan kaldirir.
    """
    router = APIRouter(tags=["ui"])
    max_page = settings.ui.max_page_size
    # Denetim kayitlari ekrana verilmeden once DLP'den gecer (bkz. audit_recent).
    ui_dlp = build_dlp_engine(settings)

    @router.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        """Kok yolu arayuze yonlendirir.

        Servisin adresini yazan operator JSON bir 404 ile karsilasmamali.
        Yonlendirme **gecicidir** (307): arayuz kapatilabilir bir ozelliktir ve
        kalici bir yonlendirme tarayici tarafindan onbelleklenip kapatildiktan
        sonra da /ui'ye gitmeye devam ederdi.

        Arayuz kapaliyken bu router hic kayitli olmaz, dolayisiyla kok yol yine
        404 doner - kapali bir ozelligin varligi disaridan anlasilmaz.
        """
        return RedirectResponse(url="/ui", status_code=307)

    @router.get("/ui", include_in_schema=False)
    @router.get("/ui/", include_in_schema=False)
    def ui_index() -> FileResponse:
        """Arayuz kabugu.

        Bu yanit kimlik dogrulamasi ISTEMEZ ve hicbir veri tasimaz: yalnizca
        HTML iskeleti. Token'i girecegi ekrani gormek icin token isteyen bir
        arayuz kullanilamaz olurdu. Veri tasiyan her uc ayrica dogrulanir.
        """
        if not INDEX_FILE.is_file():
            raise HTTPException(
                status_code=404,
                detail={"error": "Arayuz dosyalari kurulu degil.", "code": "UI_NOT_INSTALLED"},
            )
        return FileResponse(INDEX_FILE, media_type="text/html", headers=UI_SECURITY_HEADERS)

    @router.get("/ui/config", summary="Arayuzun ihtiyac duydugu yapilandirma")
    def ui_config(actor: ActorContext = Depends(actor_dependency)) -> dict[str, Any]:
        """Arayuzun neyi gosterebilecegi.

        Kapsamlar burada da bildirilir ki arayuz kullaniciya erisemeyecegi bir
        sekmeyi hic gostermesin. Bu bir yetki kontrolu DEGILDIR - gercek kapi
        her ucun kendi kapsam kontroludur; bu yalniz bos ekran gostermemek
        icindir.
        """
        return {
            "actor": actor.to_dict(include_scopes=True),
            "app_env": settings.app_env,
            "mode": "simulation" if settings.sap.backend == "mock" else "live",
            "dry_run": settings.sap.dry_run,
            "log_stream_enabled": settings.ui.log_stream_enabled,
            "can_read_audit": actor.has_scope(SCOPE_AUDIT_READ),
            "max_page_size": max_page,
        }

    @router.get("/audit/recent", summary="Son denetim kayitlari ve zincir durumu")
    def audit_recent(
        actor: ActorContext = Depends(actor_dependency),
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> dict[str, Any]:
        _require_audit_read(actor)
        ledger = audit_ledger()
        capped = min(limit, max_page)
        # Tenant filtresi cagirandan DEGIL actor'dan alinir: bir denetci baska
        # bir tenant'in kayitlarini parametre degistirerek goremez.
        entries = ledger.recent(limit=capped, tenant=actor.tenant)

        # Denetim kaydinin `detail`/`before`/`after` alanlari SAP IS VERISI
        # tasir: tedarikci adi, fiyat, miktar, fatura tarafi. Defterin kendi
        # `redact()` fonksiyonu yalniz KIMLIK BILGISI maskeler (parola, token);
        # veri siniflandirmasi (D2/D3) hakkinda hicbir sey bilmez. Sonuc:
        # `audit.read` kapsami olan bir denetci, "ne oldu" bilgisiyle birlikte
        # is verisinin tamamini da goruyordu.
        #
        # Defterdeki kayit degismez - zincir butunlugu ve adli inceleme degeri
        # korunur. Maskeleme yalniz bu GORUNUMDE yapilir; tam kayda erisim
        # sap_get_execution_audit uzerinden, kendi amac ve kapsam kapisiyla
        # alinir.
        masked = ui_dlp.apply(
            list(reversed(entries)),
            actor=actor,
            sink="client",
            purpose="audit_review",
        )
        return {
            "tenant": actor.tenant,
            "count": len(entries),
            "limit": capped,
            # Tam zincir taramasi buyuk defterlerde pahalidir; arayuz son
            # pencereyi dogrular. Tam dogrulama sap_get_execution_audit ile.
            "chain": ledger.verify(limit=max(capped, 200)),
            # En yeni ustte: operator ekraninda son olay ilk gorunmeli.
            "entries": masked.payload,
            "dlp_masked": bool(masked.findings),
        }

    @router.get("/logs", summary="Canli log tamponu")
    def logs(
        actor: ActorContext = Depends(actor_dependency),
        limit: int = Query(default=100, ge=1, le=1000),
        level: str = Query(default="INFO"),
    ) -> dict[str, Any]:
        _require_audit_read(actor)
        if not settings.ui.log_stream_enabled:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "Canli log gorunumu kapali (AGENT_UI_LOG_STREAM=false).",
                    "code": "LOG_STREAM_DISABLED",
                },
            )
        buffer = get_log_buffer()
        if buffer is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "Log tamponu kurulu degil (LOG_BUFFER_SIZE=0).",
                    "code": "LOG_BUFFER_DISABLED",
                },
            )
        normalized = level.upper()
        if normalized not in _LEVELS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"Gecersiz seviye '{level}'. {'/'.join(_LEVELS)} olmali.",
                    "code": "INVALID_LOG_LEVEL",
                },
            )
        capped = min(limit, max_page)
        return {
            "capacity": buffer.capacity,
            # Maskelemenin acik olup olmadigi gorunur olmali: operator
            # ekranindaki bir e-postanin gercek mi maskeli mi oldugunu
            # tahmin etmek zorunda kalmamali.
            "masked": buffer.mask,
            "level": normalized,
            "limit": capped,
            "entries": buffer.snapshot(limit=capped, min_level=_LEVELS[normalized]),
        }

    # --- MCP baglanti teshisi ---------------------------------------------
    # Arayuz MCP'yi kendi calisma kanali yapmaz. Bu uclar yalniz stdio
    # sunucusunun gorunurlugunu ve el sikismasini yonetim ekranina tasir.

    @router.get("/ui/mcp", summary="MCP baglanti durusu ve istemci yapilandirmasi")
    def mcp_status(actor: ActorContext = Depends(actor_dependency)) -> dict[str, Any]:
        _require_platform_read(actor)
        return status_snapshot(get_settings())

    @router.post("/ui/mcp/test", summary="MCP stdio initialize ve tools/list testi")
    async def mcp_test(actor: ActorContext = Depends(actor_dependency)) -> dict[str, Any]:
        _require_platform_read(actor)
        try:
            return await probe_stdio(get_settings())
        except MCPProbeError as exc:
            log.warning("MCP stdio teshisi basarisiz: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={"error": str(exc), "code": "MCP_HANDSHAKE_FAILED"},
            ) from None

    # --- Calisma zamani ayarlari ------------------------------------------
    # Bu uclar arayuz router'inin icinde duruyor: arayuz kapaliyken hic
    # kayitli olmazlar. Kapatilmis bir yonetim yuzeyi 403 degil 404
    # dondurmelidir; varligi disaridan anlasilmamali.

    def _service(actor: ActorContext) -> ConfigService:
        # Ayarlar her istekte taze okunur: baska bir surec ya da elle yapilan
        # bir degisiklikten sonra bayat bir goruntu gostermemek icin.
        return ConfigService(get_settings(), audit=audit_ledger())

    def _refused(exc: Exception) -> HTTPException:
        if isinstance(exc, ConfigRefused):
            durum = {
                "MISSING_SCOPE": 403,
                "SOD_VIOLATION": 403,
                "ENV_PINNED": 409,
                "WOULD_BREAK_PRODUCTION_GATE": 409,
                "UNKNOWN_CHANGE": 404,
            }.get(exc.code, 400)
            govde: dict[str, Any] = {"error": exc.message, "code": exc.code}
            if exc.detail is not None:
                govde["detail"] = exc.detail
            return HTTPException(status_code=durum, detail=govde)
        return HTTPException(
            status_code=400, detail={"error": str(exc), "code": "INVALID_SETTING"}
        )

    @router.get("/ui/settings", summary="Degistirilebilir ayarlar ve mevcut degerleri")
    def read_settings(
        lang: str = Query("tr", pattern="^(tr|en)$"),
        actor: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Ayar tablosu.

        Okumak icin `platform.read` yeterli: operator neyin nasil
        yapilandirildigini gorebilmeli. DEGISTIRMEK ayri bir kapsam ister ve
        her alan kendi `locked`/`locked_reason` bilgisini tasir, boylece
        arayuz neden degistiremedigini kullaniciya soyleyebilir.

        Bu uc hicbir sir dondurmez: kimlik bilgileri ve anahtarlar izin
        listesinde bulunmadigi icin buraya hic ulasmaz.
        """
        if not actor.has_scope(SCOPE_PLATFORM_READ):
            raise HTTPException(
                status_code=403,
                detail={"error": "platform.read kapsami gerekiyor.", "code": "MISSING_SCOPE"},
            )
        return _service(actor).describe(actor, lang=lang)

    @router.post("/ui/settings", summary="Ayar degisikligi oner")
    def propose_setting(
        change: SettingChange,
        actor: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Bir ayar degisikligi onerir.

        Uc kapidan gecer: izin listesi, uretim engeli mandali, iki kisi
        kurali. Sonuc doguran bir ayar dogrudan uygulanmaz; ikinci bir
        kimligin onayini bekler.
        """
        try:
            sonuc = _service(actor).propose(
                change.key, change.value, actor=actor, reason=change.reason
            )
        except (ConfigRefused, SettingError) as exc:
            raise _refused(exc) from None
        return asdict(sonuc)

    @router.post("/ui/settings/approve", summary="Bekleyen ayar degisikligini onayla")
    def approve_setting(
        action: ApprovalAction,
        actor: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        """Bekleyen bir degisikligi ikinci kimlikle onaylar.

        Oneren kisi onaylayamaz. Mandal onay aninda YENIDEN calisir: oneriyle
        onay arasinda baska bir ayar degismis ve kombinasyon guvensiz hale
        gelmis olabilir.
        """
        try:
            sonuc = _service(actor).approve(action.change_id, actor=actor)
        except (ConfigRefused, SettingError) as exc:
            raise _refused(exc) from None
        return asdict(sonuc)

    @router.post("/ui/settings/cancel", summary="Bekleyen ayar degisikligini geri cek")
    def cancel_setting(
        action: ApprovalAction,
        actor: ActorContext = Depends(actor_dependency),
    ) -> dict[str, Any]:
        try:
            sonuc = _service(actor).cancel(action.change_id, actor=actor)
        except (ConfigRefused, SettingError) as exc:
            raise _refused(exc) from None
        return asdict(sonuc)

    return router


def mount_assets(app: Any) -> bool:
    """Statik varliklari `/ui/assets` altina baglar. Dosyalar yoksa False doner.

    Kaynak agacindan calisirken dizin her zaman vardir; kurulu bir wheel'de
    paket verisi eksikse servis yine de acilmali, yalniz arayuz olmamalidir.
    """
    if not ASSETS_DIR.is_dir():
        log.warning("Arayuz varliklari bulunamadi (%s); /ui/assets baglanmadi.", ASSETS_DIR)
        return False
    app.mount("/ui/assets", StaticFiles(directory=str(ASSETS_DIR)), name="ui-assets")
    return True
