"""OData istemcileri icin ortak HTTP cekirdegi.

Sagladigi adapter garantileri:
  - Host allowlist: SSRF ve yanlis sisteme yazma riski kesilir.
  - CSRF token akisi: fetch + 403'te tek kez yenileme.
  - Retry: yalniz guvenli durumlarda; `Retry-After` basligina uyar.
  - Yapilandirilmis hata: her hatali yanit `SAPFault`a cevrilir.
  - Correlation ID: her istege eklenir, hataya ve audit'e tasinir.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from .errors import RETRYABLE_STATUS, SAPError, SAPFault, parse_sap_error

log = logging.getLogger(__name__)

MODIFYING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE", "MERGE"})


def split_link(link: str) -> tuple[str, dict[str, str]]:
    """Bir OData nextLink'i (yol, query parametreleri) olarak ayirir.

    Gerekli cunku httpx'te `params` verildiginde URL'deki query string tamamen
    degistirilir. nextLink'in tasidigi skiptoken/sayfa parametrelerini kaybetmemek
    icin once ayristirip sonra sap-client gibi sabit parametrelerle birlestiririz.
    """
    parts = urlsplit(link)
    path = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return path, dict(parse_qsl(parts.query, keep_blank_values=True))


class HostNotAllowed(SAPError):
    """Istek allowlist'te olmayan bir hosta gidiyor."""

    def __init__(self, host: str, allowed: tuple[str, ...]) -> None:
        super().__init__(
            f"Host '{host}' izinli listede degil (izinli: {', '.join(allowed) or 'yok'}). "
            "SAP_ALLOWED_HOSTS ayarini kontrol edin.",
            code="EGRESS_BLOCKED",
            detail=host,
        )
        self.host = host


@dataclass
class ODataResponse:
    status_code: int
    data: Any
    etag: str = ""
    next_link: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.data in (None, {}, [])


