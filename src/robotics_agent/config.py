"""Merkezi konfigurasyon. Tum ayarlar ortam degiskenlerinden okunur (.env destekli)."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Sira onemli. `load_dotenv()` `.env` degerlerini `os.environ`e yaziyor;
# ondan SONRA bakildiginda bir anahtarin kabuktan mi yoksa `.env`den mi
# geldigi anlasilamaz. Ayrim guvenlik acisindan tasiyici: yalniz gercekten
# dis ortamdan gelen bir anahtar, arayuzden yapilan bir degisikligi yener.
# Bu yuzden anlik goruntu once alinir.
from .runtime_config.store import apply_overrides, snapshot_process_env

snapshot_process_env()
load_dotenv()
# Arayuzden yapilmis ayar degisiklikleri `.env`in uzerine, dis ortamin
# altina uygulanir. Izin listesi disindaki hicbir anahtar buradan gecemez.
apply_overrides()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "evet", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(_env(key) or default))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_tuple(key: str, default: str = "") -> tuple[str, ...]:
    """Virgul ile ayrilmis listeyi okur. Bos deger bos demet dondurur."""
    raw = _env(key, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _env_path(key: str, default: str) -> Path:
    return Path(_env(key, default)).expanduser().resolve()


#: Bir tool handler'inin calisabilecegi en uzun sure. Kayitli hicbir tool
#: `timeout_s` olarak bunu asamaz (registry kayit sirasinda reddeder), bu yuzden
#: oturum lease'i bu degeri ust sinir olarak kabul edebilir.
TOOL_TIMEOUT_CEILING_SECONDS: float = 120.0

#: Gercek bir SAP sistemine baglanan backend'ler. `mock` disaridadir.
#: Uretim profili ve baglanti dogrulamasi bu kumeye gore karar verir; tek
#: kaynak olmasi yeni backend eklendiginde kapinin atlanmasini engeller.
_LIVE_SAP_BACKENDS: frozenset[str] = frozenset({"odata", "ecc"})


@dataclass(frozen=True)
class SAPSettings:
    """SAP baglanti ve organizasyon ayarlari."""

    backend: str = field(default_factory=lambda: _env("SAP_BACKEND", "mock").lower())
    base_url: str = field(default_factory=lambda: _env("SAP_BASE_URL").rstrip("/"))
    client: str = field(default_factory=lambda: _env("SAP_CLIENT", "100"))
    username: str = field(default_factory=lambda: _env("SAP_USERNAME"))
    password: str = field(default_factory=lambda: _env("SAP_PASSWORD"))
    verify_ssl: bool = field(default_factory=lambda: _env_bool("SAP_VERIFY_SSL", True))
    timeout: int = field(default_factory=lambda: _env_int("SAP_TIMEOUT", 45))

    # Sistem kimligi: audit ve evidence kayitlarinda hangi sistemin okundugunu belirler.
    system_alias: str = field(default_factory=lambda: _env("SAP_SYSTEM_ALIAS", "S4-MOCK"))
    tenant: str = field(default_factory=lambda: _env("SAP_TENANT", "100"))

    # basic  -> kullanici/parola (yalniz gelistirme)
    # oauth2 -> client credentials token akisi
    # destination -> SAP BTP Destination servisi uzerinden cozumleme
    # apikey -> SAP API Business Hub sandbox (sandbox.api.sap.com). SALT OKUNUR
    #           bir SAP sistemine karsi kontrat dogrulamak icindir; uretimde
    #           kullanilamaz.
    auth_mode: str = field(default_factory=lambda: _env("SAP_AUTH_MODE", "basic").lower())
    api_key: str = field(default_factory=lambda: _env("SAP_API_KEY"))
    api_key_header: str = field(default_factory=lambda: _env("SAP_API_KEY_HEADER", "APIKey"))
    oauth_token_url: str = field(default_factory=lambda: _env("SAP_OAUTH_TOKEN_URL"))
    oauth_client_id: str = field(default_factory=lambda: _env("SAP_OAUTH_CLIENT_ID"))
    oauth_client_secret: str = field(default_factory=lambda: _env("SAP_OAUTH_CLIENT_SECRET"))
    oauth_scope: str = field(default_factory=lambda: _env("SAP_OAUTH_SCOPE"))
    destination_name: str = field(default_factory=lambda: _env("SAP_DESTINATION_NAME"))
    destination_service_url: str = field(default_factory=lambda: _env("SAP_DESTINATION_SERVICE_URL"))

    # --- Cloud Connector (ProxyType=OnPremise) ------------------------------
    # On-premise bir destination'a BTP'den DOGRUDAN erisilemez; trafik
    # connectivity proxy uzerinden gecer. Bos birakilirsa on-premise
    # destination acik hata verir (sessiz timeout yerine).
    connectivity_proxy_url: str = field(
        default_factory=lambda: _env("SAP_CONNECTIVITY_PROXY_URL")
    )
    connectivity_token_url: str = field(
        default_factory=lambda: _env("SAP_CONNECTIVITY_TOKEN_URL")
    )
    connectivity_client_id: str = field(
        default_factory=lambda: _env("SAP_CONNECTIVITY_CLIENT_ID")
    )
    connectivity_client_secret: str = field(
        default_factory=lambda: _env("SAP_CONNECTIVITY_CLIENT_SECRET")
    )
    cloud_connector_location_id: str = field(
        default_factory=lambda: _env("SAP_CLOUD_CONNECTOR_LOCATION_ID")
    )

    # OData surum tercihi: released V4 servisi varsa V4, yoksa V2'ye duser.
    odata_version: str = field(default_factory=lambda: _env("SAP_ODATA_VERSION", "auto").lower())
    page_size: int = field(default_factory=lambda: _env_int("SAP_PAGE_SIZE", 100))
    max_pages: int = field(default_factory=lambda: _env_int("SAP_MAX_PAGES", 10))

    company_code: str = field(default_factory=lambda: _env("SAP_COMPANY_CODE", "1000"))
    plant: str = field(default_factory=lambda: _env("SAP_PLANT", "1100"))
    purch_org: str = field(default_factory=lambda: _env("SAP_PURCH_ORG", "1000"))
    purch_group: str = field(default_factory=lambda: _env("SAP_PURCH_GROUP", "R01"))
    currency: str = field(default_factory=lambda: _env("SAP_CURRENCY", "EUR"))
    # Malzeme aciklamasinda tercih edilen dil. Kodda sabit "TR" idi; farkli
    # dilde veri tasiyan bir sistemde (ornegin API Hub sandbox'i) aciklama
    # sessizce bos ya da yanlis dilde gelirdi.
    description_language: str = field(
        default_factory=lambda: _env("SAP_DESCRIPTION_LANGUAGE", "TR").upper()
    )
    storage_location: str = field(default_factory=lambda: _env("SAP_STORAGE_LOCATION", "0001"))

    # Ilk urun profili S/4HANA Public Edition read-only'dir. Bu bayrak
    # SAP_DRY_RUN'dan daha gucludur: mutating tool'lari gorunmez yapar,
    # policy'de reddeder ve OData HTTP katmaninda POST/PATCH/PUT/DELETE'i
    # durdurur. Gelecekteki write paketinin testleri bunu acikca false yapar.
    read_only: bool = field(default_factory=lambda: _env_bool("SAP_READ_ONLY", True))
    dry_run: bool = field(default_factory=lambda: _env_bool("SAP_DRY_RUN", True))
    approval_threshold: float = field(
        default_factory=lambda: _env_float("APPROVAL_THRESHOLD", 25_000.0)
    )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.backend not in ({"mock"} | _LIVE_SAP_BACKENDS):
            problems.append(
                f"SAP_BACKEND '{self.backend}' gecersiz. "
                "'mock', 'odata' (S/4HANA) veya 'ecc' (ECC 6.0 EHP8) olmali."
            )
        if self.auth_mode not in {"basic", "oauth2", "destination", "apikey"}:
            problems.append(
                f"SAP_AUTH_MODE '{self.auth_mode}' gecersiz. "
                "basic/oauth2/destination/apikey olmali."
            )
        if self.odata_version not in {"auto", "v2", "v4"}:
            problems.append(f"SAP_ODATA_VERSION '{self.odata_version}' gecersiz. auto/v2/v4 olmali.")
        if not 1 <= self.timeout <= 120:
            problems.append("SAP_TIMEOUT 1-120 saniye araliginda olmali.")
        if not 1 <= self.page_size <= 500:
            problems.append("SAP_PAGE_SIZE 1-500 araliginda olmali.")
        if not 1 <= self.max_pages <= 20:
            problems.append("SAP_MAX_PAGES 1-20 araliginda olmali.")
        # ECC 6.0 EHP8'de OData V4 yoktur: RAP ABAP 7.53+ ister, EHP8 7.50'dir.
        # Yanlis konfigurasyon calisma zamaninda 404 olarak degil, burada patlar.
        if self.backend == "ecc" and self.odata_version == "v4":
            problems.append(
                "SAP_ODATA_VERSION=v4 ECC backend'i ile kullanilamaz. "
                "ECC 6.0 EHP8 (NetWeaver 7.50) yalniz OData V2 destekler; 'v2' veya "
                "'auto' kullanin."
            )
        # Gercek SAP baglantisi isteyen backend'ler ayni baglanti sozlesmesini paylasir.
        if self.backend in _LIVE_SAP_BACKENDS:
            if not self.base_url and self.auth_mode != "destination":
                problems.append("SAP_BASE_URL bos (SAP_BACKEND=odata icin zorunlu).")
            if self.auth_mode == "basic" and (not self.username or not self.password):
                problems.append(
                    "SAP_USERNAME / SAP_PASSWORD bos (SAP_AUTH_MODE=basic icin zorunlu)."
                )
            if self.auth_mode == "oauth2" and not (
                self.oauth_token_url and self.oauth_client_id and self.oauth_client_secret
            ):
                problems.append(
                    "SAP_OAUTH_TOKEN_URL / SAP_OAUTH_CLIENT_ID / SAP_OAUTH_CLIENT_SECRET eksik."
                )
            if self.auth_mode == "destination" and not (
                self.destination_name and self.destination_service_url
            ):
                problems.append("SAP_DESTINATION_NAME / SAP_DESTINATION_SERVICE_URL eksik.")
            if self.auth_mode == "apikey" and not self.api_key:
                problems.append(
                    "SAP_API_KEY bos (SAP_AUTH_MODE=apikey icin zorunlu). "
                    "https://api.sap.com hesabinizdan alin."
                )
        return problems


@dataclass(frozen=True)
class SecuritySettings:
    """Kimlik dogrulama, yetkilendirme ve veri sinirlari."""

    # none         -> kimlik dogrulama yok (yalniz yerel gelistirme; API uretimde reddeder)
    # static_token -> principals dosyasindaki token -> actor eslesmesi
    # oidc         -> JWT dogrulama (IAS/XSUAA)
    auth_mode: str = field(default_factory=lambda: _env("AGENT_AUTH_MODE", "none").lower())
    principals_file: str = field(default_factory=lambda: _env("AGENT_PRINCIPALS_FILE"))
    oidc_issuer: str = field(default_factory=lambda: _env("AGENT_OIDC_ISSUER"))
    oidc_audience: str = field(default_factory=lambda: _env("AGENT_OIDC_AUDIENCE"))
    oidc_jwks_url: str = field(default_factory=lambda: _env("AGENT_OIDC_JWKS_URL"))
    oidc_roles_claim: str = field(default_factory=lambda: _env("AGENT_OIDC_ROLES_CLAIM", "groups"))
    # Operatorun yeniden dagitim yapmadan kapatabilecegi tool'lar. Olay aninda
    # tek bir yolu kesmek gerekir; `SAP_DRY_RUN` hepsini birden kapatir, bu ise
    # cerrahi mudahaledir. Kapatilan tool modele HIC gosterilmez ve cagrilirsa
    # policy kapisinda reddedilir - iki katman, cunku model listeyi gormese de
    # adi tahmin edebilir.
    disabled_tools: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("AGENT_DISABLED_TOOLS", "")
    )

    rate_limit_per_minute: int = field(default_factory=lambda: _env_int("AGENT_RATE_LIMIT", 30))
    max_request_bytes: int = field(default_factory=lambda: _env_int("AGENT_MAX_REQUEST_BYTES", 65_536))

    # Giden trafik allowlist'i: SSRF ve yanlis sisteme yazma riskini keser.
    allowed_sap_hosts: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("SAP_ALLOWED_HOSTS")
    )

    # R3/R4 yazma penceresi ("08:00-20:00"); bos ise pencere kontrolu yok.
    write_window: str = field(default_factory=lambda: _env("AGENT_WRITE_WINDOW"))
    approval_ttl_minutes: int = field(default_factory=lambda: _env_int("AGENT_APPROVAL_TTL_MIN", 60))
    # Tool sonucu preview'lari API yanitinda maskelenir.
    mask_tool_previews: bool = field(default_factory=lambda: _env_bool("AGENT_MASK_PREVIEWS", True))

    # local -> onay kaydi yerel API/CLI uzerinden uretilir (gelistirme/pilot)
    # bpa   -> SAP Build Process Automation workflow'undan dogrulanir (uretim)
    approval_gateway: str = field(
        default_factory=lambda: _env("AGENT_APPROVAL_GATEWAY", "local").lower()
    )
    bpa_base_url: str = field(default_factory=lambda: _env("AGENT_BPA_BASE_URL"))
    bpa_definition_id: str = field(default_factory=lambda: _env("AGENT_BPA_DEFINITION_ID"))
    bpa_token_url: str = field(default_factory=lambda: _env("AGENT_BPA_TOKEN_URL"))
    bpa_client_id: str = field(default_factory=lambda: _env("AGENT_BPA_CLIENT_ID"))
    bpa_client_secret: str = field(default_factory=lambda: _env("AGENT_BPA_CLIENT_SECRET"))

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.auth_mode not in {"none", "static_token", "oidc"}:
            problems.append(
                f"AGENT_AUTH_MODE '{self.auth_mode}' gecersiz. none/static_token/oidc olmali."
            )
        if self.auth_mode == "static_token" and not self.principals_file:
            problems.append("AGENT_PRINCIPALS_FILE bos (AGENT_AUTH_MODE=static_token icin zorunlu).")
        if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_jwks_url):
            problems.append("AGENT_OIDC_ISSUER / AGENT_OIDC_JWKS_URL eksik.")
        if self.approval_gateway not in {"local", "bpa"}:
            problems.append(
                f"AGENT_APPROVAL_GATEWAY '{self.approval_gateway}' gecersiz. local/bpa olmali."
            )
        if self.approval_gateway == "bpa" and not (
            self.bpa_base_url and self.bpa_definition_id and self.bpa_token_url
        ):
            problems.append(
                "AGENT_BPA_BASE_URL / AGENT_BPA_DEFINITION_ID / AGENT_BPA_TOKEN_URL eksik."
            )
        if self.write_window:
            try:
                start, end = self.write_window.split("-", 1)
                for part in (start, end):
                    hour, minute = part.strip().split(":")
                    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                        raise ValueError
            except ValueError:
                problems.append(
                    f"AGENT_WRITE_WINDOW '{self.write_window}' gecersiz. Ornek: 08:00-20:00"
                )
        return problems


@dataclass(frozen=True)
class StateSettings:
    """Kalici durum: oturum, onay, idempotency ve audit."""

    dir: Path = field(default_factory=lambda: _env_path("AGENT_STATE_DIR", "./state"))
    # memory -> yalniz surec icinde; sqlite -> restart ve coklu worker'a dayanikli
    session_backend: str = field(
        default_factory=lambda: _env("AGENT_SESSION_BACKEND", "sqlite").lower()
    )
    session_ttl_hours: int = field(default_factory=lambda: _env_int("AGENT_SESSION_TTL_HOURS", 24))
    max_sessions: int = field(default_factory=lambda: _env_int("AGENT_MAX_SESSIONS", 500))
    evidence_ttl_minutes: int = field(default_factory=lambda: _env_int("AGENT_EVIDENCE_TTL_MIN", 120))
    evidence_max_entries: int = field(default_factory=lambda: _env_int("AGENT_EVIDENCE_MAX", 500))
    # Audit zinciri checkpoint'lerinin yazilacagi harici (ideal olarak WORM)
    # hedef. Bos = disa aktarim kapali; zincir yine tutulur ama defterin
    # kendisi yeniden yazilirsa bunu kanitlayacak bagimsiz bir kopya olmaz.
    audit_checkpoint_path: str = field(
        default_factory=lambda: _env("AGENT_AUDIT_CHECKPOINT_PATH", "")
    )
    audit_checkpoint_every: int = field(
        default_factory=lambda: _env_int("AGENT_AUDIT_CHECKPOINT_EVERY", 100)
    )

    @property
    def checkpoint_enabled(self) -> bool:
        """Harici checkpoint hedefi tanimli mi?"""
        return bool(str(self.audit_checkpoint_path).strip())

    @property
    def db_path(self) -> Path:
        return self.dir / "agent_state.sqlite3"

    @property
    def audit_mirror_path(self) -> Path:
        """Audit kayitlarinin okunabilir JSONL kopyasi (dis log toplayicilar icin).

        Zincirin dogruluk kaynagi `db_path` icindeki `audit_entries` tablosudur;
        bu dosya yalniz aynadir. `AGENT_AUDIT_MIRROR=false` ile kapatilabilir.
        """
        return self.dir / "audit_ledger.jsonl"

    @property
    def audit_mirror_enabled(self) -> bool:
        return _env_bool("AGENT_AUDIT_MIRROR", True)

    def ensure_dirs(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        if self.session_backend not in {"memory", "sqlite"}:
            return [
                f"AGENT_SESSION_BACKEND '{self.session_backend}' gecersiz. memory/sqlite olmali."
            ]
        return []


@dataclass(frozen=True)
class PrivacySettings:
    """DLP, alan erisimi, takma kimlik ve saklama ayarlari."""

    # enforce -> kararlar uygulanir | report -> yalniz bulgu | off -> gelistirme
    dlp_mode: str = field(default_factory=lambda: _env("AGENT_DLP_MODE", "enforce").lower())
    # Siniflandirilmamis alani D3 kabul et. Uretimde her zaman acik.
    strict_unknown_fields: bool = field(
        default_factory=lambda: _env_bool("AGENT_STRICT_UNKNOWN_FIELDS", False)
    )
    # Takma kimlik anahtari. Uretimde KMS/secret manager'dan gelmelidir.
    pseudonymization_key_id: str = field(
        default_factory=lambda: _env("AGENT_PSEUDONYMIZATION_KEY_ID")
    )
    kms_key_id: str = field(default_factory=lambda: _env("AGENT_KMS_KEY_ID"))
    evidence_encryption: bool = field(
        default_factory=lambda: _env_bool("AGENT_EVIDENCE_ENCRYPTION", False)
    )
    session_encryption: bool = field(
        default_factory=lambda: _env_bool("AGENT_SESSION_ENCRYPTION", False)
    )
    audit_encryption: bool = field(
        default_factory=lambda: _env_bool("AGENT_AUDIT_ENCRYPTION", False)
    )
    artifact_ttl_hours: int = field(default_factory=lambda: _env_int("AGENT_ARTIFACT_TTL_HOURS", 24))
    # Periyodik saklama temizligi. 0 -> job kapali (yalniz gelistirme).
    retention_sweep_seconds: int = field(
        default_factory=lambda: _env_int("AGENT_RETENTION_SWEEP_SECONDS", 900)
    )
    data_policy_file: str = field(default_factory=lambda: _env("AGENT_DATA_POLICY_FILE"))

    def validate(self) -> list[str]:
        if self.dlp_mode not in {"enforce", "report", "off"}:
            return [f"AGENT_DLP_MODE '{self.dlp_mode}' gecersiz. enforce/report/off olmali."]

        # Sifreleme ayarlari FAIL-CLOSED dogrulanir. Bu bayraklar uzun sure
        # hicbir yerde okunmuyordu: `true` yapan operator kanit ve oturum
        # kayitlarinin diskte sifreli durdugunu saniyordu. Artik acik ama
        # kurulamiyorsa surec baslamaz - "acik ama calismiyor" durumu yok.
        problems: list[str] = []
        if self.evidence_encryption or self.session_encryption or self.audit_encryption:
            from .security_at_rest import AtRestConfigError, load_key

            try:
                load_key()
            except AtRestConfigError as exc:
                problems.append(str(exc))
            else:
                try:
                    from .security_at_rest import _load_aesgcm

                    _load_aesgcm()
                except AtRestConfigError as exc:
                    problems.append(str(exc))
        return problems


@dataclass(frozen=True)
class CacheSettings:
    """Tenant ve yetki kapsamina duyarli guvenli okuma cache'i ayarlari."""

    # memory -> surec ici | none -> kapali. redis henuz desteklenmez.
    backend: str = field(default_factory=lambda: _env("AGENT_CACHE_BACKEND", "memory").lower())
    default_ttl_seconds: int = field(
        default_factory=lambda: _env_int("AGENT_CACHE_DEFAULT_TTL_SECONDS", 60)
    )
    max_entries_per_tenant: int = field(
        default_factory=lambda: _env_int("AGENT_CACHE_MAX_ENTRIES", 500)
    )
    # D3 cache'lenmez; bu bayrak yalniz "asla acilmasin" niyetini belgeler ve
    # yanlislikla true yapilirsa uretim kapisinda yakalanir.
    d3_enabled: bool = field(default_factory=lambda: _env_bool("AGENT_D3_CACHE_ENABLED", False))

    @property
    def enabled(self) -> bool:
        return self.backend not in {"none", "off", ""}

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.backend not in {"memory", "none", "off", "redis"}:
            problems.append(
                f"AGENT_CACHE_BACKEND '{self.backend}' gecersiz. memory/none olmali "
                "(redis henuz desteklenmiyor)."
            )
        if self.backend == "redis":
            problems.append(
                "AGENT_CACHE_BACKEND=redis henuz uygulanmadi; memory veya none kullanin."
            )
        return problems


