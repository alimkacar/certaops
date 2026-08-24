"""SAP BTP Destination cozumleme ve OAuth2 token yonetimi.

Uretimde SAP kimligi `.env` icindeki Basic kullanici parolasi olmamalidir.
Desteklenen modlar:

  basic       -> kullanici/parola (yalniz yerel gelistirme; uyari uretir)
  oauth2      -> client credentials; token onbelleklenir ve suresi dolmadan yenilenir
  destination -> BTP Destination servisinden URL + kimlik bilgisi cozumlenir
                 (Cloud Connector arkasindaki on-premise sistem dahil)

Secret'lar yalnizca bu modulde tutulur; modele ve loglara asla verilmez.

Iki tasarim notu:

1. **Destination token'i sabit bir header degildir.** Destination servisi
   `authTokens[].value` ile birlikte `expiresIn` doner. Token'i client
   header'ina bir kez gomup birakmak, uzun omurlu bir serviste sure dolunca
   her SAP cagrisinin 401 donmesi demektir; teshisi zordur cunku ilk saatler
   sorunsuz gecer. Bu yuzden destination modu da `token_provider` uretir ve
   saglayici sure dolmadan destination'i yeniden cozer.

2. **`ProxyType: OnPremise` bir bilgi etiketi degil, yonlendirme karari.**
   Cloud Connector arkasindaki sistem yalniz BTP connectivity proxy uzerinden
   erisilir; dogrudan istek ic aga hic ulasmaz. Proxy yapilandirilmamissa
   baglanti sessizce timeout'a dusmek yerine acik hata verir.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...config import SAPSettings
from .errors import SAPError

log = logging.getLogger(__name__)


@dataclass
class ResolvedConnection:
    """Bir SAP sistemine baglanmak icin gereken her sey."""

    base_url: str
    auth: httpx.Auth | None = None
    token_provider: Any = None  # Callable[[], str] | None
    headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = True
    proxy_type: str = "Internet"
    origin: str = "config"
    warnings: tuple[str, ...] = ()
    #: Cloud Connector arkasindaki sistem icin BTP connectivity proxy URL'i.
    proxy_url: str = ""
    #: Proxy'ye gonderilecek `Proxy-Authorization` degerini ureten saglayici.
    proxy_auth_provider: Callable[[], str] | None = None
    #: Coklu Cloud Connector kurulumunda hedef location.
    location_id: str = ""
    #: Destination'in kimlik dogrulama tipi. Principal propagation'i bundan
    #: anlariz: SAP yetkileri son kullaniciya mi yoksa teknik kullaniciya mi
    #: gore uygulaniyor?
    auth_type: str = ""

    #: SAP yetkilerini SON KULLANICIYA gore uygulayan kimlik akislari.
    PRINCIPAL_AUTH_TYPES = frozenset(
        {"PrincipalPropagation", "SAMLAssertion", "OAuth2SAMLBearerAssertion"}
    )

    @property
    def is_on_premise(self) -> bool:
        return self.proxy_type.lower() == "onpremise"

    @property
    def principal_propagation(self) -> bool:
        """SAP calisan kisiyi goruyor mu?

        False ise SAP'in KENDI denetim kaydinda yalnizca teknik kullanici
        gorunur; "bu belgeyi kim actirdi" sorusunun cevabi SAP tarafinda
        yoktur, yalnizca bizim audit zincirimizdedir. Yazma yollarinda bu
        onemli bir sinirlamadir ve gizlenmemelidir.
        """
        return self.auth_type in self.PRINCIPAL_AUTH_TYPES

    def describe(self) -> dict[str, Any]:
        """Secret icermeyen ozet (health/capability tool'lari icin)."""
        return {
            "base_url": self.base_url,
            "auth": "oauth2" if self.token_provider else ("basic" if self.auth else "none"),
            "proxy_type": self.proxy_type,
            "origin": self.origin,
            "verify_ssl": self.verify_ssl,
            "via_connectivity_proxy": bool(self.proxy_url),
            "location_id": self.location_id,
            "auth_type": self.auth_type,
            # SAP tarafinda islemin insana atfedilip atfedilemedigi.
            "principal_propagation": self.principal_propagation,
            "sap_attribution": "principal" if self.principal_propagation else "technical_user",
            "warnings": list(self.warnings),
        }


class OAuth2TokenProvider:
    """Client credentials token saglayici; suresi dolmadan yeniler."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str = "",
        client: httpx.Client | None = None,
        skew_seconds: int = 60,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._client = client or httpx.Client(timeout=20.0)
        self._skew = max(10, skew_seconds)
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - self._skew:
                return self._token
            data = {"grant_type": "client_credentials"}
            if self._scope:
                data["scope"] = self._scope
            response = self._client.post(
                self._token_url, data=data, auth=(self._client_id, self._client_secret)
            )
            if not response.is_success:
                raise SAPError(
                    f"OAuth2 token alinamadi (HTTP {response.status_code}).",
                    code="OAUTH_TOKEN_FAILED",
                    detail=self._token_url,
                )
            payload = response.json()
            self._token = str(payload.get("access_token", ""))
            if not self._token:
                raise SAPError(
                    "OAuth2 yanitinda access_token yok.", code="OAUTH_TOKEN_MISSING",
                    detail=self._token_url,
                )
            # `or 3600` kullanmak `expires_in: 0` degerini de 3600'e cevirirdi:
            # zaten olmus bir token bir saat boyunca onbellekte kalir ve her SAP
            # cagrisi 401 doner. Teshisi zor bir arizadir. Eksik/None ile 0
            # ayrilir.
            raw_expiry = payload.get("expires_in")
            if raw_expiry in (None, ""):
                lifetime = 3600.0
            else:
                try:
                    lifetime = float(raw_expiry)
                except (TypeError, ValueError):
                    lifetime = 3600.0
            self._expires_at = time.time() + max(0.0, lifetime)
            return self._token

    def expires_in(self) -> float:
        return max(0.0, self._expires_at - time.time())

    def close(self) -> None:
        self._client.close()


@dataclass
class _DestinationSnapshot:
    """Destination servisinden gelen tek cozumleme sonucu."""

    base_url: str
    token: str
    token_type: str
    proxy_type: str
    auth_type: str
    expires_at: float
    warnings: tuple[str, ...]


class DestinationResolver:
    """BTP Destination servisinden baglanti bilgisi cozer."""

    def __init__(
        self,
        *,
        service_url: str,
        token_provider: OAuth2TokenProvider,
        client: httpx.Client | None = None,
    ) -> None:
        self._service_url = service_url.rstrip("/")
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=30.0)

    def fetch(self, name: str) -> _DestinationSnapshot:
        """Destination'i cozer. Her cagrida taze token doner."""
        response = self._client.get(
            f"{self._service_url}/destination-configuration/v1/destinations/{name}",
            headers={"Authorization": f"Bearer {self._token_provider()}"},
        )
        if not response.is_success:
            raise SAPError(
                f"Destination '{name}' cozumlenemedi (HTTP {response.status_code}).",
                code="DESTINATION_RESOLVE_FAILED",
                detail=name,
            )
        payload = response.json()
        config = payload.get("destinationConfiguration", {}) or {}
        auth_tokens = payload.get("authTokens") or []

        base_url = str(config.get("URL", "")).rstrip("/")
        if not base_url:
            raise SAPError(
                f"Destination '{name}' URL icermiyor.", code="DESTINATION_NO_URL", detail=name
            )

        token = ""
        token_type = "Bearer"
        # `expiresIn` yoksa muhafazakar davran: 10 dakika sonra yeniden coz.
        # Uzun bir varsayilan, sure dolmus bir token'i saatlerce kullanmak demek.
        lifetime = 600.0
        if auth_tokens:
            entry = auth_tokens[0]
            if entry.get("error"):
                raise SAPError(
                    f"Destination '{name}' kimlik dogrulama hatasi dondurdu.",
                    code="DESTINATION_AUTH_FAILED",
                    detail=name,
                )
            token = str(entry.get("value", ""))
            token_type = str(entry.get("type") or "Bearer")
            raw_expiry = entry.get("expiresIn")
            if raw_expiry not in (None, ""):
                try:
                    lifetime = float(raw_expiry)
                except (TypeError, ValueError):
                    lifetime = 600.0

        auth_type = str(config.get("Authentication", ""))
        warnings: list[str] = []
        if auth_type == "BasicAuthentication":
            warnings.append(
                "Destination Basic Authentication kullaniyor. Principal propagation veya "
                "OAuth2 tercih edilmeli."
            )
        if auth_type in {"PrincipalPropagation", "SAMLAssertion", "OAuth2SAMLBearerAssertion"}:
            warnings.append(
                "Principal propagation aktif: SAP yetkileri son kullaniciya gore uygulanir. "
                "Agent teknik kullanici yetkisiyle genisletme yapamaz."
            )

        return _DestinationSnapshot(
            base_url=base_url,
            token=token,
            token_type=token_type,
            proxy_type=str(config.get("ProxyType", "Internet")),
            auth_type=auth_type,
            expires_at=time.time() + max(0.0, lifetime),
            warnings=tuple(warnings),
        )

    def resolve(self, name: str) -> ResolvedConnection:
        """Tek seferlik cozumleme (teshis ve geriye donuk uyumluluk).

        Uretim yolu `DestinationTokenProvider` uzerinden gider; bu metot
        token'i yenilemez, yalniz anlik durumu dondurur.
        """
        snapshot = self.fetch(name)
        headers: dict[str, str] = {}
        if snapshot.token:
            headers["Authorization"] = f"{snapshot.token_type} {snapshot.token}"
        return ResolvedConnection(
            base_url=snapshot.base_url,
            headers=headers,
            proxy_type=snapshot.proxy_type,
            auth_type=snapshot.auth_type,
            origin=f"destination:{name}",
            warnings=snapshot.warnings,
        )

    def close(self) -> None:
        self._client.close()


class DestinationTokenProvider:
    """Destination token'ini suresi dolmadan yeniden cozer.

    `OAuth2TokenProvider` ile ayni sozlesme (`() -> str`), ama token'i
    destination servisinden alir. Bu sinif olmadan destination modu, ilk
    cozumlemede alinan token'i omur boyu kullanirdi.
    """

    def __init__(
        self, resolver: DestinationResolver, name: str, *, skew_seconds: int = 60
    ) -> None:
        self._resolver = resolver
        self._name = name
        self._skew = max(10, skew_seconds)
        self._snapshot: _DestinationSnapshot | None = None
        self._lock = threading.Lock()

    def snapshot(self) -> _DestinationSnapshot:
        with self._lock:
            current = self._snapshot
            if current is None or time.time() >= current.expires_at - self._skew:
                current = self._resolver.fetch(self._name)
                self._snapshot = current
            return current

    def __call__(self) -> str:
        return self.snapshot().token

    @property
    def has_token(self) -> bool:
        return bool(self.snapshot().token)


def _connectivity_proxy(cfg: SAPSettings) -> tuple[str, Callable[[], str] | None]:
    """Cloud Connector trafiginin gectigi BTP connectivity proxy'si.

    Proxy kimlik dogrulamasi da sureli bir OAuth2 token'idir; sabit deger
    olarak gomulmez, saglayici uzerinden her istekte tazelenebilir hale gelir.
    """
    if not cfg.connectivity_proxy_url:
        return "", None
    provider: Callable[[], str] | None = None
    if cfg.connectivity_client_id and cfg.connectivity_token_url:
        provider = OAuth2TokenProvider(
            token_url=cfg.connectivity_token_url,
            client_id=cfg.connectivity_client_id,
            client_secret=cfg.connectivity_client_secret,
        )
    return cfg.connectivity_proxy_url.rstrip("/"), provider


def resolve_connection(cfg: SAPSettings) -> ResolvedConnection:
    """Ayarlara gore baglanti bilgisini cozer."""
    if cfg.auth_mode == "destination":
        oauth = OAuth2TokenProvider(
            token_url=cfg.oauth_token_url,
            client_id=cfg.oauth_client_id,
            client_secret=cfg.oauth_client_secret,
            scope=cfg.oauth_scope,
        )
        resolver = DestinationResolver(
            service_url=cfg.destination_service_url, token_provider=oauth
        )
        provider = DestinationTokenProvider(resolver, cfg.destination_name)
        snapshot = provider.snapshot()

        warnings = list(snapshot.warnings)
        proxy_url, proxy_auth = "", None
        if snapshot.proxy_type.lower() == "onpremise":
            proxy_url, proxy_auth = _connectivity_proxy(cfg)
            if not proxy_url:
                # Sessiz timeout yerine acik hata: on-premise sistem
                # connectivity proxy olmadan BTP'den erisilebilir DEGILDIR.
                raise SAPError(
                    f"Destination '{cfg.destination_name}' ProxyType=OnPremise bildiriyor "
                    "ancak SAP_CONNECTIVITY_PROXY_URL tanimli degil. Cloud Connector "
                    "arkasindaki sisteme dogrudan baglanilamaz.",
                    code="CONNECTIVITY_PROXY_MISSING",
                    detail=cfg.destination_name,
                )
            if proxy_auth is None:
                warnings.append(
                    "Connectivity proxy kimlik dogrulamasi yapilandirilmamis "
                    "(SAP_CONNECTIVITY_TOKEN_URL/CLIENT_ID). Proxy anonim erisime "
                    "acik degilse istekler 407 doner."
                )

        return ResolvedConnection(
            base_url=snapshot.base_url,
            # Token'i header'a gommuyoruz: her istekte saglayicidan taze okunur.
            token_provider=provider if snapshot.token else None,
            verify_ssl=cfg.verify_ssl,
            proxy_type=snapshot.proxy_type,
            auth_type=snapshot.auth_type,
            origin=f"destination:{cfg.destination_name}",
            warnings=tuple(warnings),
            proxy_url=proxy_url,
            proxy_auth_provider=proxy_auth,
            location_id=cfg.cloud_connector_location_id,
        )

    if cfg.auth_mode == "apikey":
        # SAP API Business Hub sandbox: gercek SAP tarafindan barindirilan,
        # gercek verili, SALT OKUNUR bir S/4HANA Cloud sistemi. Servis
        # yollarini, entity/alan adlarini ve hata bicimini dogrulamak icin
        # kullanilir; yazma denemesi sandbox tarafindan reddedilir.
        return ResolvedConnection(
            base_url=cfg.base_url,
            headers={cfg.api_key_header: cfg.api_key},
            verify_ssl=cfg.verify_ssl,
            origin="config:apikey(sandbox)",
            warnings=(
                "SAP_AUTH_MODE=apikey: API Business Hub sandbox'i SALT OKUNURDUR. "
                "Yazma cagrilari SAP tarafindan reddedilir; kontrat dogrulamasi "
                "icin kullanin.",
            ),
        )

    if cfg.auth_mode == "oauth2":
        provider = OAuth2TokenProvider(
            token_url=cfg.oauth_token_url,
            client_id=cfg.oauth_client_id,
            client_secret=cfg.oauth_client_secret,
            scope=cfg.oauth_scope,
        )
        return ResolvedConnection(
            base_url=cfg.base_url,
            token_provider=provider,
            verify_ssl=cfg.verify_ssl,
            origin="config:oauth2",
        )

    warnings = (
        "SAP_AUTH_MODE=basic: kullanici/parola uretim modeli olarak onerilmez. "
        "BTP Destination + OAuth2/principal propagation kullanin.",
    )
    return ResolvedConnection(
        base_url=cfg.base_url,
        auth=httpx.BasicAuth(cfg.username, cfg.password) if cfg.username else None,
        verify_ssl=cfg.verify_ssl,
        origin="config:basic",
        warnings=warnings,
    )


def build_http_client(connection: ResolvedConnection, cfg: SAPSettings) -> httpx.Client:
    """Cozulmus baglanti icin httpx istemcisi kurar.

    On-premise destination'da trafik connectivity proxy'ye yonlendirilir.
    `Proxy-Authorization` ve `SAP-Connectivity-SCC-Location-ID` proxy'nin
    kendisine aittir; hedef SAP sistemine gonderilmez.
    """
    headers = {"Accept": "application/json", "Accept-Language": cfg.description_language}
    headers.update(connection.headers)

    kwargs: dict[str, Any] = {
        "base_url": connection.base_url,
        "auth": connection.auth,
        "verify": connection.verify_ssl,
        "timeout": cfg.timeout,
        "headers": headers,
    }
    if connection.proxy_url:
        proxy_headers: dict[str, str] = {}
        if connection.proxy_auth_provider is not None:
            proxy_headers["Proxy-Authorization"] = (
                f"Bearer {connection.proxy_auth_provider()}"
            )
        if connection.location_id:
            proxy_headers["SAP-Connectivity-SCC-Location-ID"] = connection.location_id
        kwargs["proxy"] = httpx.Proxy(url=connection.proxy_url, headers=proxy_headers)
    return httpx.Client(**kwargs)