@dataclass
class ODataHttpCore:
    """Tek SAP sistemine karsi HTTP islerini yapan cekirdek.

    `client` disaridan verilir; testler `httpx.MockTransport` ile gercek ag
    olmadan tam davranisi dogrulayabilir.
    """

    client: httpx.Client
    odata_version: str = "v4"
    sap_client: str = ""
    allowed_hosts: tuple[str, ...] = ()
    max_retries: int = 3
    csrf_enabled: bool = True
    token_provider: Callable[[], str] | None = None
    sleep: Callable[[float], None] = time.sleep

    _csrf_token: str = field(default="", init=False, repr=False)

    # --- Guvenlik -----------------------------------------------------------
    def _assert_host_allowed(self, url: str) -> None:
        if not self.allowed_hosts:
            return  # allowlist tanimlanmamis: base_url disi cagri zaten yapilmaz
        host = urlsplit(str(url)).hostname or ""
        for allowed in self.allowed_hosts:
            candidate = allowed.strip().lower()
            if not candidate:
                continue
            if host == candidate or (candidate.startswith("*.") and host.endswith(candidate[1:])):
                return
        raise HostNotAllowed(host, self.allowed_hosts)

    # --- Istek --------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        service_root: str = "",
        correlation_id: str = "",
        etag: str = "",
        expect_json: bool = True,
    ) -> ODataResponse:
        method = method.upper()
        merged_headers: dict[str, str] = {
            "Accept": "application/json",
            "Accept-Language": "TR",
        }
        if correlation_id:
            merged_headers["sap-correlationid"] = correlation_id
            merged_headers["X-Correlation-ID"] = correlation_id
        if self.token_provider is not None:
            merged_headers["Authorization"] = f"Bearer {self.token_provider()}"
        if method in MODIFYING_METHODS:
            merged_headers["Content-Type"] = "application/json"
            if etag:
                # Optimistic locking: eski veri uzerine yazmayi engeller.
                merged_headers["If-Match"] = etag
        if headers:
            merged_headers.update(headers)

        query = self._params(params)
        attempt = 0
        last_fault: SAPFault | None = None

        while attempt < max(1, self.max_retries):
            attempt += 1
            if method in MODIFYING_METHODS and self.csrf_enabled:
                merged_headers["x-csrf-token"] = self._ensure_csrf(service_root or path)

            url = self.client.base_url.join(path) if self.client.base_url else httpx.URL(path)
            self._assert_host_allowed(str(url))

            try:
                response = self.client.request(
                    method, path, params=query, json=json_body, headers=merged_headers
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                # Yazma cagrilarinda sonuc bilinmiyor: cekirdek burada retry yapmaz,
                # karar core.execution'daki idempotency/mutabakat katmanina aittir.
                if method in MODIFYING_METHODS:
                    raise
                if attempt >= self.max_retries:
                    raise
                self.sleep(min(8.0, 2.0**attempt))
                continue

            if response.is_success:
                return self._to_response(response, expect_json=expect_json)

            fault = parse_sap_error(
                status_code=response.status_code,
                body=response.text,
                headers=response.headers,
                target_api=service_root or path,
                request_path=str(response.request.url.path),
                correlation_id=correlation_id,
                odata_version=self.odata_version,
            )
            last_fault = fault

            # CSRF token suresi dolmus: bir kez yenile ve tekrar dene.
            if fault.is_csrf and attempt < self.max_retries:
                self._csrf_token = ""
                continue

            if fault.http_status in RETRYABLE_STATUS and attempt < self.max_retries:
                if method in MODIFYING_METHODS:
                    # Idempotent olmayan cagriyi kor bir sekilde tekrarlamayiz.
                    break
                delay = fault.retry_after_s if fault.retry_after_s is not None else min(8.0, 2.0**attempt)
                log.warning(
                    "SAP %s yanitladi, %.1f s sonra tekrar denenecek (%s)",
                    fault.http_status,
                    delay,
                    fault.target_api,
                )
                self.sleep(delay)
                continue

            raise fault.to_error()

        if last_fault is not None:
            raise last_fault.to_error()
        raise SAPError("SAP cagrisi tamamlanamadi.", code="UNKNOWN")

    # --- Yardimcilar --------------------------------------------------------
    def _params(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if self.sap_client:
            params["sap-client"] = self.sap_client
        if self.odata_version == "v2":
            params["$format"] = "json"
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _ensure_csrf(self, service_root: str) -> str:
        if self._csrf_token:
            return self._csrf_token
        root = service_root.rstrip("/")
        headers = {"x-csrf-token": "Fetch", "Accept": "application/xml"}
        if self.token_provider is not None:
            headers["Authorization"] = f"Bearer {self.token_provider()}"
        response = self.client.get(
            f"{root}/$metadata",
            params={"sap-client": self.sap_client} if self.sap_client else None,
            headers=headers,
        )
        token = response.headers.get("x-csrf-token", "")
        if not token:
            raise SAPError(
                "CSRF token alinamadi. Kullanicinin OData yetkisi ve oturum cerezleri "
                "kontrol edilmeli.",
                code="CSRF_FETCH_FAILED",
                detail=root,
            )
        self._csrf_token = token
        return token

    def _to_response(self, response: httpx.Response, *, expect_json: bool) -> ODataResponse:
        etag = response.headers.get("etag", "")
        if not expect_json:
            return ODataResponse(
                status_code=response.status_code,
                data=response.text,
                etag=etag,
                headers=dict(response.headers),
            )
        if not response.content:
            return ODataResponse(
                status_code=response.status_code, data=None, etag=etag, headers=dict(response.headers)
            )
        try:
            body = response.json()
        except ValueError:
            return ODataResponse(
                status_code=response.status_code,
                data=response.text,
                etag=etag,
                headers=dict(response.headers),
            )
        if isinstance(body, dict) and not etag:
            etag = str(body.get("@odata.etag", "") or "")
        next_link = ""
        if isinstance(body, dict):
            next_link = str(body.get("@odata.nextLink", "") or body.get("__next", "") or "")
        return ODataResponse(
            status_code=response.status_code,
            data=body,
            etag=etag,
            next_link=next_link,
            headers=dict(response.headers),
        )

    def metadata(self, service_root: str, *, correlation_id: str = "") -> str:
        """Servis $metadata belgesini (EDMX XML) dondurur."""
        response = self.request(
            "GET",
            f"{service_root.rstrip('/')}/$metadata",
            headers={"Accept": "application/xml"},
            service_root=service_root,
            correlation_id=correlation_id,
            expect_json=False,
        )
        return str(response.data or "")

    def close(self) -> None:
        self.client.close()