@dataclass(frozen=True)
class RiskSettings:
    """Cagri aninda etki skoru ureten dinamik risk motoru ayarlari."""

    # enforce -> effective_tier uygulanir | report -> yalniz audit'e yazilir
    scoring_mode: str = field(
        default_factory=lambda: _env("AGENT_RISK_SCORING_MODE", "enforce").lower()
    )
    max_parallel_reads: int = field(default_factory=lambda: _env_int("AGENT_MAX_PARALLEL_READS", 4))

    @property
    def enforced(self) -> bool:
        return self.scoring_mode == "enforce"

    def validate(self) -> list[str]:
        if self.scoring_mode not in {"enforce", "report"}:
            return [
                f"AGENT_RISK_SCORING_MODE '{self.scoring_mode}' gecersiz. enforce/report olmali."
            ]
        return []


@dataclass(frozen=True)
class TokenBudget:
    """Sema, tool sonucu ve nihai yanit icin token guardrail'leri."""

    schema_tokens_per_turn: int = field(default_factory=lambda: _env_int("BUDGET_SCHEMA_TOKENS", 3000))
    single_result_tokens: int = field(default_factory=lambda: _env_int("BUDGET_RESULT_TOKENS", 1200))
    turn_result_tokens: int = field(default_factory=lambda: _env_int("BUDGET_TURN_RESULT_TOKENS", 6000))
    answer_tokens: int = field(default_factory=lambda: _env_int("BUDGET_ANSWER_TOKENS", 1200))
    keep_full_results: int = field(default_factory=lambda: _env_int("BUDGET_KEEP_FULL_RESULTS", 6))
    # Zaman asimindan sonra arka planda calismaya devam eden tool thread'i
    # ust siniri. Asilirsa yeni cagri baslatilmaz ve acik hata donulur.
    # 0 = sinirsiz (onerilmez; uzun omurlu serviste thread birikir).
    max_abandoned_tool_threads: int = field(
        default_factory=lambda: _env_int("BUDGET_MAX_ABANDONED_THREADS", 16)
    )


