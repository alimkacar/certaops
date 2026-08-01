"""Tenant ve yetki kapsamina duyarlı guvenli SAP okuma cache'i.

Cache burada bir performans detayi degil, bir **yetki yuzeyi**dir: yanlis
anahtarlanmis bir cache, alan bazli yetkiyi ve tenant izolasyonunu tek
hamlede etkisiz kilar. Bu yuzden anahtar tenant, kapsam hash'i, organizasyon
ve tool surumunu icerir; D3 veri hicbir kosulda yazilmaz.
"""

import threading

from ..privacy.classification import DataClass
from .base import (
    NO_CACHE,
    CacheBackend,
    CacheEntry,
    CacheKey,
    CachePolicy,
    CacheStats,
    build_cache_key,
    entry_for,
    null_cache,
)
from .secure_cache import SecureCache

__all__ = [
    "NO_CACHE",
    "CacheBackend",
    "CacheEntry",
    "CacheKey",
    "CachePolicy",
    "CacheStats",
    "DataClass",
    "SecureCache",
    "build_cache_key",
    "entry_for",
    "get_tool_cache",
    "null_cache",
    "reset_tool_cache",
]

_lock = threading.Lock()
_cache: SecureCache | None = None


def get_tool_cache(settings: object | None = None) -> SecureCache:
    """Surec genelinde paylasilan tool cache'i.

    Tek ornek olmasi bilinclidir: invalidation yalnizca tum okuyucularin ayni
    depoyu gormesi durumunda ise yarar.
    """
    global _cache
    with _lock:
        if _cache is None:
            cfg = getattr(settings, "cache", None) if settings is not None else None
            _cache = SecureCache(
                max_entries_per_tenant=int(getattr(cfg, "max_entries_per_tenant", 500)),
                enabled=bool(getattr(cfg, "enabled", True)),
            )
        return _cache


def reset_tool_cache() -> None:
    """Testler arasi temiz baslangic."""
    global _cache
    with _lock:
        _cache = None
