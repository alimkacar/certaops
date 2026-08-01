"""Cache anahtari, girdisi ve backend sozlesmesi.

Bir SAP okuma cache'inin en buyuk riski **yanlis kisiye dogru cevap** vermektir.
Ayni `sap_purchase_order_360` cagrisi iki farkli kullanicida farkli alanlar
dondurur (alan bazli yetki), farkli tenant'ta tamamen farkli veriye bakar.
Bu yuzden anahtar sadece tool adi + argumanlardan degil, tum yetki ve veri
izolasyonu boyutlarindan olusur:

    tenant + subject/role-scope-hash + system_alias + company_code + plant
    + purchasing_org + tool_version + normalized_arguments + detail_level

`subject` yerine **kapsam hash'i** kullanilir: ayni role ve ayni organizasyon
kapsamina sahip iki kullanici ayni cevabi gorur, cache paylasilir; kapsami
farkli olan kullanici asla ayni anahtara dusmez. Kullaniciya ozgu alan
donduren cevaplar `subject_bound=True` ile isaretlenir ve anahtara subject
eklenir; kullaniciya ozgu alan iceren cevap ortak tenant cache'ine yazilmaz.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from ..contracts.actor import ActorContext
from ..privacy.classification import DataClass

__all__ = [
    "CacheBackend",
    "CacheEntry",
    "CacheKey",
    "CachePolicy",
    "CacheStats",
    "build_cache_key",
]

# Cache disi tutulan sunum/teknik argumanlar: bunlar is sonucunu degistirmez
# ya da her cagride farklidir.
_NON_SEMANTIC_ARGS = frozenset({"idempotency_key", "approval_id", "include_evidence"})


@dataclass(frozen=True)
class CachePolicy:
    """Tool'un TTL, kapsam ve gecersiz kilma cache sozlesmesi."""

    ttl_seconds: int = 0
    vary_by: tuple[str, ...] = ("tenant", "subject", "company_code", "plant")
    # Cache'lenebilecek en yuksek veri sinifi. D3 hicbir kosulda cache'lenmez.
    max_class: DataClass = DataClass.D2
    # Cevap kullaniciya ozgu alan (kisisellestirilmis projeksiyon) iceriyor mu?
    subject_bound: bool = True
    # Bu tool'un sonucunu gecersiz kilan is nesnesi etiketleri.
    invalidated_by: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def allows(self, data_class: DataClass) -> bool:
        """D3 asla; digerleri tool'un bildirdigi tavana kadar."""
        if data_class is DataClass.D3:
            return False
        return data_class.level <= self.max_class.level

    def to_dict(self) -> dict[str, Any]:
        return {
            "ttl_seconds": self.ttl_seconds,
            "vary_by": list(self.vary_by),
            "max_class": self.max_class.value,
            "subject_bound": self.subject_bound,
            "invalidated_by": list(self.invalidated_by),
        }


NO_CACHE = CachePolicy(ttl_seconds=0)


@dataclass(frozen=True)
class CacheKey:
    """Hesaplanmis cache anahtari.

    `tenant` ayri tutulur cunku silme/invalidation her zaman tenant sinirlidir:
    bir tenant'in yazmasi baska tenant'in cache'ini temizleyemez.
    """

    tenant: str
    digest: str
    tool: str

    def __str__(self) -> str:
        return f"{self.tenant}:{self.tool}:{self.digest}"


@dataclass
class CacheEntry:
    """Saklanan cevap ve tazelik bilgisi."""

    payload: Any
    stored_at: datetime
    expires_at: datetime
    data_class: DataClass
    tool: str
    tags: frozenset[str] = frozenset()
    source_read_at: str = ""

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) < self.expires_at

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.stored_at).total_seconds()

    def freshness(self) -> dict[str, Any]:
        """Cache hit cevabina eklenen kaynak ve yas bilgisi.

        Model, verinin "az once okundu" mu yoksa "60 saniyelik" mi oldugunu
        bilmeden termin veya tutar taahhudu vermemelidir.
        """
        return {
            "cached": True,
            "age_seconds": round(self.age_seconds, 1),
            "source_read_at": self.source_read_at,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass
class CacheStats:
    """Cache isabet, kacirma ve tahliye telemetrisi."""

    hits: int = 0
    misses: int = 0
    stores: int = 0
    evictions: int = 0
    invalidations: int = 0
    rejected_by_class: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total * 100, 1) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "rejected_by_class": self.rejected_by_class,
            "hit_rate_pct": self.hit_rate,
        }


