"""OData V2 istemcisi.

Released V4 servisi olmayan alanlar icin gereklidir (ornegin malzeme stogu,
inspection lot, production order). Yeni gelistirmede tercih sirasi V4'tur;
bu istemci "geriye uyum + bosluk doldurma" gorevindedir.

V2'nin V4'ten ayrildigi noktalar burada kapsullenir: `d`/`results` sarmalayicisi,
`/Date(...)/ ` literali ve `__next` sayfalamasi.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .errors import SAPError
from .http import ODataHttpCore, split_link

log = logging.getLogger(__name__)


def quote(value: str) -> str:
    return str(value).replace("'", "''")


def to_odata_datetime(value: date) -> str:
    """OData V2 Edm.DateTime literali (UTC gun basi)."""
    stamp = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return f"/Date({int(stamp.timestamp() * 1000)})/"


def parse_odata_datetime(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, str) and value.startswith("/Date("):
        digits = value[6:-2].split("+")[0].split("-")[0]
        try:
            return datetime.fromtimestamp(int(digits) / 1000, tz=timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


@dataclass
class V2Page:
    rows: list[dict[str, Any]]
    next_link: str = ""

    @property
    def has_more(self) -> bool:
        return bool(self.next_link)


class ODataV2Client:
    odata_version = "v2"

    def __init__(self, core: ODataHttpCore, *, page_size: int = 100, max_pages: int = 10) -> None:
        self.core = core
        self.core.odata_version = "v2"
        self.page_size = max(1, page_size)
        self.max_pages = max(1, max_pages)

    # --- Okuma --------------------------------------------------------------
    def read(
        self,
        service: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        correlation_id: str = "",
    ) -> list[dict[str, Any]]:
        """`d`/`results` sarmalayicisini acar; tek kayitta da liste doner."""
        try:
            response = self.core.request(
                "GET",
                f"{service.rstrip('/')}/{path.lstrip('/')}",
                params=params,
                service_root=service,
                correlation_id=correlation_id,
            )
        except SAPError as exc:
            if exc.fault is not None and exc.fault.is_not_found:
                return []
            raise
        body = response.data if isinstance(response.data, dict) else {}
        payload = body.get("d", body)
        if isinstance(payload, dict) and "results" in payload:
            results = payload["results"]
            return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []
        return [payload] if isinstance(payload, dict) and payload else []

    def read_paged(
        self,
        service: str,
        entity_set: str,
        *,
        filter_expr: str = "",
        select: Sequence[str] | None = None,
        expand: Sequence[str] | None = None,
        top: int | None = None,
        max_pages: int | None = None,
        correlation_id: str = "",
    ) -> V2Page:
        params: dict[str, Any] = {"$top": top or self.page_size}
        if filter_expr:
            params["$filter"] = filter_expr
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)

        rows: list[dict[str, Any]] = []
        path = f"{service.rstrip('/')}/{entity_set.lstrip('/')}"
        current_params: Mapping[str, Any] = params
        limit = max_pages or self.max_pages

        for page_index in range(limit):
            response = self.core.request(
                "GET", path, params=current_params, service_root=service,
                correlation_id=correlation_id,
            )
            body = response.data if isinstance(response.data, dict) else {}
            payload = body.get("d", {})
            batch = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(batch, list):
                rows.extend(r for r in batch if isinstance(r, dict))
            elif isinstance(payload, dict) and payload:
                rows.append(payload)
            next_link = str((payload or {}).get("__next", "")) if isinstance(payload, dict) else ""
            if not next_link:
                return V2Page(rows=rows)
            if page_index == limit - 1:
                return V2Page(rows=rows, next_link=next_link)
            path, current_params = split_link(next_link)
        return V2Page(rows=rows)

    # --- Yazma --------------------------------------------------------------
    def create(
        self,
        service: str,
        entity_set: str,
        body: Mapping[str, Any],
        *,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        response = self.core.request(
            "POST",
            f"{service.rstrip('/')}/{entity_set.lstrip('/')}",
            json_body=dict(body),
            service_root=service,
            correlation_id=correlation_id,
        )
        body_out = response.data if isinstance(response.data, dict) else {}
        created = body_out.get("d", body_out)
        return created if isinstance(created, dict) else {}

    def metadata(self, service: str, *, correlation_id: str = "") -> str:
        return self.core.metadata(service, correlation_id=correlation_id)

    def close(self) -> None:
        self.core.close()


def expanded_rows(row: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """V2 `$expand` sonucundan alt kayitlari cikarir."""
    nested = row.get(key) or {}
    if isinstance(nested, dict):
        results = nested.get("results")
        if isinstance(results, list):
            return [r for r in results if isinstance(r, dict)]
        return [nested] if nested else []
    if isinstance(nested, list):
        return [r for r in nested if isinstance(r, dict)]
    return []