#: Desteklenen model saglayicilari. `fake` yalniz testler icindir.
_MODEL_PROVIDERS: frozenset[str] = frozenset({"gemini", "anthropic", "fake"})
_THINKING_LEVELS: frozenset[str] = frozenset({"minimal", "low", "medium", "high"})


@dataclass(frozen=True)
class ModelSettings:
    """Saglayici-bagimsiz model ayarlari.

    Saglayici secimi konfigurasyondadir; core runtime hicbir SDK tipini
    bilmez. Yeni bir saglayici eklemek buraya bir anahtar eklemek demektir.
    """

    provider: str = field(default_factory=lambda: _env("MODEL_PROVIDER", "gemini").lower())
    name: str = field(default_factory=lambda: _env("MODEL_NAME", "gemini-3.7-flash"))
    timeout_s: float = field(default_factory=lambda: _env_float("MODEL_TIMEOUT", 90.0))
    max_retries: int = field(default_factory=lambda: _env_int("MODEL_MAX_RETRIES", 2))

    # --- Gemini -------------------------------------------------------------
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    # developer -> Gemini Developer API | vertex -> Google Cloud Vertex AI
    gemini_backend: str = field(
        default_factory=lambda: _env("GEMINI_BACKEND", "developer").lower()
    )
    google_cloud_project: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT"))
    google_cloud_location: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_LOCATION"))
    # Taban muhakeme seviyesi. Runtime istegin karmasikligina gore yukseltir.
    thinking_level: str = field(
        default_factory=lambda: _env("GEMINI_THINKING_LEVEL", "low").lower()
    )
    # `high` maliyetlidir ve gecikmeyi buyutur: yalniz acik konfigurasyonla.
    allow_high_thinking: bool = field(
        default_factory=lambda: _env_bool("GEMINI_ALLOW_HIGH_THINKING", False)
    )
    # Saglayicinin istek/yaniti saklamasi. SAP verisi icin varsayilan KAPALI.
    store_interactions: bool = field(
        default_factory=lambda: _env_bool("GEMINI_STORE_INTERACTIONS", False)
    )

    # --- Anthropic (opsiyonel) ---------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))

    @property
    def configured(self) -> bool:
        """Saglayici gercekten cagrilabilir durumda mi?"""
        if self.provider == "gemini":
            if self.gemini_backend == "vertex":
                return bool(self.google_cloud_project and self.google_cloud_location)
            return bool(self.gemini_api_key)
        if self.provider == "anthropic":
            return bool(self.anthropic_api_key)
        return True

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.provider not in _MODEL_PROVIDERS:
            problems.append(
                f"MODEL_PROVIDER '{self.provider}' gecersiz "
                f"({', '.join(sorted(_MODEL_PROVIDERS))})."
            )
        if not self.name:
            problems.append("MODEL_NAME bos olamaz.")
        if self.thinking_level not in _THINKING_LEVELS:
            problems.append(
                f"GEMINI_THINKING_LEVEL '{self.thinking_level}' gecersiz "
                f"({', '.join(sorted(_THINKING_LEVELS))})."
            )
        if self.thinking_level == "high" and not self.allow_high_thinking:
            problems.append(
                "GEMINI_THINKING_LEVEL=high icin GEMINI_ALLOW_HIGH_THINKING=true "
                "gerekir (maliyet ve gecikme bilincli bir karardir)."
            )
        if self.provider == "gemini":
            if self.gemini_backend not in {"developer", "vertex"}:
                problems.append(
                    f"GEMINI_BACKEND '{self.gemini_backend}' gecersiz "
                    "(developer/vertex)."
                )
            elif self.gemini_backend == "vertex" and not (
                self.google_cloud_project and self.google_cloud_location
            ):
                problems.append(
                    "GEMINI_BACKEND=vertex icin GOOGLE_CLOUD_PROJECT ve "
                    "GOOGLE_CLOUD_LOCATION zorunludur."
                )
            elif self.gemini_backend == "developer" and not self.gemini_api_key:
                problems.append("GEMINI_API_KEY tanimli degil.")
        if self.provider == "anthropic" and not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY tanimli degil (MODEL_PROVIDER=anthropic).")
        return problems

    def describe(self) -> dict[str, object]:
        """Health ciktisi. **API anahtari asla yer almaz.**"""
        payload: dict[str, object] = {
            "provider": self.provider,
            "model": self.name,
            "configured": self.configured,
            "thinking_level": self.thinking_level,
            "store_interactions": self.store_interactions,
        }
        if self.provider == "gemini":
            payload["backend"] = self.gemini_backend
            if self.gemini_backend == "vertex":
                payload["location"] = self.google_cloud_location
        return payload


