"""SAP hatalarinin yapilandirilmis modeli.

Token tasarrufu icin SAP hata detayini silmek yasaktir. Hata kodu, hedef API ve
correlation ID her zaman korunur. Buna karsilik hata govdesi
ham HTML/stack olarak modele verilmez; sinifllandirilmis bir nesneye cevrilir.

OData V2 ve V4 hata govdeleri farklidir; ikisi de burada normalize edilir.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Yeniden denenebilir HTTP durumlari (yazma islemlerinde yalniz idempotent
# baglamda kullanilir; bkz. core.execution).
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_HTML_TAG = re.compile(r"<[^>]+>")


class SAPError(RuntimeError):
    """SAP tarafindan donen is/teknik hatasi.

    Geriye donuk uyumluluk icin `code`/`detail` alanlari korunur; yeni kod
    `fault` uzerinden yapilandirilmis bilgiye erisir.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        detail: str = "",
        fault: SAPFault | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or (fault.code if fault else "")
        self.detail = detail or (fault.target_api if fault else "")
        self.fault = fault

    @property
    def is_authorization(self) -> bool:
        return bool(self.fault and self.fault.is_authorization)

    @property
    def is_concurrency(self) -> bool:
        return bool(self.fault and self.fault.is_concurrency)

    @property
    def is_retryable(self) -> bool:
        return bool(self.fault and self.fault.is_retryable)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": str(self),
            "sap_code": self.code,
            "detail": self.detail,
        }
        if self.fault is not None:
            payload.update(self.fault.to_dict())
        return payload


class SAPNotSupported(SAPError):
    """Bu backend/sistem bu yetenegi desteklemiyor.

    Sessizce bos veri dondurmek yerine acik hata verilir; kismi destek tam
    destekmis gibi sunulmaz.
    """

    def __init__(self, capability: str, *, backend: str = "", hint: str = "") -> None:
        message = f"'{capability}' bu backend'de ({backend or 'bilinmiyor'}) desteklenmiyor."
        if hint:
            message += f" {hint}"
        super().__init__(message, code="CAPABILITY_NOT_SUPPORTED", detail=capability)
        self.capability = capability
        self.hint = hint


@dataclass(frozen=True)
class SAPFault:
    """Normalize edilmis SAP hatasi."""

    http_status: int
    code: str
    message: str
    target_api: str
    correlation_id: str = ""
    severity: str = "error"
    details: tuple[dict[str, Any], ...] = ()
    retry_after_s: float | None = None
    request_path: str = ""
    odata_version: str = ""

    @property
    def is_retryable(self) -> bool:
        return self.http_status in RETRYABLE_STATUS

    @property
    def is_authorization(self) -> bool:
        return self.http_status in {401, 403}

    @property
    def is_concurrency(self) -> bool:
        # 412 Precondition Failed -> ETag uyusmazligi (stale update)
        return self.http_status in {409, 412}

    @property
    def is_not_found(self) -> bool:
        return self.http_status == 404

    @property
    def is_csrf(self) -> bool:
        return self.http_status == 403 and "csrf" in self.message.lower()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "http_status": self.http_status,
            "sap_code": self.code,
            "message": self.message,
            "target_api": self.target_api,
            "severity": self.severity,
        }
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        if self.request_path:
            payload["request_path"] = self.request_path
        if self.details:
            payload["details"] = list(self.details)[:10]
        if self.retry_after_s is not None:
            payload["retry_after_s"] = self.retry_after_s
        if self.is_concurrency:
            payload["conflict"] = "ETag uyusmazligi; kaydi yeniden okuyup diff'i tekrar onaylatin."
        if self.is_authorization:
            payload["authorization"] = "Eksik SAP yetkisi. Yetki nesnesi/rol kontrolu gerekiyor."
        return payload

    def to_error(self) -> SAPError:
        return SAPError(
            f"SAP {self.http_status}: {self.message}",
            code=self.code or str(self.http_status),
            detail=self.target_api,
            fault=self,
        )