class CacheBackend(Protocol):
    """Cache uygulamalarinin ortak sozlesmesi.

    Redis/Valkey gibi bir backend'e gecerken yalnizca bu protokolu karsilayan
    yeni bir sinif yazilir; cagiran taraf degismez.
    """

    def get(self, key: CacheKey) -> CacheEntry | None: ...

    def set(self, key: CacheKey, entry: CacheEntry) -> None: ...

    def invalidate_tags(self, tenant: str, tags: frozenset[str]) -> int: ...

    def clear(self, *, tenant: str = "") -> int: ...

    @property
    def stats(self) -> CacheStats: ...


def build_cache_key(
    *,
    tool: str,
    tool_version: str,
    actor: ActorContext,
    system_alias: str,
    arguments: Mapping[str, Any],
    detail: str,
    policy: CachePolicy,
    org_defaults: Mapping[str, str] | None = None,
) -> CacheKey:
    """Tenant, actor, organizasyon ve tool boyutlarini iceren anahtar uretir.

    Anahtar **hash'lenir**: ham argumanlar (malzeme numarasi, tedarikci adi)
    cache anahtarinda acikta durmaz; Redis'e gecildiginde anahtar listesi
    tek basina is bilgisi sizdirmaz.
    """
    defaults = org_defaults or {}
    parts: list[str] = [
        f"tenant={actor.tenant}",
        f"tool={tool}",
        f"version={tool_version}",
        f"system={system_alias}",
        f"detail={detail}",
        f"scope={_scope_hash(actor)}",
        f"company_code={_org_value(arguments, 'company_code', defaults)}",
        f"plant={_org_value(arguments, 'plant', defaults)}",
        f"purchasing_org={_org_value(arguments, 'purchasing_org', defaults)}",
        f"args={_normalize_arguments(arguments)}",
    ]
    if policy.subject_bound:
        # Kullaniciya ozgu projeksiyon donen cevap ortak tenant cache'ine
        # yazilmaz; anahtar subject'e baglanir.
        parts.append(f"subject={actor.subject}")
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return CacheKey(tenant=actor.tenant, digest=digest, tool=tool)


def _scope_hash(actor: ActorContext) -> str:
    """Rol/kapsam ve organizasyon yetkisinin kararli ozeti.

    Kapsam degisen bir kullanici (rol kaybi, tesis daralmasi) eski cache'e
    **dusmez**: anahtar degisir, cevap yeniden uretilir. Bu, yetki daraltmanin
    cache uzerinden atlatilamamasini garanti eder.
    """
    material = json.dumps(
        {
            "scopes": sorted(actor.scopes),
            "company_codes": sorted(actor.company_codes),
            "plants": sorted(actor.plants),
            "purchasing_orgs": sorted(actor.purchasing_orgs),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _org_value(arguments: Mapping[str, Any], key: str, defaults: Mapping[str, str]) -> str:
    raw = arguments.get(key)
    if isinstance(raw, list | tuple | set):
        return ",".join(sorted(str(v) for v in raw))
    if raw not in (None, ""):
        return str(raw)
    return str(defaults.get(key, ""))


def _normalize_arguments(arguments: Mapping[str, Any]) -> str:
    """Argumanlari kararli, anlam tasiyan bir metne cevirir.

    Sozluk siralamasi ve liste ici sozluk siralamasi normalize edilir ki
    `{"a":1,"b":2}` ile `{"b":2,"a":1}` ayni anahtari uretsin.
    """
    cleaned = {
        key: value
        for key, value in arguments.items()
        if key not in _NON_SEMANTIC_ARGS and value not in (None, "")
    }
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class _Noop:
    """Cache kapaliyken kullanilan bos backend."""

    _stats: CacheStats = field(default_factory=CacheStats)

    def get(self, key: CacheKey) -> CacheEntry | None:  # noqa: ARG002
        return None

    def set(self, key: CacheKey, entry: CacheEntry) -> None:  # noqa: ARG002
        return None

    def invalidate_tags(self, tenant: str, tags: frozenset[str]) -> int:  # noqa: ARG002
        return 0

    def clear(self, *, tenant: str = "") -> int:  # noqa: ARG002
        return 0

    @property
    def stats(self) -> CacheStats:
        return self._stats


def null_cache() -> CacheBackend:
    """Cache devre disi birakildiginda kullanilan backend."""
    return _Noop()


def entry_for(
    payload: Any,
    *,
    tool: str,
    data_class: DataClass,
    ttl_seconds: int,
    tags: frozenset[str] = frozenset(),
    source_read_at: str = "",
    now: datetime | None = None,
) -> CacheEntry:
    moment = now or datetime.now(timezone.utc)
    return CacheEntry(
        payload=payload,
        stored_at=moment,
        expires_at=moment + timedelta(seconds=max(1, ttl_seconds)),
        data_class=data_class,
        tool=tool,
        tags=tags,
        source_read_at=source_read_at or moment.isoformat(),
    )
