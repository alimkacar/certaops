"""Merkezi konfigurasyon. Tum ayarlar ortam degiskenlerinden okunur (.env destekli)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
    auth_mode: str = field(default_factory=lambda: _env("SAP_AUTH_MODE", "basic").lower())
    oauth_token_url: str = field(default_factory=lambda: _env("SAP_OAUTH_TOKEN_URL"))
    oauth_client_id: str = field(default_factory=lambda: _env("SAP_OAUTH_CLIENT_ID"))
    oauth_client_secret: str = field(default_factory=lambda: _env("SAP_OAUTH_CLIENT_SECRET"))
    oauth_scope: str = field(default_factory=lambda: _env("SAP_OAUTH_SCOPE"))
    destination_name: str = field(default_factory=lambda: _env("SAP_DESTINATION_NAME"))
    destination_service_url: str = field(default_factory=lambda: _env("SAP_DESTINATION_SERVICE_URL"))

    # OData surum tercihi: released V4 servisi varsa V4, yoksa V2'ye duser.
    odata_version: str = field(default_factory=lambda: _env("SAP_ODATA_VERSION", "auto").lower())
    page_size: int = field(default_factory=lambda: _env_int("SAP_PAGE_SIZE", 100))
    max_pages: int = field(default_factory=lambda: _env_int("SAP_MAX_PAGES", 10))

    company_code: str = field(default_factory=lambda: _env("SAP_COMPANY_CODE", "1000"))
    plant: str = field(default_factory=lambda: _env("SAP_PLANT", "1100"))
    purch_org: str = field(default_factory=lambda: _env("SAP_PURCH_ORG", "1000"))
    purch_group: str = field(default_factory=lambda: _env("SAP_PURCH_GROUP", "R01"))
    currency: str = field(default_factory=lambda: _env("SAP_CURRENCY", "EUR"))
    storage_location: str = field(default_factory=lambda: _env("SAP_STORAGE_LOCATION", "0001"))

    dry_run: bool = field(default_factory=lambda: _env_bool("SAP_DRY_RUN", True))
    approval_threshold: float = field(
        default_factory=lambda: _env_float("APPROVAL_THRESHOLD", 25_000.0)
    )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.backend not in {"mock", "odata"}:
            problems.append(f"SAP_BACKEND '{self.backend}' gecersiz. 'mock' veya 'odata' olmali.")
        if self.auth_mode not in {"basic", "oauth2", "destination"}:
            problems.append(
                f"SAP_AUTH_MODE '{self.auth_mode}' gecersiz. basic/oauth2/destination olmali."
            )
        if self.odata_version not in {"auto", "v2", "v4"}:
            problems.append(f"SAP_ODATA_VERSION '{self.odata_version}' gecersiz. auto/v2/v4 olmali.")
        if self.backend == "odata":
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
    artifact_ttl_hours: int = field(default_factory=lambda: _env_int("AGENT_ARTIFACT_TTL_HOURS", 24))
    # Periyodik saklama temizligi. 0 -> job kapali (yalniz gelistirme).
    retention_sweep_seconds: int = field(
        default_factory=lambda: _env_int("AGENT_RETENTION_SWEEP_SECONDS", 900)
    )
    data_policy_file: str = field(default_factory=lambda: _env("AGENT_DATA_POLICY_FILE"))

    def validate(self) -> list[str]:
        if self.dlp_mode not in {"enforce", "report", "off"}:
            return [f"AGENT_DLP_MODE '{self.dlp_mode}' gecersiz. enforce/report/off olmali."]
        return []


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


@dataclass(frozen=True)
class AgentSettings:
    """Claude API ve agent dongusu ayarlari."""

    api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: _env("AGENT_MODEL", "claude-sonnet-5"))
    max_tokens: int = field(default_factory=lambda: _env_int("AGENT_MAX_TOKENS", 8000))
    temperature: float = field(default_factory=lambda: _env_float("AGENT_TEMPERATURE", 0.2))
    # Use-case bazli tool adimi siniri. Global 25 yerine akis bazinda sinir uygulanir.
    max_tool_iterations: int = field(
        default_factory=lambda: _env_int("AGENT_MAX_TOOL_ITERATIONS", 12)
    )
    iteration_limits: tuple[tuple[str, int], ...] = field(
        default_factory=lambda: _parse_iteration_limits(
            _env(
                "AGENT_ITERATION_LIMITS",
                "platform:4,master_data:5,procurement_read:7,procurement_write:8,project_finance:6,reporting:6",
            )
        )
    )
    # CLI/demo gibi yerel kanallarda kullanilan varsayilan actor.
    local_subject: str = field(default_factory=lambda: _env("AGENT_LOCAL_SUBJECT", "local-operator"))
    local_roles: tuple[str, ...] = field(
        default_factory=lambda: _env_tuple("AGENT_LOCAL_ROLES", "VIEWER,PURCHASER")
    )

    def iteration_limit(self, domain: str = "") -> int:
        for key, limit in self.iteration_limits:
            if key == domain:
                return limit
        return self.max_tool_iterations


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
    agent: AgentSettings = field(default_factory=AgentSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    state: StateSettings = field(default_factory=StateSettings)
    budget: TokenBudget = field(default_factory=TokenBudget)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
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
        if self.app_env not in {"development", "staging", "production"}:
            problems.append(
                f"APP_ENV '{self.app_env}' gecersiz. development/staging/production olmali."
            )
        if not self.agent.api_key:
            problems.append("ANTHROPIC_API_KEY tanimli degil.")
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
        if self.sap.backend == "odata":
            if not self.security.allowed_sap_hosts:
                blockers.append(
                    "SAP_BACKEND=odata iken SAP_ALLOWED_HOSTS bos olamaz (egress allowlist)."
                )
            if self.sap.auth_mode == "basic":
                blockers.append(
                    "SAP_AUTH_MODE=basic uretimde kullanilamaz. "
                    "oauth2 veya destination kullanin."
                )
            if not self.sap.verify_ssl:
                blockers.append("SAP_VERIFY_SSL=false uretimde kabul edilemez.")
        if not self.sap.dry_run and self.security.approval_gateway == "local":
            blockers.append(
                "SAP_DRY_RUN=false iken AGENT_APPROVAL_GATEWAY=local olamaz: gercek yazma "
                "dogrulanmis bir onay gecidi (bpa) gerektirir."
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
                "AGENT_KMS_KEY_ID tanimli degil: evidence/session sifreleme anahtari "
                "kaynak kodda veya .env'de tutulamaz."
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
            "auth_mode": self.security.auth_mode,
            "sap_backend": self.sap.backend,
            "sap_auth_mode": self.sap.auth_mode,
            "dry_run": self.sap.dry_run,
            "approval_gateway": self.security.approval_gateway,
            "session_backend": self.state.session_backend,
            "egress_allowlist": list(self.security.allowed_sap_hosts),
            "dlp_mode": self.privacy.dlp_mode,
            "risk_scoring_mode": self.risk.scoring_mode,
            "cache_backend": self.cache.backend,
            "strict_unknown_fields": self.privacy.strict_unknown_fields or self.is_production,
            "production_blockers": blockers,
            "production_ready": not blockers,
        }


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Singleton ayar nesnesi."""
    global _settings
    if _settings is None or reload:
        if reload:
            load_dotenv(override=True)
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def setup_logging(level: str | None = None) -> logging.Logger:
    """Tek noktadan logging kurulumu. Gurultulu kutuphaneler susturulur."""
    resolved = (level or get_settings().log_level).upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "pdfminer", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("robotics_agent")