@dataclass(frozen=True)
class AgentSettings:
    """Agent dongusu ayarlari (saglayicidan bagimsiz)."""

    max_tokens: int = field(default_factory=lambda: _env_int("AGENT_MAX_TOKENS", 8000))
    # Use-case bazli tool adimi siniri. Global 25 yerine akis bazinda sinir uygulanir.
    max_tool_iterations: int = field(
        default_factory=lambda: _env_int("AGENT_MAX_TOOL_ITERATIONS", 12)
    )
    iteration_limits: tuple[tuple[str, int], ...] = field(
        default_factory=lambda: _parse_iteration_limits(
            _env(
                "AGENT_ITERATION_LIMITS",
                "platform:4,master_data:5,procurement_read:7,procurement_write:8,p2p_finance:6,reporting:6",
            )
        )
    )
    # CLI/demo gibi yerel kanallarda kullanilan varsayilan actor.
    local_subject: str = field(default_factory=lambda: _env("AGENT_LOCAL_SUBJECT", "local-operator"))
    local_roles: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("AGENT_LOCAL_ROLES", "VIEWER,PURCHASER")
    )
    # Modelin katki saglamayacagi sorularda yaniti LLM'e hic gondermeden
    # yerel olarak uretir (bkz. core.direct). Gizlilik + gecikme kazanci.
    # Kapatildiginda her yanit klasik LLM akisindan gecer.
    direct_answers_enabled: bool = field(
        default_factory=lambda: _env_bool("AGENT_DIRECT_ANSWERS", True)
    )
    # Muhakeme kademelendirmesinin tabani. Saglayiciya `thinking_level` olarak
    # gider; saglayici desteklemiyorsa yok sayilir.
    gemini_thinking_level: str = field(
        default_factory=lambda: _env("GEMINI_THINKING_LEVEL", "low").lower()
    )
    # Pack bazli yukseltmeler. Yalniz YUKSELTIR (bkz. reasoning_level).
    reasoning_levels: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: _parse_reasoning_levels(
            _env(
                "AGENT_REASONING_LEVELS",
                "procurement_write:medium,p2p_finance:medium",
            )
        )
    )

    def iteration_limit(self, domain: str = "") -> int:
        for key, limit in self.iteration_limits:
            if key == domain:
                return limit
        return self.max_tool_iterations

    def reasoning_level(self, packs: Iterable[str] = ()) -> str:
        """Bu turda acilan pack'lere gore muhakeme seviyesi.

        Kural **tek yonludur**: override yalnizca seviyeyi YUKSELTEBILIR.
        Yapilandirilmis taban asagi cekilemez, cunku yanlis yazilmis bir
        override bir yazma yolunu az dusunulmus birakabilirdi. Birden fazla
        pack acildiysa en yuksegi kazanir; taninmayan pack ve gecersiz
        seviye degeri sessizce yok sayilir.
        """
        base = self.gemini_thinking_level
        if base not in _REASONING_ORDER:
            base = "low"
        best = _REASONING_ORDER.index(base)
        overrides = dict(self.reasoning_levels)
        for pack in packs:
            level = overrides.get(pack)
            if level not in _REASONING_ORDER:
                continue
            best = max(best, _REASONING_ORDER.index(level))
        return _REASONING_ORDER[best]


