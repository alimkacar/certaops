"""SAP BTP Destination cozumleme ve OAuth2 token yonetimi.

Uretimde SAP kimligi `.env` icindeki Basic kullanici parolasi olmamalidir.
Desteklenen modlar:

  basic       -> kullanici/parola (yalniz yerel gelistirme; uyari uretir)
  oauth2      -> client credentials; token onbelleklenir ve suresi dolmadan yenilenir
  destination -> BTP Destination servisinden URL + kimlik bilgisi cozumlenir
                 (Cloud Connector arkasindaki on-premise sistem dahil)

Secret'lar yalnizca bu modulde tutulur; modele ve loglara asla verilmez.
"""

from __future__ import annotations

import logging
import threading
import time
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

    def describe(self) -> dict[str, Any]:
        """Secret icermeyen ozet (health/capability tool'lari icin)."""
        return {
            "base_url": self.base_url,
            "auth": "oauth2" if self.token_provider else ("basic" if self.auth else "none"),
            "proxy_type": self.proxy_type,
            "origin": self.origin,
            "verify_ssl": self.verify_ssl,
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
            self._expires_at = time.time() + float(payload.get("expires_in", 3600) or 3600)
            return self._token

    def expires_in(self) -> float:
        return max(0.0, self._expires_at - time.time())

    def close(self) -> None:
        self._client.close()


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

    def resolve(self, name: str) -> ResolvedConnection:
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

        headers: dict[str, str] = {}
        token_provider = None
        if auth_tokens:
            token = auth_tokens[0]
            value = str(token.get("value", ""))
            token_type = str(token.get("type", "Bearer"))
            if value:
                headers["Authorization"] = f"{token_type} {value}"

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

        return ResolvedConnection(
            base_url=base_url,
            token_provider=token_provider,
            headers=headers,
            proxy_type=str(config.get("ProxyType", "Internet")),
            origin=f"destination:{name}",
            warnings=tuple(warnings),
        )

    def close(self) -> None:
        self._client.close()


def resolve_connection(cfg: SAPSettings) -> ResolvedConnection:
    """Ayarlara gore baglanti bilgisini cozer."""
    if cfg.auth_mode == "destination":
        token_provider = OAuth2TokenProvider(
            token_url=cfg.oauth_token_url,
            client_id=cfg.oauth_client_id,
            client_secret=cfg.oauth_client_secret,
            scope=cfg.oauth_scope,
        )
        resolver = DestinationResolver(
            service_url=cfg.destination_service_url, token_provider=token_provider
        )
        resolved = resolver.resolve(cfg.destination_name)
        return ResolvedConnection(
            base_url=resolved.base_url,
            token_provider=resolved.token_provider,
            headers=resolved.headers,
            verify_ssl=cfg.verify_ssl,
            proxy_type=resolved.proxy_type,
            origin=resolved.origin,
            warnings=resolved.warnings,
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
    headers = {"Accept": "application/json", "Accept-Language": "TR"}
    headers.update(connection.headers)
    return httpx.Client(
        base_url=connection.base_url,
        auth=connection.auth,
        verify=connection.verify_ssl,
        timeout=cfg.timeout,
        headers=headers,
    )