def _strip_html(text: str) -> str:
    cleaned = _HTML_TAG.sub(" ", text)
    return " ".join(cleaned.split())


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_sap_error(
    *,
    status_code: int,
    body: str,
    headers: Any,
    target_api: str,
    request_path: str = "",
    correlation_id: str = "",
    odata_version: str = "",
) -> SAPFault:
    """SAP hata govdesini (V2/V4/duz metin) tek modele cevirir."""
    code = ""
    message = ""
    severity = "error"
    details: list[dict[str, Any]] = []

    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {}

    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code", "") or "")
        raw_message = error.get("message")
        if isinstance(raw_message, dict):
            # OData V2: {"message": {"lang": "en", "value": "..."}}
            message = str(raw_message.get("value", "") or "")
        elif isinstance(raw_message, str):
            # OData V4: {"message": "..."}
            message = raw_message

        # V4 detay listesi
        for item in error.get("details") or []:
            if isinstance(item, dict):
                details.append(
                    {
                        "code": item.get("code", ""),
                        "message": item.get("message", ""),
                        "target": item.get("target", ""),
                        "severity": item.get("@SAP__common.Severity")
                        or item.get("severity", "error"),
                    }
                )
        # V2 inner error detaylari
        inner = error.get("innererror") or {}
        if isinstance(inner, dict):
            for item in (inner.get("errordetails") or []):
                if isinstance(item, dict):
                    details.append(
                        {
                            "code": item.get("code", ""),
                            "message": item.get("message", ""),
                            "target": item.get("propertyref", ""),
                            "severity": item.get("severity", "error"),
                        }
                    )
        numeric_severity = error.get("@SAP__common.numericSeverity")
        if numeric_severity is not None:
            severity = {1: "success", 2: "info", 3: "warning", 4: "error"}.get(
                int(numeric_severity), "error"
            )

    if not message:
        message = _strip_html(body)[:500] or f"HTTP {status_code}"

    if not correlation_id and hasattr(headers, "get"):
        correlation_id = (
            headers.get("sap-correlationid")
            or headers.get("x-correlationid")
            or headers.get("x-request-id")
            or ""
        )

    return SAPFault(
        http_status=status_code,
        code=code or str(status_code),
        message=message,
        target_api=target_api,
        correlation_id=correlation_id,
        severity=severity,
        details=tuple(details),
        retry_after_s=_retry_after(headers),
        request_path=request_path,
        odata_version=odata_version,
    )


@dataclass
class AuthorizationExplanation:
    """`sap_explain_authorization_failure` icin kural katalogu sonucu."""

    diagnosis: str
    likely_missing: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    grants_nothing: bool = field(default=True, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnosis": self.diagnosis,
            "likely_missing_authorizations": list(self.likely_missing),
            "next_steps": list(self.next_steps),
            "note": "Bu tool yetki vermez; yalnizca eksik yetkiyi tarif eder.",
        }


# Hangi API hangi SAP yetki nesnesine denk gelir (kisitli, belgelenmis katalog).
_AUTH_CATALOGUE: dict[str, tuple[str, ...]] = {
    "product": ("M_MATE_MAT (malzeme ana verisi goruntuleme)", "M_MATE_WRK (tesis yetkisi)"),
    "stock": ("M_MSEG_WMB (malzeme belgesi)", "M_MATE_WRK"),
    "availability": ("M_MATE_WRK", "C_APO_ATP / ATP okuma yetkisi"),
    "mrp": ("M_MTDI_ORG (MRP alani)", "M_MATE_WRK"),
    "inforecord": ("M_EINF_EKO (bilgi kaydi / satinalma org)",),
    "supplier": ("F_LFA1_APP (tedarikci)", "M_LFM1_EKO (satinalma org)"),
    "purchaserequisition": ("M_BANF_EKG (satinalma grubu)", "M_BANF_WRK (tesis)", "M_BANF_BSA"),
    "purchaseorder": ("M_BEST_EKG", "M_BEST_WRK", "M_BEST_BSA"),
    "project": ("C_PRPS_KOK (WBS)", "K_CCA (masraf yeri/kontrolling alani)"),
}


def explain_authorization_failure(fault: SAPFault) -> AuthorizationExplanation:
    """401/403 hatasini rol/yetki diline cevirir. Yetki vermez, tarif eder."""
    if fault.http_status == 401:
        return AuthorizationExplanation(
            diagnosis=(
                "Kimlik dogrulama basarisiz (401). Teknik kullanici/token gecersiz veya suresi "
                "gecmis olabilir. Bu bir is yetkisi sorunu degil, baglanti kimligi sorunudur."
            ),
            next_steps=(
                "Destination/OAuth token gecerliligini kontrol edin.",
                "sap_connection_health tool'unu calistirin.",
            ),
        )

    target = fault.target_api.lower()
    candidates: list[str] = []
    for key, objects in _AUTH_CATALOGUE.items():
        if key in target:
            candidates.extend(objects)

    return AuthorizationExplanation(
        diagnosis=(
            f"SAP yetkilendirme reddi (403) - hedef API: {fault.target_api}. "
            f"SAP mesaji: {fault.message[:200]}"
        ),
        likely_missing=tuple(candidates) or ("Hedef API icin ilgili is yetkisi nesnesi",),
        next_steps=(
            "Teknik kullanicinin rolunu SU53/STAUTHTRACE ciktisiyla dogrulayin.",
            "Yalnizca ihtiyac duyulan tesis/satinalma organizasyonu icin yetki verin.",
            "Yetki degisikligi talebini SAP yoneticisine acin; agent kendi yetkisini genisletemez.",
        ),
    )