#: Muhakeme seviyeleri, dusukten yukseye. Sira karsilastirmayi tanimlar.
_REASONING_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high")


def _parse_reasoning_levels(raw: str) -> tuple[tuple[str, str], ...]:
    """`pack:seviye,pack:seviye` metnini ayristirir. Gecersiz girdi atlanir."""
    out: list[tuple[str, str]] = []
    for part in raw.split(","):
        if ":" not in part:
            continue
        key, _, level = part.partition(":")
        key, level = key.strip(), level.strip().lower()
        if key and level:
            out.append((key, level))
    return tuple(out)


def _parse_iteration_limits(raw: str) -> tuple[tuple[str, int], ...]:
    out: list[tuple[str, int]] = []
    for part in raw.split(","):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        try:
            out.append((key.strip(), int(value.strip())))
        except ValueError:
            continue
    return tuple(out)


@dataclass(frozen=True)
class LoggingSettings:
    """Yapilandirilmis loglama.

    Log, `core.audit` defterinin yerine GECMEZ: audit "ne yapildi" sorusunun
    hukuki cevabidir, log teshis kaydidir. Ama teshis kaydi da hassas veri
    tasir; `mask` bu yuzden varsayilan olarak aciktir ve uretimde kapatilamaz.
    """

    # text -> insan okunur konsol satiri; json -> log toplayici icin tek satir
    log_format: str = field(default_factory=lambda: _env("LOG_FORMAT", "text").lower())
    # Maskeleme log'a YAZILMADAN once uygulanir. `log.exception` bir SAP yanit
    # govdesini traceback'e dokebilir; disk ve stdout guvenli bolge degildir.
    mask: bool = field(default_factory=lambda: _env_bool("LOG_MASK", True))
    # Arayuzun /logs ucunun okudugu dairesel tampon. 0 -> tampon kurulmaz.
    buffer_size: int = field(default_factory=lambda: _env_int("LOG_BUFFER_SIZE", 500))
    # Bos ise dosyaya yazilmaz (container'da stdout/stderr toplanir).
    file: str = field(default_factory=lambda: _env("LOG_FILE", ""))
    file_max_bytes: int = field(default_factory=lambda: _env_int("LOG_FILE_MAX_BYTES", 5_242_880))
    file_backup_count: int = field(default_factory=lambda: _env_int("LOG_FILE_BACKUP_COUNT", 3))

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.log_format not in {"text", "json"}:
            problems.append(f"LOG_FORMAT '{self.log_format}' gecersiz. text/json olmali.")
        if self.buffer_size < 0:
            problems.append("LOG_BUFFER_SIZE negatif olamaz.")
        if self.file and self.file_max_bytes < 1024:
            problems.append("LOG_FILE_MAX_BYTES en az 1024 olmali.")
        if self.file_backup_count < 0:
            problems.append("LOG_FILE_BACKUP_COUNT negatif olamaz.")
        return problems


