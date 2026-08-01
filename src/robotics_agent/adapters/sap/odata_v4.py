"""OData V4 istemcisi.

Released OData V4 servisi tercih edilir; cunku
ETag/`If-Match`, server-driven pagination (`@odata.nextLink`), JSON `$batch` ve
yapilandirilmis hata modeli standarttir.

Desteklenen isler:
  read_entity      -> tek kayit + ETag
  read_collection  -> nextLink takibi ile sayfali okuma
  create           -> Prefer: return=representation ile olusturulan kaydi geri okur
  update           -> If-Match ile optimistic locking
  invoke_action    -> bound/unbound action cagrisi
  batch            -> bagimsiz okumalari tek round-trip'te toplar
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import SAPError
from .http import ODataHttpCore, split_link

log = logging.getLogger(__name__)


@dataclass
class Page:
    """Bir sayfalik okuma sonucu."""

    rows: list[dict[str, Any]]
    next_link: str = ""
    total_count: int | None = None

    @property
    def has_more(self) -> bool:
        return bool(self.next_link)


@dataclass
class BatchRequest:
    id: str
    method: str
    url: str
    headers: Mapping[str, str] | None = None
    body: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "method": self.method.upper(),
            "url": self.url,
        }
        if self.headers:
            payload["headers"] = dict(self.headers)
        if self.body is not None:
            payload["body"] = self.body
        return payload


@dataclass
class BatchResponse:
    id: str
    status: int
    body: Any
    headers: Mapping[str, str]

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300

    @property
    def rows(self) -> list[dict[str, Any]]:
        if isinstance(self.body, dict):
            value = self.body.get("value")
            if isinstance(value, list):
                return value
            return [self.body]
        return []


class ODataV4Client:
    """Tek bir S/4HANA sistemine karsi V4 istemcisi."""

    odata_version = "v4"

    def __init__(self, core: ODataHttpCore, *, page_size: int = 100, max_pages: int = 10) -> None:
        self.core = core
        self.core.odata_version = "v4"
        self.page_size = max(1, page_size)
        self.max_pages = max(1, max_pages)

    # --- Okuma --------------------------------------------------------------
    def read_entity(
        self,
        service: str,
        entity_path: str,
        *,
        params: Mapping[str, Any] | None = None,
        correlation_id: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """(kayit, etag). Kayit yoksa (None, "")."""
        try:
            response = self.core.request(
                "GET",
                f"{service.rstrip('/')}/{entity_path.lstrip('/')}",
                params=params,
                service_root=service,
                correlation_id=correlation_id,
            )
        except SAPError as exc:
            if exc.fault is not None and exc.fault.is_not_found:
                return None, ""
            raise
        data = response.data if isinstance(response.data, dict) else None
        return data, response.etag

    def read_collection(
        self,
        service: str,
        entity_set: str,
        *,
        filter_expr: str = "",
        select: Sequence[str] | None = None,
        expand: Sequence[str] | None = None,
        order_by: str = "",
        top: int | None = None,
        count: bool = False,
        max_pages: int | None = None,
        correlation_id: str = "",
    ) -> Page:
        """Server-driven pagination ile koleksiyon okur.

        `@odata.nextLink` takip edilir; `max_pages` ile sinirlandirilir ki bir
        tool tek cagrida sinirsiz veri cekmesin.
        """
        params: dict[str, Any] = {"$top": top or self.page_size}
        if filter_expr:
            params["$filter"] = filter_expr
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)
        if order_by:
            params["$orderby"] = order_by
        if count:
            params["$count"] = "true"

        rows: list[dict[str, Any]] = []
        total: int | None = None
        page_limit = max_pages or self.max_pages
        path = f"{service.rstrip('/')}/{entity_set.lstrip('/')}"
        current_params: Mapping[str, Any] = params

        for page_index in range(page_limit):
            response = self.core.request(
                "GET",
                path,
                params=current_params,
                service_root=service,
                correlation_id=correlation_id,
            )
            body = response.data if isinstance(response.data, dict) else {}
            batch_rows = body.get("value")
            if isinstance(batch_rows, list):
                rows.extend(r for r in batch_rows if isinstance(r, dict))
            if total is None and "@odata.count" in body:
                try:
                    total = int(body["@odata.count"])
                except (TypeError, ValueError):
                    total = None
            next_link = response.next_link
            if not next_link:
                return Page(rows=rows, total_count=total)
            if page_index == page_limit - 1:
                # Butce doldu: kalan sayfayi cursor olarak geri veriyoruz ki
                # tool sinirsiz veri cekmek yerine imleci modele gosterebilsin.
                return Page(rows=rows, next_link=next_link, total_count=total)
            # nextLink mutlak veya goreli olabilir; skiptoken query'de tasinir.
            path, current_params = split_link(next_link)

        return Page(rows=rows, total_count=total)

    # --- Yazma --------------------------------------------------------------
    def create(
        self,
        service: str,
        entity_set: str,
        body: Mapping[str, Any],
        *,
        correlation_id: str = "",
        return_representation: bool = True,
    ) -> tuple[dict[str, Any], str]:
        """Kayit olusturur. Donen kayit ve ETag verilir."""
        headers = {"Prefer": "return=representation"} if return_representation else {}
        response = self.core.request(
            "POST",
            f"{service.rstrip('/')}/{entity_set.lstrip('/')}",
            json_body=dict(body),
            headers=headers,
            service_root=service,
            correlation_id=correlation_id,
        )
        created = response.data if isinstance(response.data, dict) else {}
        return created, response.etag

    def update(
        self,
        service: str,
        entity_path: str,
        body: Mapping[str, Any],
        *,
        etag: str,
        correlation_id: str = "",
    ) -> tuple[dict[str, Any], str]:
        """PATCH + If-Match. ETag olmadan guncelleme yapilmaz."""
        if not etag:
            raise SAPError(
                "Guncelleme icin ETag zorunlu. Kaydi once okuyup ETag alin "
                "(stale update korumasi).",
                code="ETAG_REQUIRED",
                detail=entity_path,
            )
        response = self.core.request(
            "PATCH",
            f"{service.rstrip('/')}/{entity_path.lstrip('/')}",
            json_body=dict(body),
            headers={"Prefer": "return=representation"},
            service_root=service,
            correlation_id=correlation_id,
            etag=etag,
        )
        updated = response.data if isinstance(response.data, dict) else {}
        return updated, response.etag

    def invoke_action(
        self,
        service: str,
        action_path: str,
        body: Mapping[str, Any] | None = None,
        *,
        etag: str = "",
        correlation_id: str = "",
    ) -> dict[str, Any]:
        response = self.core.request(
            "POST",
            f"{service.rstrip('/')}/{action_path.lstrip('/')}",
            json_body=dict(body or {}),
            service_root=service,
            correlation_id=correlation_id,
            etag=etag,
        )
        return response.data if isinstance(response.data, dict) else {}

    # --- Batch --------------------------------------------------------------
    def batch(
        self,
        service: str,
        requests: Iterable[BatchRequest],
        *,
        correlation_id: str = "",
    ) -> list[BatchResponse]:
        """JSON `$batch`: bagimsiz okumalari tek round-trip'te calistirir.

        Latency'yi dusurur. Token tasarrufu saglamaz; sonuc projeksiyonu yine
        tool tarafinda yapilmalidir.
        """
        payload = {"requests": [r.to_dict() for r in requests]}
        response = self.core.request(
            "POST",
            f"{service.rstrip('/')}/$batch",
            json_body=payload,
            service_root=service,
            correlation_id=correlation_id,
        )
        body = response.data if isinstance(response.data, dict) else {}
        out: list[BatchResponse] = []
        for item in body.get("responses") or []:
            if not isinstance(item, dict):
                continue
            out.append(
                BatchResponse(
                    id=str(item.get("id", "")),
                    status=int(item.get("status", 0) or 0),
                    body=item.get("body"),
                    headers=item.get("headers") or {},
                )
            )
        return out

    def metadata(self, service: str, *, correlation_id: str = "") -> str:
        return self.core.metadata(service, correlation_id=correlation_id)

    def close(self) -> None:
        self.core.close()


def quote(value: str) -> str:
    """V4 string literalinde tek tirnak kacisi."""
    return str(value).replace("'", "''")


def escape_key(value: str) -> str:
    """Anahtar segmentini guvenli sekilde sarar: Entity('KEY')."""
    return f"'{quote(value)}'"
