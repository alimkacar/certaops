"""API kimlik dogrulama ve actor cozumleme.

API'de kimlik dogrulama olmadan uretim profili baslatilamaz.
Desteklenen modlar:

  none          -> yalniz yerel gelistirme. `require_actor` uretim kontrolunde
                   reddeder; actor hicbir kapsam tasimaz.
  static_token  -> principals dosyasindaki token hash'i -> actor eslesmesi.
                   Token'lar dosyada sha256 olarak tutulur, duz metin degil.
  oidc          -> IAS/XSUAA JWT dogrulama (PyJWT + JWKS).

Rol -> kapsam donusumu `contracts.actor.ROLE_SCOPES` icindedir; principals
dosyasi kapsam degil **rol** ve organizasyon kapsami tanimlar. Boylece yetki
modeli tek yerden yonetilir.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..contracts import ORG_WILDCARD, ActorContext, unknown_roles

log = logging.getLogger(__name__)


class AuthenticationError(Exception):
    """Kimlik dogrulanamadi."""

    def __init__(self, message: str, *, code: str = "UNAUTHENTICATED") -> None:
        super().__init__(message)
        self.code = code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Principals dosyasindaki bir kayit."""

    subject: str
    tenant: str
    roles: tuple[str, ...]
    company_codes: tuple[str, ...] = (ORG_WILDCARD,)
    plants: tuple[str, ...] = (ORG_WILDCARD,)
    purchasing_orgs: tuple[str, ...] = (ORG_WILDCARD,)
    display_name: str = ""

    def to_actor(self, *, auth_method: str) -> ActorContext:
        return ActorContext(
            subject=self.subject,
            tenant=self.tenant,
            roles=tuple(r.upper() for r in self.roles),
            company_codes=frozenset(self.company_codes),
            plants=frozenset(self.plants),
            purchasing_orgs=frozenset(self.purchasing_orgs),
            auth_method=auth_method,
            display_name=self.display_name,
        )


def _principal_from_dict(
    raw: dict[str, Any], *, missing_is_wildcard: bool = True
) -> Principal:
    def org(key: str) -> tuple[str, ...]:
        value = raw.get(key)
        if value is None:
            # Uretimde eksik organizasyon tanimi wildcard'a donusmez:
            # principal dosyasinda kapsam acikca yazilmalidir.
            return (ORG_WILDCARD,) if missing_is_wildcard else ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(v) for v in value)

    roles = tuple(str(r).upper() for r in raw.get("roles", ()))
    missing = unknown_roles(roles)
    if missing:
        log.warning(
            "Principal %s bilinmeyen rol tasiyor (kapsam uretmez): %s",
            raw.get("subject", "?"),
            ", ".join(missing),
        )
    return Principal(
        subject=str(raw["subject"]),
        tenant=str(raw.get("tenant", "100")),
        roles=roles,
        company_codes=org("company_codes"),
        plants=org("plants"),
        purchasing_orgs=org("purchasing_orgs"),
        display_name=str(raw.get("display_name", "")),
    )


class StaticTokenAuthenticator:
    """Token hash'ine gore actor cozer.

    Principals dosyasi bicimi:
        {
          "principals": [
            {
              "token_sha256": "...",
              "subject": "purchaser@firma.com",
              "tenant": "100",
              "roles": ["PURCHASER"],
              "plants": ["1100"],
              "purchasing_orgs": ["1000"]
            }
          ]
        }
    """

    def __init__(self, principals_file: str | Path, *, missing_is_wildcard: bool = True) -> None:
        self.path = Path(principals_file)
        self._missing_is_wildcard = missing_is_wildcard
        self._lock = threading.Lock()
        self._by_hash: dict[str, Principal] = {}
        self._mtime = 0.0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise AuthenticationError(
                f"Principals dosyasi bulunamadi: {self.path}", code="PRINCIPALS_MISSING"
            )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        entries = raw.get("principals", [])
        mapping: dict[str, Principal] = {}
        for entry in entries:
            digest = str(entry.get("token_sha256", "")).strip().lower()
            if not digest:
                log.warning("Principal %s token_sha256 icermiyor, atlandi.", entry.get("subject"))
                continue
            mapping[digest] = _principal_from_dict(
                entry, missing_is_wildcard=self._missing_is_wildcard
            )
        self._by_hash = mapping
        self._mtime = self.path.stat().st_mtime
        log.info("%d principal yuklendi (%s)", len(mapping), self.path)

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            with self._lock:
                self._load()

    def authenticate(self, token: str) -> ActorContext:
        if not token:
            raise AuthenticationError("Bearer token yok.")
        self._reload_if_changed()
        digest = hash_token(token)
        # Sabit zamanli karsilastirma: hash tablosu lookup'i yeterli olsa da
        # eslesme bulunmadiginda da ayni maliyeti odemek icin dogrulama yapilir.
        for known, principal in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return principal.to_actor(auth_method="static_token")
        raise AuthenticationError("Token taninmiyor.", code="INVALID_TOKEN")