@dataclass(frozen=True)
class UISettings:
    """Tarayici tabanli operator arayuzu.

    Arayuz API ile **ayni origin**den servis edilir: CORS acilmaz, token
    baska bir origin'e gonderilmez. Arayuz kendi yetkisi olan bir kanal
    degildir; her istek ayni `Authorization` kapisindan ve ayni kapsam
    kontrollerinden gecer.

    Varsayilan olarak uretim disinda aciktir: uretimde arayuzu acmak bilincli
    bir karar olmalidir.
    """

    enabled: bool = field(
        default_factory=lambda: _env_bool(
            "AGENT_UI_ENABLED", _env("APP_ENV", "development").lower() != "production"
        )
    )
    # Canli log gorunumu. Loglar maskelenmis olsa da operator ekraninda sunucu
    # ic durumunu gosterir; ayrica kapatilabilir olmasi gerekir.
    log_stream_enabled: bool = field(
        default_factory=lambda: _env_bool("AGENT_UI_LOG_STREAM", True)
    )
    #: Tek istekte donen azami kayit sayisi (log ve audit icin ortak tavan).
    max_page_size: int = field(default_factory=lambda: _env_int("AGENT_UI_MAX_PAGE_SIZE", 200))
    #: `/chat/stream` (SSE) ucu. Toplam sureyi DEGISTIRMEZ; yalniz ilk
    #: karakterin gorunme suresini kisaltir - uzun yanitlarda algilanan
    #: gecikme belirgin olcude duser.
    #:
    #: Varsayilan KAPALI. Akan metin, tek seferlik yolun `sanitize_for_client`
    #: kapisindan farkli olarak parca parca temizlenmek zorundadir
    #: (`privacy.StreamSanitizer` bunu sinir-guvenli yapar). Yeni bir yol
    #: acmak bilincli bir karar olmali; bayrak bu karari gorunur kilar.
    stream_enabled: bool = field(
        default_factory=lambda: _env_bool("AGENT_UI_STREAM", False)
    )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.max_page_size < 1:
            problems.append("AGENT_UI_MAX_PAGE_SIZE en az 1 olmali.")
        return problems


