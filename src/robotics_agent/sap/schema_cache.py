"""Surec genelinde paylasilan `$metadata` ve servis-surumu onbellegi.

Neden gerekli
-------------
`SAPAgentRuntime.__init__` her ornek icin `build_backend()` cagirir ve
runtime'lar `(tenant, subject, session_id)` basina onbelleklenir. Yani her
YENI OTURUM taze bir backend, taze bir `$metadata` onbellegi demekti:

  - `$metadata` belgesi S/4HANA released servislerinde yuz kilobaytlar
    mertebesindedir ve indirilip XML olarak ayristirilir,
  - `_alias_for()` V4/V2 secimi icin ayrica sonda cagrilari yapar.

Bunlarin ikisi de **sistemin semasidir**, kullanicinin verisi degil. Ayni
sisteme baglanan ikinci bir oturumun bunlari bastan indirmesi icin hicbir
sebep yok; olcumde bu, her yeni oturumun ilk cevabina saniyeler ekliyordu.

Guvenlik siniri
---------------
Burada tutulan sey **sema**dir: entity tipleri, alan adlari, zorunluluklar.
Hicbir is kaydi, hicbir kullaniciya ozgu deger girmez. Yine de onbellek
anahtari baglanti kimligini (host + client + servis yolu) icerir: iki ayri
SAP sistemi ya da iki ayri client birbirinin semasini gormez.

Kullanici kimligi anahtara **girmez ve girmemelidir** - girseydi onbellek
kullanici basina bolunur ve tum fayda kaybolurdu. Bu guvenli, cunku
`$metadata` her kullanici icin aynidir ve yetkilendirme sema uzerinden
degil her cagrida policy katmaninda yapilir.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

_lock = threading.Lock()
_entries: dict[tuple[str, ...], Any] = {}


def cached(key: tuple[str, ...], produce: Callable[[], T]) -> T:
    """Anahtar icin degeri dondurur; yoksa uretip saklar.

    `produce` KILIT DISINDA calisir: `$metadata` indirmek saniyeler surebilir
    ve o sure boyunca kilidi tutmak, baska bir servisin semasini bekleyen
    thread'leri gereksiz yere durdururdu. Iki thread ayni anda ayni semayi
    indirirse ikisi de dogru sonuc uretir; kaybedilen tek sey bir kereye
    mahsus tekrar eden bir indirmedir.
    """
    with _lock:
        if key in _entries:
            return _entries[key]

    value = produce()

    with _lock:
        # Yarista kaybedersek ONCEKI degeri kullaniriz: ayni sema oldugu icin
        # fark yok, ama tek bir nesne paylasilmis olur.
        return _entries.setdefault(key, value)


def clear() -> None:
    """Onbellegi bosaltir (test ve `reset_backend` icin)."""
    with _lock:
        _entries.clear()


def size() -> int:
    with _lock:
        return len(_entries)
