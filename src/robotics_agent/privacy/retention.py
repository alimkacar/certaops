"""Saklama politikasi ve periyodik purge.

Onceki surumde TTL temizligi uygulama baslangicina ve tekil erisimlere
bagliydi: uzun sure ayakta kalan bir surecte suresi dolmus oturum ve evidence
kayitlari diskte kalabilir. Bu modul su garantileri uygular:

  - Her veri turunun **acikca bildirilmis** bir saklama suresi vardir.
  - Temizlik periyodik bir job olarak calisir, istek yolunda degil.
  - Purge kucuk batch'lerle ilerler; tek bir buyuk DELETE
    veritabanini kilitleyip istek yolunu yavaslatmaz.
  - Silme islemi audit'e **veri icerigi olmadan** yazilir.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "RETENTION_POLICY",
    "PurgeReport",
    "RetentionRule",
    "RetentionSweeper",
]


@dataclass(frozen=True)
class RetentionRule:
    """Bir veri turunun saklama sozlesmesi."""

    name: str
    max_age_minutes: int
    table: str
    timestamp_column: str
    note: str = ""
    # Yasal saklama kapsamindaki veri kullanici oturum verisinden ayri
    # degerlendirilir; purge job bunlara dokunmaz.
    legal_hold: bool = False
    # Sutun mutlak son kullanma anini mi tutuyor (evidence.expires_at) yoksa
    # kaydin olusma anini mi (sessions.last_seen)? Ilkinde esik `simdi`dir,
    # ikincisinde `simdi - max_age`.
    absolute_expiry: bool = False

    @property
    def active(self) -> bool:
        """Purge job bu kurali calistirir mi?"""
        return not self.legal_hold and (self.absolute_expiry or self.max_age_minutes > 0)

    def cutoff(self, *, now: datetime | None = None) -> datetime:
        moment = now or datetime.now(timezone.utc)
        if self.absolute_expiry:
            return moment
        return moment - timedelta(minutes=self.max_age_minutes)


# Varsayilan saklama tablosu. Sureler ayarlardan override edilebilir; buradaki
# degerler "kurumsal politika verilmediginde" gecerli guvenli varsayilanlardir.
RETENTION_POLICY: tuple[RetentionRule, ...] = (
    RetentionRule(
        name="session",
        max_age_minutes=24 * 60,
        table="sessions",
        timestamp_column="last_seen",
        note="Sohbet/oturum durumu.",
    ),
    RetentionRule(
        name="evidence",
        max_age_minutes=120,
        table="evidence",
        timestamp_column="expires_at",
        note="Tam SAP payload'i; TTL kaydin kendisinde (expires_at) yazilidir.",
        absolute_expiry=True,
    ),
    RetentionRule(
        name="idempotency",
        max_age_minutes=7 * 24 * 60,
        table="idempotency",
        timestamp_column="created_at",
        note="Payload yerine hash tutulur; mutabakat penceresi kadar saklanir.",
    ),
    RetentionRule(
        name="approval",
        max_age_minutes=30 * 24 * 60,
        table="approvals",
        timestamp_column="granted_at",
        note="Onay kaydi; yasal ihtiyaca gore uzatilabilir.",
    ),
    RetentionRule(
        name="audit",
        max_age_minutes=0,
        table="audit_entries",
        timestamp_column="recorded_at",
        note="Yasal saklama kapsaminda; purge job dokunmaz.",
        legal_hold=True,
    ),
)


@dataclass
class PurgeReport:
    """Tek bir purge turunun sonucu. Veri icerigi tasimaz."""

    started_at: datetime
    deleted: dict[str, int]
    duration_ms: float
    batches: int = 0
    errors: tuple[str, ...] = ()

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "deleted": dict(self.deleted),
            "total_deleted": self.total_deleted,
            "duration_ms": round(self.duration_ms, 1),
            "batches": self.batches,
            "errors": list(self.errors),
        }


class RetentionSweeper:
    """Periyodik saklama suresi temizleyicisi.

    Tasarim notlari:
      - `batch_size` kucuk tutulur; her tabloda en fazla `max_batches` tur
        calisir. Boylece tek bir sweep uzun sure kilit tutmaz.
      - Job **idempotenttir**: yarida kesilse bir sonraki turda kaldigi yerden
        devam eder, kayip veya cift silme olmaz.
      - `legal_hold=True` kurallara hic dokunulmaz.
    """

    def __init__(
        self,
        db: Any,
        *,
        rules: tuple[RetentionRule, ...] = RETENTION_POLICY,
        batch_size: int = 500,
        max_batches: int = 20,
        audit_hook: Callable[[PurgeReport], None] | None = None,
    ) -> None:
        self._db = db
        self._rules = rules
        self._batch_size = max(50, batch_size)
        self._max_batches = max(1, max_batches)
        self._audit_hook = audit_hook
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    # --- Tek tur ------------------------------------------------------------
    def sweep(self, *, now: datetime | None = None) -> PurgeReport:
        started = now or datetime.now(timezone.utc)
        clock = time.perf_counter()
        deleted: dict[str, int] = {}
        errors: list[str] = []
        batches = 0

        for rule in self._rules:
            if not rule.active:
                continue
            try:
                count, used = self._purge_rule(rule, now=started)
            except Exception as exc:  # noqa: BLE001 - purge hatasi servisi durdurmaz
                # Hata mesajina tablo adi disinda icerik yazilmaz.
                errors.append(f"{rule.name}: {type(exc).__name__}")
                log.warning("retention purge hatasi | %s | %s", rule.name, type(exc).__name__)
                continue
            batches += used
            if count:
                deleted[rule.name] = count

        report = PurgeReport(
            started_at=started,
            deleted=deleted,
            duration_ms=(time.perf_counter() - clock) * 1000,
            batches=batches,
            errors=tuple(errors),
        )
        if report.total_deleted or report.errors:
            log.info(
                "retention purge | silinen=%d | batch=%d | %.0f ms",
                report.total_deleted, report.batches, report.duration_ms,
            )
        if self._audit_hook is not None:
            self._audit_hook(report)
        return report

    def _purge_rule(self, rule: RetentionRule, *, now: datetime) -> tuple[int, int]:
        """Bir kurali kucuk batch'lerle temizler. (silinen, batch_sayisi) doner."""
        cutoff = rule.cutoff(now=now).isoformat()
        total = 0
        batches = 0
        for _ in range(self._max_batches):
            with self._db.write() as conn:
                if not _table_exists(conn, rule.table):
                    return 0, 0
                rows = conn.execute(
                    f"SELECT rowid FROM {rule.table} "  # noqa: S608 - tablo adi sabit listeden
                    f"WHERE {rule.timestamp_column} IS NOT NULL "
                    f"AND {rule.timestamp_column} < ? LIMIT ?",
                    (cutoff, self._batch_size),
                ).fetchall()
                if not rows:
                    break
                ids = [row[0] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM {rule.table} WHERE rowid IN ({placeholders})",  # noqa: S608
                    ids,
                )
                total += len(ids)
                batches += 1
            if len(rows) < self._batch_size:
                break
        return total, batches

    # --- Zamanlayici --------------------------------------------------------
    def start(self, *, interval_seconds: int = 900) -> None:
        """Arka planda periyodik saklama temizligi baslatir."""
        with self._lock:
            if self._timer is not None:
                return
            self._stopped.clear()
            self._schedule(max(60, interval_seconds))

    def _schedule(self, interval: int) -> None:
        def _run() -> None:
            if self._stopped.is_set():
                return
            try:
                self.sweep()
            finally:
                if not self._stopped.is_set():
                    self._schedule(interval)

        timer = threading.Timer(interval, _run)
        timer.daemon = True
        timer.name = "retention-sweeper"
        timer.start()
        self._timer = timer

    def stop(self) -> None:
        with self._lock:
            self._stopped.set()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def policy_report(self) -> list[dict[str, Any]]:
        """`/health` ve teshis ciktilari icin okunabilir politika ozeti."""
        return [
            {
                "data": rule.name,
                "max_age_minutes": rule.max_age_minutes,
                "legal_hold": rule.legal_hold,
                "note": rule.note,
            }
            for rule in self._rules
        ]


def _table_exists(conn: Any, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None