class UnsafeProductionConfig(RuntimeError):
    """Uretim profilinde guvensiz yapilandirma tespit edildi.

    Servis baslamaz. Yanlis bir deployment ayarinin sessizce gercek SAP yazmasi
    yapmasindansa uygulamanin hic acilmamasi tercih edilir.
    """

    def __init__(self, problems: list[str]) -> None:
        detail = "\n  - ".join(problems)
        super().__init__(
            "Uretim profili (APP_ENV=production) guvenlik kapilarindan gecemedi:\n  - "
            + detail
        )
        self.problems = problems


@dataclass(frozen=True)
class Settings:
    sap: SAPSettings = field(default_factory=SAPSettings)
    model: ModelSettings = field(default_factory=ModelSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    state: StateSettings = field(default_factory=StateSettings)
    budget: TokenBudget = field(default_factory=TokenBudget)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    ui: UISettings = field(default_factory=UISettings)
    # development | staging | production
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development").lower())
    output_dir: Path = field(
        default_factory=lambda: Path(_env("OUTPUT_DIR", "./output")).expanduser().resolve()
    )
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state.ensure_dirs()

    def validate(self) -> list[str]:
        problems = list(self.sap.validate())
        problems += self.security.validate()
        problems += self.state.validate()
        problems += self.privacy.validate()
        problems += self.cache.validate()
        problems += self.risk.validate()
        problems += self.logging.validate()
        problems += self.ui.validate()
        if self.app_env not in {"development", "staging", "production"}:
            problems.append(
                f"APP_ENV '{self.app_env}' gecersiz. development/staging/production olmali."
            )
        problems.extend(self.model.validate())
        return problems

    # --- Uretim kapisi ------------------------------------------------------
    def production_blockers(self) -> list[str]:
        """Uretimde servisi durdurmasi gereken yapilandirma kombinasyonlari.

        Bu liste bosalmadan `APP_ENV=production` ile servis baslatilamaz.
        Gelistirme/staging profilinde ayni kosullar yalnizca uyari uretir.
        """
        blockers: list[str] = []

        if self.security.auth_mode == "none":
            blockers.append(
                "AGENT_AUTH_MODE=none: API kimlik dogrulamasi kapali. "
                "static_token veya oidc kullanin."
            )
        if self.security.auth_mode == "oidc" and not (
            self.security.oidc_issuer
            and self.security.oidc_jwks_url
            and self.security.oidc_audience
        ):
            blockers.append(
                "OIDC modunda AGENT_OIDC_ISSUER, AGENT_OIDC_JWKS_URL ve "
                "AGENT_OIDC_AUDIENCE zorunludur."
            )
        if self.sap.backend == "mock":
            # Uretimde simulasyon backend'i **hicbir kosulda** kabul edilmez.
            # Yanlis bir deployment ayarinin uydurma SAP verisini gercekmis
            # gibi sunmasi, hatali veri gostermekten daha kotudur: kimse
            # yanlis oldugunu fark etmez.
            blockers.append(
                "SAP_BACKEND=mock uretimde kullanilamaz: simulasyon backend'i gercek "
                "olmayan SAP verisi uretir. SAP_BACKEND=odata kullanin."
            )
        # Gercek SAP baglantisi kuran her backend ayni kapidan gecer. Yeni bir
        # backend eklendiginde bu kume genisletilmezse (ornegin `ecc`), guvensiz
        # yapilandirma uretimde sessizce kabul edilir.
        if self.sap.backend in _LIVE_SAP_BACKENDS:
            if not self.security.allowed_sap_hosts:
                blockers.append(
                    "SAP_BACKEND=odata iken SAP_ALLOWED_HOSTS bos olamaz (egress allowlist)."
                )
            if self.sap.auth_mode == "basic":
                blockers.append(
                    "SAP_AUTH_MODE=basic uretimde kullanilamaz. "
                    "oauth2 veya destination kullanin."
                )
            if self.sap.auth_mode == "apikey":
                # Sandbox salt okunurdur ve paylasimlidir: uretim verisi yok,
                # kullanici bazli yetki yok, denetlenebilir kimlik yok.
                blockers.append(
                    "SAP_AUTH_MODE=apikey yalniz SAP API Business Hub sandbox'i "
                    "icindir; uretimde kullanilamaz. oauth2 veya destination kullanin."
                )
            if not self.sap.verify_ssl:
                blockers.append("SAP_VERIFY_SSL=false uretimde kabul edilemez.")
        if not self.sap.dry_run and self.security.approval_gateway == "local":
            blockers.append(
                "SAP_DRY_RUN=false iken AGENT_APPROVAL_GATEWAY=local olamaz: gercek yazma "
                "dogrulanmis bir onay gecidi (bpa) gerektirir."
            )
        # Bu surumun uretim sozlesmesi read-only'dir. Write kodu gelecek paket
        # icin korunur ve test edilebilir, fakat bugunku build mutasyon acik
        # sekilde production-ready ilan edilemez.
        if not self.sap.read_only:
            blockers.append(
                "SAP_READ_ONLY=false: bu surum yalniz S/4HANA read-only profilinde "
                "uretime alinabilir. Write paketi henuz canli tenant kabulunu gecmedi."
            )
        if self.sap.read_only and not self.sap.dry_run:
            blockers.append(
                "SAP_READ_ONLY=true iken SAP_DRY_RUN=false celiskili: read-only uretim "
                "profilinde iki kilit de acik olmalidir."
            )
        # --- Model saglayici kapilari ---------------------------------------
        if self.model.provider == "fake":
            blockers.append(
                "MODEL_PROVIDER=fake yalnizca testler icindir; uretimde kullanilamaz."
            )
        if not self.model.configured:
            blockers.append(
                f"MODEL_PROVIDER={self.model.provider} yapilandirilmamis "
                "(API anahtari veya Vertex proje/lokasyon eksik)."
            )
        if self.model.store_interactions:
            blockers.append(
                "GEMINI_STORE_INTERACTIONS=true: SAP verisi saglayici tarafinda "
                "kalici olarak saklanamaz."
            )
        if self.model.provider == "gemini" and self.model.gemini_backend == "developer":
            blockers.append(
                "GEMINI_BACKEND=developer uretimde SAP verisi icin onerilmez; "
                "kurumsal veri isleme sozlesmesi icin GEMINI_BACKEND=vertex kullanin."
            )
        if self.state.session_backend == "memory":
            blockers.append(
                "AGENT_SESSION_BACKEND=memory kalici degildir; sqlite veya kurumsal "
                "veritabani kullanin."
            )

        # --- Gizlilik ve risk kapilari --------------------------------------
        if self.privacy.dlp_mode != "enforce":
            blockers.append(
                f"AGENT_DLP_MODE={self.privacy.dlp_mode}: uretimde DLP yalniz 'enforce' "
                "modunda calisabilir."
            )
        if not self.privacy.pseudonymization_key_id and not os.getenv(
            "AGENT_PSEUDONYMIZATION_SECRET"
        ):
            blockers.append(
                "AGENT_PSEUDONYMIZATION_KEY_ID (veya AGENT_PSEUDONYMIZATION_SECRET) tanimli "
                "degil: takma kimlikler her restart'ta degisir ve audit korelasyonu kirilir."
            )
        if not self.privacy.kms_key_id:
            blockers.append(
                "AGENT_KMS_KEY_ID tanimli degil: evidence/session/audit sifreleme anahtari "
                "kaynak kodda veya .env'de tutulamaz."
            )
        if not self.privacy.audit_encryption:
            blockers.append(
                "AGENT_AUDIT_ENCRYPTION=false: audit govdesi uretimde diske duz metin "
                "yazilamaz. AES-256-GCM sifrelemesini acin."
            )
        if self.privacy.retention_sweep_seconds <= 0:
            blockers.append(
                "AGENT_RETENTION_SWEEP_SECONDS=0: periyodik saklama temizligi kapali."
            )
        if self.cache.d3_enabled:
            blockers.append("AGENT_D3_CACHE_ENABLED=true olamaz: D3 veri cache'lenmez.")
        if not self.risk.enforced:
            blockers.append(
                f"AGENT_RISK_SCORING_MODE={self.risk.scoring_mode}: uretimde runtime risk "
                "skoru yalniz 'enforce' modunda calisabilir."
            )

        # --- Loglama ve arayuz kapilari -------------------------------------
        if not self.logging.mask:
            # Maskesiz log, hassas veriyi audit defterinin disina - dosyaya,
            # stdout'a ve log toplayiciya - kopyalar. Orada saklama politikasi
            # ve erisim kontrolu bizim degil, altyapinin elindedir.
            blockers.append(
                "LOG_MASK=false: uretimde log kayitlari maskelenmeden yazilamaz."
            )
        if self.ui.enabled and self.security.auth_mode == "none":
            # `AGENT_AUTH_MODE=none` zaten ayri bir blocker; bu kural onun
            # gevsetilmesi halinde bile arayuzun kimliksiz acilmasini engeller.
            blockers.append(
                "AGENT_UI_ENABLED=true iken AGENT_AUTH_MODE=none olamaz: operator "
                "arayuzu kimlik dogrulamasiz sunulamaz."
            )
        return blockers

    def enforce_production_profile(self) -> None:
        """Uretim profilinde guvensiz yapilandirmada exception firlatir."""
        if not self.is_production:
            return
        blockers = self.production_blockers()
        if blockers:
            raise UnsafeProductionConfig(blockers)

    def posture(self) -> dict[str, object]:
        """Guvenlik duruşu ozeti (health ve teshis ciktilari icin)."""
        blockers = self.production_blockers()
        return {
            "app_env": self.app_env,
            "model_provider": self.model.provider,
            "model_name": self.model.name,
            "auth_mode": self.security.auth_mode,
            "sap_backend": self.sap.backend,
            "sap_auth_mode": self.sap.auth_mode,
            "read_only": self.sap.read_only,
            "dry_run": self.sap.dry_run,
            "approval_gateway": self.security.approval_gateway,
            "session_backend": self.state.session_backend,
            "egress_allowlist": list(self.security.allowed_sap_hosts),
            "dlp_mode": self.privacy.dlp_mode,
            "risk_scoring_mode": self.risk.scoring_mode,
            "cache_backend": self.cache.backend,
            "strict_unknown_fields": self.privacy.strict_unknown_fields or self.is_production,
            "log_format": self.logging.log_format,
            "log_masking": self.logging.mask,
            "ui_enabled": self.ui.enabled,
            "production_blockers": blockers,
            "production_ready": not blockers,
        }


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Singleton ayar nesnesi."""
    global _settings
    if _settings is None or reload:
        if reload:
            # Calisan surecin acik ortam degiskenleri `.env` dosyasindan daha
            # yuksek onceliklidir. Aksi halde container/CI secret'lari ve acil
            # guvenlik kapilari reload sirasinda sessizce geri alinabilir.
            load_dotenv(override=False)
            # Reload'da da override'lar yeniden uygulanir; aksi halde
            # arayuzden yapilmis bir degisiklik ilk reload'da sessizce
            # kaybolurdu.
            apply_overrides()
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def setup_logging(
    level: str | None = None,
    *,
    log_format: str | None = None,
    log_file: str | None = None,
) -> logging.Logger:
    """Tek noktadan logging kurulumu.

    Ayarlari okur ve kurulumu `observability.logging.configure_logging`e
    devreder. Import dairesel olmasin diye modul burada, cagri aninda alinir:
    `observability` paketi `config`i degil, `config` `observability`yi bilir.

    Acik verilen argumanlar ortam degiskenlerini ezer (CLI bayraklari icin).
    """
    settings = get_settings()
    from .observability.logging import configure_logging

    return configure_logging(
        level=(level or settings.log_level).upper(),
        fmt=(log_format or settings.logging.log_format),
        mask=settings.logging.mask,
        buffer_size=settings.logging.buffer_size,
        log_file=(log_file if log_file is not None else settings.logging.file),
        file_max_bytes=settings.logging.file_max_bytes,
        file_backup_count=settings.logging.file_backup_count,
    )
