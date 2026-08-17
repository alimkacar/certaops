"""SAP cagrilari icin devre kesici.

Retry tek bir cagriyi kurtarir; devre kesici **sistemi** kurtarir. SAP tarafinda
bir kesinti oldugunda her istegin timeout'a kadar beklemesi uc sorun uretir:

  1. Kullanici her soruda `timeout x retry` kadar bekler.
  2. Thread havuzu dolar; saglikli tenant'lar da etkilenir.
  3. Toparlanmaya calisan SAP sistemi yeniden istek yagmuruna tutulur.

Kesici uc durumludur:

    closed     -> normal calisma; ardisik hata sayilir.
    open       -> `reset_seconds` boyunca cagri **gonderilmez**, hizli hata.
    half_open  -> tek bir deneme cagrisi gecer; basarirsa kapanir, hata verirse
                  yeniden acilir ve bekleme suresi bastan baslar.

## Neyi hata sayar

Yalniz **altyapi** hatalari devreyi acar: timeout, ag hatasi, 5xx, 429.
Is/yetki hatalari (400, 401, 403, 404, 409, 412) sayilmaz. Bu ayrim onemli:
yetkisiz bir kullanicinin 50 kez 403 almasi SAP'in saglikli oldugunu gosterir,
devreyi acmasi diger kullanicilari cezalandirmak olurdu.

## Yazma cagrilari

Devre acikken yazma **istegi hic gonderilmez**. Bu belirsizlik uretmez, tam
tersini yapar: `CircuitOpen` istegin SAP'a ULASMADIGINI garanti eder, dolayisiyla
cagiran taraf mutabakat aramak zorunda kalmaz. Ucusta yakalanan bir yazma ise
kesiciden bagimsiz olarak `core.execution` idempotency/mutabakat yoluna duser.

Saat `monotonic` uzerinden ve disaridan verilebilir; testler gercek zaman
beklemeden tum durum gecislerini dogrular.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import SAPError

__all__ = ["BreakerState", "CircuitBreaker", "CircuitOpen"]

BreakerState = Literal["closed", "open", "half_open"]

#: Devreyi acan HTTP durumlari. Is/yetki hatalari bilincli olarak disaridadir.
INFRASTRUCTURE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504, 507, 509})


class CircuitOpen(SAPError):
    """Devre acik: istek SAP'a **gonderilmedi**."""

    def __init__(self, name: str, *, retry_after_s: float, failures: int) -> None:
        super().__init__(
            f"SAP devre kesicisi acik ({name}). Son {failures} cagri altyapi hatasi "
            f"verdi; istek gonderilmedi. Yaklasik {retry_after_s:.0f} saniye sonra "
            "tekrar denenecek.",
            code="CIRCUIT_OPEN",
            detail=name,
        )
        self.name = name
        self.retry_after_s = retry_after_s
        self.failures = failures
        #: Cagiran taraf icin acik sozlesme: istek gonderilmedi, SAP'ta yan etki yok.
        self.request_sent = False


@dataclass
class CircuitBreaker:
    """Tek bir SAP sistemi/hostu icin ardisik hata sayaci.

    Ornek her SAP sistemi icin birdir: bir sistemin kesintisi digerini kapatmaz.
    Thread-safe; ayni cekirdek birden fazla worker tarafindan kullanilabilir.
    """

    name: str = "sap"
    enabled: bool = True
    failure_threshold: int = 5
    reset_seconds: float = 30.0
    clock: Callable[[], float] = time.monotonic

    _state: BreakerState = field(default="closed", init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _probe_in_flight: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # Telemetri: kac istek kesici tarafindan engellendi, kac kez acildi.
    _short_circuited: int = field(default=0, init=False)
    _opened_count: int = field(default=0, init=False)

    # --- Kapi ---------------------------------------------------------------
    def allow(self) -> None:
        """Cagri gonderilebilir mi? Gonderilemezse ``CircuitOpen`` firlatir."""
        if not self.enabled:
            return
        with self._lock:
            if self._state == "closed":
                return
            if self._state == "open":
                waited = self.clock() - self._opened_at
                if waited < self.reset_seconds:
                    self._short_circuited += 1
                    raise CircuitOpen(
                        self.name,
                        retry_after_s=max(0.0, self.reset_seconds - waited),
                        failures=self._failures,
                    )
                # Bekleme doldu: tek bir deneme cagrisina izin ver.
                self._state = "half_open"
                self._probe_in_flight = True
                return
            # half_open: ayni anda yalniz bir deneme ucar.
            if self._probe_in_flight:
                self._short_circuited += 1
                raise CircuitOpen(
                    self.name, retry_after_s=self.reset_seconds, failures=self._failures
                )
            self._probe_in_flight = True

    # --- Geri bildirim ------------------------------------------------------
    def record_success(self) -> None:
        """Basarili cagri sayaci sifirlar ve devreyi kapatir."""
        if not self.enabled:
            return
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._probe_in_flight = False

    def record_failure(self) -> None:
        """Altyapi hatasi. Esik asilirsa devre acilir."""
        if not self.enabled:
            return
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._state == "half_open" or self._failures >= self.failure_threshold:
                if self._state != "open":
                    self._opened_count += 1
                self._state = "open"
                self._opened_at = self.clock()

    def record_ignored(self) -> None:
        """Is/yetki hatasi: saglik gostergesi degil, sayac degismez.

        Deneme cagrisi kilidini birakir; aksi halde yetkisiz tek bir cagri
        devreyi kalici half_open'da kilitlerdi.
        """
        if not self.enabled:
            return
        with self._lock:
            self._probe_in_flight = False

    @staticmethod
    def counts_as_failure(status: int) -> bool:
        """Bu HTTP durumu altyapi hatasi mi?"""
        return status in INFRASTRUCTURE_STATUS

    # --- Gozlemlenebilirlik -------------------------------------------------
    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "enabled": self.enabled,
                "state": self._state,
                "consecutive_failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "reset_seconds": self.reset_seconds,
                "short_circuited_calls": self._short_circuited,
                "opened_count": self._opened_count,
            }

    def reset(self) -> None:
        """Testler ve manuel mudahale icin temiz baslangic."""
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._opened_at = 0.0
            self._probe_in_flight = False


def null_breaker(name: str = "sap") -> CircuitBreaker:
    """Kesici devre disi birakildiginda kullanilan ornek."""
    return CircuitBreaker(name=name, enabled=False)


def breaker_for(cfg: Any, *, name: str = "") -> CircuitBreaker:
    """`SAPSettings` degerlerinden bir kesici uretir.

    Ornek **sistem basinadir**: bir SAP sisteminin kesintisi digerini kapatmaz.
    Ayni sistemin V2 ve V4 cekirdekleri ayni ornegi paylasmalidir; aksi halde
    esik iki kat yuksege cikar.
    """
    return CircuitBreaker(
        name=name or str(getattr(cfg, "system_alias", "") or "sap"),
        enabled=bool(getattr(cfg, "breaker_enabled", True)),
        failure_threshold=max(1, int(getattr(cfg, "breaker_failure_threshold", 5))),
        reset_seconds=max(0.0, float(getattr(cfg, "breaker_reset_seconds", 30.0))),
    )