class OIDCAuthenticator:
    """IAS/XSUAA JWT dogrulama.

    PyJWT kuruluysa imza, issuer, audience ve expiry dogrulanir. Kurulu degilse
    token KABUL EDILMEZ: dogrulanmamis JWT'yi gecirmek kimlik dogrulamasi degildir.
    """

    def __init__(self, settings: Settings) -> None:
        security = settings.security
        self.issuer = security.oidc_issuer
        self.audience = security.oidc_audience
        self.jwks_url = security.oidc_jwks_url
        self.roles_claim = security.oidc_roles_claim
        self.default_tenant = settings.sap.tenant
        # Uretimde eksik organizasyon claim'i wildcard'a donusmez.
        self.missing_claim_is_wildcard = not settings.is_production
        self._jwk_client: Any = None
        try:
            import jwt  # type: ignore[import-not-found]

            self._jwt = jwt
            self._jwk_client = jwt.PyJWKClient(self.jwks_url) if self.jwks_url else None
        except ImportError:
            self._jwt = None
            log.error(
                "AGENT_AUTH_MODE=oidc icin PyJWT gerekli. `pip install pyjwt[crypto]` "
                "kurulmadan token dogrulanamaz."
            )

    def authenticate(self, token: str) -> ActorContext:
        if self._jwt is None:
            raise AuthenticationError(
                "OIDC dogrulama kullanilamiyor: PyJWT kurulu degil.", code="OIDC_UNAVAILABLE"
            )
        if self._jwk_client is None:
            raise AuthenticationError(
                "JWKS adresi yapilandirilmamis.", code="OIDC_MISCONFIGURED"
            )
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = self._jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience or None,
                issuer=self.issuer or None,
                options={"require": ["exp", "iss"]},
            )
        except Exception as exc:  # noqa: BLE001 - kutuphane cok cesitli hata tipi atar
            raise AuthenticationError(f"JWT dogrulanamadi: {exc}", code="INVALID_TOKEN") from exc

        subject = str(claims.get("sub") or claims.get("user_name") or "")
        if not subject:
            raise AuthenticationError("Token 'sub' claim'i icermiyor.", code="INVALID_TOKEN")

        raw_roles = claims.get(self.roles_claim) or []
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        # XSUAA scope'lari "xsapp!t1.PURCHASER" bicimindedir; son parca alinir.
        roles = tuple(str(r).split(".")[-1].upper() for r in raw_roles)

        return ActorContext(
            subject=subject,
            tenant=str(claims.get("zid") or claims.get("tenant") or self.default_tenant),
            roles=roles,
            company_codes=_claim_set(
                claims, "company_codes", missing_is_wildcard=self.missing_claim_is_wildcard
            ),
            plants=_claim_set(
                claims, "plants", missing_is_wildcard=self.missing_claim_is_wildcard
            ),
            purchasing_orgs=_claim_set(
                claims, "purchasing_orgs", missing_is_wildcard=self.missing_claim_is_wildcard
            ),
            auth_method="oidc",
            display_name=str(claims.get("given_name", "")),
        )


