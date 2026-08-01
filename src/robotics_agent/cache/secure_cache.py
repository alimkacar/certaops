"""Tenant-izole, veri sinifina duyarlı in-process cache.

Guvenlik ozellikleri:

  1. **Tenant bolmesi.** Girdiler tenant basina ayri sozlukte tutulur.
     Bir tenant'in anahtar uzayi digerine hic dokunmaz; capraz tenant okuma
     bir hata degil, yapisal olarak imkansizdir.
  2. **Sinif kapisi.** D3 veri hicbir kosulda yazilmaz; D2 yalnizca tool'un
     `cache_policy` tavani izin veriyorsa.
  3. **Etiketle gecersiz kilma.** Yazma isleminden sonra ilgili is nesnesi
     etiketleri (`po:4500018821`) temizlenir.
  4. **Sinirli boyut.** Tenant basina girdi siniri; asildiginda en eski
     girdiler dusurulur. Cache bellek sizintisina donusmez.

Bu uygulama tek surec icindir. Coklu worker/pod kurulumunda ayni sozlesmeyi
karsilayan Redis backend'i yazilir (`CacheBackend` protokolu); cagiran taraf
degismez.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import datetime, timezone

from ..privacy.classification import DataClass
from .base import CacheEntry, CacheKey, CacheStats

log = logging.getLogger(__name__)

__all__ = ["SecureCache"]


class SecureCache:
    """Tenant bolmeli, TTL'li ve sinif duyarli cache."""

    def __init__(self, *, max_entries_per_tenant: int = 500, enabled: bool = True) -> None:
        self._buckets: dict[str, OrderedDict[str, CacheEntry]] = {}
        self._max = max(16, max_entries_per_tenant)
        self._enabled = enabled
        self._lock = threading.RLock()
        self._stats = CacheStats()

    # --- Okuma --------------------------------------------------------------
    def get(self, key: CacheKey, *, now: datetime | None = None) -> CacheEntry | None:
        if not self._enabled:
            return None
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            bucket = self._buckets.get(key.tenant)
            if bucket is None:
                self._stats.misses += 1
                return None
            entry = bucket.get(key.digest)
            if entry is None:
                self._stats.misses += 1
                return None
            if not entry.is_fresh(now=moment):
                del bucket[key.digest]
                self._stats.evictions += 1
                self._stats.misses += 1
                return None
            # LRU: taze girdi sona tasinir.
            bucket.move_to_end(key.digest)
            self._stats.hits += 1
            return entry

    # --- Yazma --------------------------------------------------------------
    def set(self, key: CacheKey, entry: CacheEntry) -> None:
        if not self._enabled:
            return
        if entry.data_class is DataClass.D3:
            # Fail-closed: cagiran taraf yanlislikla D3 yazmaya kalkarsa
            # sessizce kabul edilmez, sayilir ve reddedilir.
            self._stats.rejected_by_class += 1
            log.debug("cache reddi | D3 veri cache'lenmez | tool=%s", entry.tool)
            return
        with self._lock:
            bucket = self._buckets.setdefault(key.tenant, OrderedDict())
            bucket[key.digest] = entry
            bucket.move_to_end(key.digest)
            self._stats.stores += 1
            while len(bucket) > self._max:
                bucket.popitem(last=False)
                self._stats.evictions += 1

    # --- Gecersiz kilma -----------------------------------------------------
    def invalidate_tags(self, tenant: str, tags: frozenset[str]) -> int:
        """Yazma sonrasi ilgili is nesnelerinin cache girdilerini temizler.

        Yalnizca verilen tenant'in bolmesine dokunur.
        """
        if not tags:
            return 0
        with self._lock:
            bucket = self._buckets.get(tenant)
            if not bucket:
                return 0
            doomed = [
                digest for digest, entry in bucket.items() if entry.tags & tags
            ]
            for digest in doomed:
                del bucket[digest]
            self._stats.invalidations += len(doomed)
            if doomed:
                log.info(
                    "cache invalidation | tenant=%s | etiket=%d | girdi=%d",
                    tenant, len(tags), len(doomed),
                )
            return len(doomed)

    def invalidate_tool(self, tenant: str, tool: str) -> int:
        """Bir tool'un tum girdilerini duser (sema/surum degisikliginde)."""
        with self._lock:
            bucket = self._buckets.get(tenant)
            if not bucket:
                return 0
            doomed = [digest for digest, entry in bucket.items() if entry.tool == tool]
            for digest in doomed:
                del bucket[digest]
            self._stats.invalidations += len(doomed)
            return len(doomed)

    def clear(self, *, tenant: str = "") -> int:
        with self._lock:
            if tenant:
                bucket = self._buckets.pop(tenant, None)
                return len(bucket) if bucket else 0
            total = sum(len(b) for b in self._buckets.values())
            self._buckets.clear()
            return total

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Suresi dolmus girdileri toplu temizler (retention job cagirir)."""
        moment = now or datetime.now(timezone.utc)
        removed = 0
        with self._lock:
            for bucket in self._buckets.values():
                doomed = [d for d, e in bucket.items() if not e.is_fresh(now=moment)]
                for digest in doomed:
                    del bucket[digest]
                removed += len(doomed)
            self._stats.evictions += removed
        return removed

    # --- Teshis -------------------------------------------------------------
    @property
    def stats(self) -> CacheStats:
        return self._stats

    @property
    def enabled(self) -> bool:
        return self._enabled

    def size(self, *, tenant: str = "") -> int:
        with self._lock:
            if tenant:
                return len(self._buckets.get(tenant, {}))
            return sum(len(b) for b in self._buckets.values())

    def tenants(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._buckets))
