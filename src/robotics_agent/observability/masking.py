"""PII ve ticari veri maskeleme.

Maskeleme modeli cagirmadan **once** ve istemciye yanit donmeden once yapilir.
"Model sonra gormezden gelsin" bir guvenlik kontrolu degildir.

Bu modul dar kapsamlidir ve bilerek boyle: e-posta, telefon, IBAN, kart numarasi
ve secret benzeri anahtarlar. Is verisi (fiyat, malzeme numarasi) maskelenmez;
maskelenirse karar verilemez hale gelir.
"""

from __future__ import annotations

import re
from typing import Any

_SECRET_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cookie",
    "private_key",
    "client_secret",
)

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2})[A-Z0-9]{10,30}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s()-]{8,}\d)(?!\d)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

MASK = "***"


def mask_text(value: str) -> str:
    """Metindeki kimlik/odeme/secret desenlerini maskeler."""
    if not value:
        return value
    masked = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", value)
    masked = _EMAIL.sub(lambda m: f"{m.group(1)}{MASK}{m.group(2)}", masked)
    masked = _IBAN.sub(lambda m: f"{m.group(1)}{MASK}", masked)
    masked = _CARD.sub(MASK, masked)
    masked = _PHONE.sub(MASK, masked)
    return masked


def mask_payload(value: Any, *, _depth: int = 0) -> Any:
    """Sozluk/liste yapisini gezerek maskeler; secret anahtarlari tumden gizler."""
    if _depth > 10:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS):
                out[key] = MASK
            else:
                out[key] = mask_payload(item, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [mask_payload(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return mask_text(value)
    return value


def truncate_preview(text: str, *, limit: int = 600) -> str:
    """API yanitindaki tool preview'i icin guvenli kirpma."""
    masked = mask_text(text)
    if len(masked) <= limit:
        return masked
    return masked[:limit] + f"... [{len(masked) - limit} karakter kirpildi]"