def _claim_set(
    claims: dict[str, Any], key: str, *, missing_is_wildcard: bool
) -> frozenset[str]:
    """Token claim'ini organizasyon kapsamina cevirir.

    Claim yoksa davranis calisma profiline baglidir:
      - gelistirme: wildcard (kolaylik)
      - uretim: **bos kume** = hicbir tesis/sirket kodu. Fail-closed.
    """
    value = claims.get(key)
    if value is None:
        return frozenset({ORG_WILDCARD}) if missing_is_wildcard else frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(v) for v in value)


class Authenticator:
    """Yapilandirilmis moda gore actor cozer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.mode = settings.security.auth_mode
        self._impl: Any = None
        if self.mode == "static_token":
            self._impl = StaticTokenAuthenticator(
                settings.security.principals_file,
                missing_is_wildcard=not settings.is_production,
            )
        elif self.mode == "oidc":
            self._impl = OIDCAuthenticator(settings)

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    def resolve(self, authorization_header: str | None) -> ActorContext:
        """Authorization basligindan actor uretir."""
        if self.mode == "none":
            # Kimlik dogrulama kapali: yerel operator kapsami verilir ve bu
            # durum health/telemetride uyari olarak gorunur.
            return ActorContext.local_operator(
                subject=self.settings.agent.local_subject,
                tenant=self.settings.sap.tenant,
                roles=self.settings.agent.local_roles,
            )

        token = _bearer_token(authorization_header)
        return self._impl.authenticate(token)


def _bearer_token(header: str | None) -> str:
    if not header:
        raise AuthenticationError("Authorization basligi yok.")
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError("Authorization basligi 'Bearer <token>' olmali.")
    return parts[1].strip()


# --- Rate limit -------------------------------------------------------------
class RateLimiter:
    """Actor bazli sabit pencereli rate limit (process-lokal).

    Coklu worker'da toplam limit worker sayisina bolunur; paylasilan sayim icin
    `SharedRateLimiter` kullanilir. Dagitik kurulumda Redis/API gateway tabanli
    bir uygulama ile degistirilebilir, arayuz aynidir.
    """

    def __init__(self, per_minute: int) -> None:
        self.limit = max(1, per_minute)
        self._buckets: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """(izin_var, kalan). Limit asilirsa (False, 0)."""
        now = time.time()
        window = int(now // 60)
        with self._lock:
            count, stored_window = self._buckets.get(key, (0, window))
            if stored_window != window:
                count, stored_window = 0, window
            if count >= self.limit:
                return False, 0
            self._buckets[key] = (count + 1, stored_window)
            return True, self.limit - count - 1

    def retry_after(self) -> int:
        return 60 - int(time.time() % 60)


class SharedRateLimiter(RateLimiter):
    """Worker'lar arasi paylasilan istek sayaci.

    Sayim durum veritabaninda tutulur; `BEGIN IMMEDIATE` sayesinde iki worker
    ayni pencerede ayni anahtari eszamanli artiramaz. Boylece yapilandirilan
    limit toplam limittir, worker basina degil.
    """

    def __init__(self, per_minute: int, db: Any) -> None:
        super().__init__(per_minute)
        self._db = db

    def check(self, key: str) -> tuple[bool, int]:
        now = time.time()
        window = int(now // 60)
        with self._db.write() as conn:
            # Gecmis pencereleri temizle: tablo sinirsiz buyumesin.
            conn.execute("DELETE FROM rate_limit WHERE window_id < ?", (window - 2,))
            row = conn.execute(
                "SELECT hits FROM rate_limit WHERE bucket_key = ? AND window_id = ?",
                (key, window),
            ).fetchone()
            hits = int(row["hits"]) if row else 0
            if hits >= self.limit:
                return False, 0
            conn.execute(
                "INSERT INTO rate_limit (bucket_key, window_id, hits) VALUES (?,?,1) "
                "ON CONFLICT(bucket_key, window_id) DO UPDATE SET hits = hits + 1",
                (key, window),
            )
        return True, self.limit - hits - 1
