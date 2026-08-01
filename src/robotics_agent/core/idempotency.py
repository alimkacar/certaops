"""Idempotency deposu, lease yonetimi ve timeout sonrasi mutabakat.

Durumlar:
  reserved  -> bir yurutme lease tutuyor; sonuc henuz bilinmiyor
  completed -> is nesnesi olustu; ayni key ile ikinci cagri onu geri doner
  unknown   -> timeout/kesinti; SAP'ta olusup olusmadigi read-back ile bulunmali
  failed    -> is/teknik hata; kontrollu retry yapilabilir

**Lease neden var:** iki worker ayni idempotency key ile ayni anda cagri
yaparsa, ikincisi ilk SAP yazmasi bitmeden kaydi "reserved" gorur. Lease
olmadan bu durumda ikinci POST gonderilirdi. Lease sahibi baskasiysa ve suresi
gecmemisse cagri `IN_PROGRESS` ile geri cevrilir; asla yeniden POST edilmez.

Lease suresi dolduysa (worker cokmusse) sahiplik devralinir, fakat once
**mutabakat zorunludur**: `RECOVERED` durumu cagiriciya bunu bildirir.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from .store import StateDatabase

log = logging.getLogger(__name__)

IdempotencyStatus = Literal["reserved", "completed", "unknown", "failed"]

# Bir yurutmenin rezervasyonu ne kadar sure kendisine ait kalir.
DEFAULT_LEASE_SECONDS = 300
# Onay rezervasyonu da ayni pencerede tutulur.
DEFAULT_APPROVAL_RESERVATION_SECONDS = 300


class BeginOutcome(str, Enum):
    """`begin()` sonucunda cagiricinin ne yapmasi gerektigi."""

    NEW = "new"                  # rezervasyon alindi, yurutme yapilabilir
    COMPLETED = "completed"      # daha once tamamlandi, mevcut sonucu don
    IN_PROGRESS = "in_progress"  # baska yurutme lease tutuyor, YAZMA
    RECOVERED = "recovered"      # lease suresi gecmis, once mutabakat yap
    RETRY = "retry"              # ayni yurutmenin kontrollu tekrari


class IdempotencyConflict(RuntimeError):
    """Ayni anahtar farkli bir payload icin kullanilmis."""


class ApprovalReservationConflict(RuntimeError):
    """Onay baska bir yurutme tarafindan rezerve edilmis veya tuketilmis."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    tenant: str
    tool: str
    payload_sha256: str
    status: IdempotencyStatus
    business_object_id: str
    result: dict[str, Any] | None
    reason: str
    attempts: int
    created_at: str
    updated_at: str
    execution_id: str
    lease_owner: str = ""
    lease_expires_at: str | None = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"

    @property
    def needs_reconciliation(self) -> bool:
        return self.status in {"unknown", "reserved"}

    def lease_is_active(self, *, now: datetime | None = None) -> bool:
        expiry = _parse(self.lease_expires_at)
        if expiry is None:
            return False
        return (now or _now()) < expiry

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.key,
            "tool": self.tool,
            "status": self.status,
            "business_object_id": self.business_object_id or None,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason or None,
            "execution_id": self.execution_id or None,
            "lease_owner": self.lease_owner or None,
            "lease_active": self.lease_is_active(),
        }


@dataclass(frozen=True)
class BeginResult:
    outcome: BeginOutcome
    record: IdempotencyRecord

    @property
    def may_execute(self) -> bool:
        """Yalniz NEW ve RETRY durumlarinda SAP'a yazilabilir."""
        return self.outcome in {BeginOutcome.NEW, BeginOutcome.RETRY}


class IdempotencyStore:
    def __init__(self, db: StateDatabase, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._db = db
        self._lease = timedelta(seconds=max(30, lease_seconds))

    # --- Rezervasyon --------------------------------------------------------
    def begin(
        self,
        key: str,
        *,
        tenant: str,
        tool: str,
        payload_sha256: str,
        execution_id: str,
        approval_id: str = "",
    ) -> BeginResult:
        """Anahtari rezerve eder; istege bagli olarak onayi ayni transaction'da tutar.

        Onay rezervasyonu ile idempotency lease'i **tek transaction icinde**
        alinir; boylece ayni onay farkli anahtarlarla eszamanli olarak iki
        yazmayi yetkilendiremez.
        """
        now = _now()
        stamp = now.isoformat()
        lease_until = (now + self._lease).isoformat()

        with self._db.write() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency WHERE tenant = ? AND key = ?", (tenant, key)
            ).fetchone()

            if row is not None:
                existing = self._row_to_record(row)
                if existing.payload_sha256 != payload_sha256:
                    raise IdempotencyConflict(
                        f"Idempotency key '{key}' farkli bir payload icin kullanilmis "
                        f"(kayitli {existing.payload_sha256[:12]}, gelen {payload_sha256[:12]}). "
                        "Yeni islem icin yeni anahtar uretin."
                    )
                if existing.is_completed:
                    return BeginResult(BeginOutcome.COMPLETED, existing)

                # Baska bir yurutme aktif lease tutuyor: kesinlikle yazma.
                if (
                    existing.lease_owner
                    and existing.lease_owner != execution_id
                    and existing.lease_is_active(now=now)
                ):
                    return BeginResult(BeginOutcome.IN_PROGRESS, existing)

                took_over = bool(existing.lease_owner) and existing.lease_owner != execution_id
                if approval_id:
                    self._reserve_approval(conn, approval_id, execution_id, now)
                conn.execute(
                    "UPDATE idempotency SET attempts = attempts + 1, updated_at = ?, "
                    "lease_owner = ?, lease_expires_at = ? WHERE tenant = ? AND key = ?",
                    (stamp, execution_id, lease_until, tenant, key),
                )
                refreshed = self._row_to_record(
                    conn.execute(
                        "SELECT * FROM idempotency WHERE tenant = ? AND key = ?", (tenant, key)
                    ).fetchone()
                )
                outcome = BeginOutcome.RECOVERED if took_over else BeginOutcome.RETRY
                return BeginResult(outcome, refreshed)

            if approval_id:
                self._reserve_approval(conn, approval_id, execution_id, now)
            conn.execute(
                """
                INSERT INTO idempotency (key, tenant, tool, payload_sha256, status,
                    business_object_id, result_json, reason, attempts, created_at,
                    updated_at, execution_id, lease_owner, lease_expires_at)
                VALUES (?,?,?,?,'reserved','',NULL,'',1,?,?,?,?,?)
                """,
                (key, tenant, tool, payload_sha256, stamp, stamp, execution_id,
                 execution_id, lease_until),
            )
            created = self._row_to_record(
                conn.execute(
                    "SELECT * FROM idempotency WHERE tenant = ? AND key = ?", (tenant, key)
                ).fetchone()
            )
        return BeginResult(BeginOutcome.NEW, created)

    @staticmethod
    def _reserve_approval(conn: Any, approval_id: str, execution_id: str, now: datetime) -> None:
        """Onayi bu yurutmeye atomik olarak rezerve eder.

        Ayni transaction icinde calisir; boylece "onay gecerli mi" kontrolu ile
        "onayi kim kullaniyor" kaydi arasinda yaris kalmaz.
        """
        row = conn.execute(
            "SELECT consumed_at, reserved_by_execution, reservation_expires_at "
            "FROM approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalReservationConflict(f"Onay kaydi bulunamadi: {approval_id}")
        if row["consumed_at"]:
            raise ApprovalReservationConflict(
                f"Onay {approval_id} daha once tuketilmis."
            )
        holder = row["reserved_by_execution"]
        expiry = _parse(row["reservation_expires_at"])
        if holder and holder != execution_id and expiry is not None and now < expiry:
            raise ApprovalReservationConflict(
                f"Onay {approval_id} su anda {holder} yurutmesi tarafindan kullaniliyor."
            )
        until = (now + timedelta(seconds=DEFAULT_APPROVAL_RESERVATION_SECONDS)).isoformat()
        conn.execute(
            "UPDATE approvals SET reserved_by_execution = ?, reserved_at = ?, "
            "reservation_expires_at = ? WHERE approval_id = ?",
            (execution_id, now.isoformat(), until, approval_id),
        )

    def release_approval(self, approval_id: str, *, execution_id: str) -> None:
        """Yazma yapilmadan cikildiginda rezervasyonu birakir."""
        if not approval_id:
            return
        with self._db.write() as conn:
            conn.execute(
                "UPDATE approvals SET reserved_by_execution = NULL, reserved_at = NULL, "
                "reservation_expires_at = NULL "
                "WHERE approval_id = ? AND reserved_by_execution = ? AND consumed_at IS NULL",
                (approval_id, execution_id),
            )

    # --- Durum gecisleri ----------------------------------------------------
    def complete(
        self,
        key: str,
        *,
        tenant: str,
        business_object_id: str,
        result: dict[str, Any],
    ) -> None:
        self._set_status(
            key,
            tenant=tenant,
            status="completed",
            business_object_id=business_object_id,
            result=result,
            reason="",
            clear_lease=True,
        )

    def mark_unknown(self, key: str, *, tenant: str, reason: str) -> None:
        """Timeout/kesinti: sonuc bilinmiyor, mutabakat gerekiyor."""
        self._set_status(key, tenant=tenant, status="unknown", reason=reason, clear_lease=True)

    def fail(self, key: str, *, tenant: str, reason: str) -> None:
        self._set_status(key, tenant=tenant, status="failed", reason=reason, clear_lease=True)

    def _set_status(
        self,
        key: str,
        *,
        tenant: str,
        status: IdempotencyStatus,
        business_object_id: str = "",
        result: dict[str, Any] | None = None,
        reason: str = "",
        clear_lease: bool = False,
    ) -> None:
        now = _now().isoformat()
        with self._db.write() as conn:
            conn.execute(
                """
                UPDATE idempotency
                   SET status = ?, business_object_id = ?, result_json = ?, reason = ?,
                       updated_at = ?,
                       lease_owner = CASE WHEN ? THEN '' ELSE lease_owner END,
                       lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END
                 WHERE tenant = ? AND key = ?
                """,
                (
                    status,
                    business_object_id,
                    json.dumps(result, ensure_ascii=False, default=str) if result else None,
                    reason,
                    now,
                    1 if clear_lease else 0,
                    1 if clear_lease else 0,
                    tenant,
                    key,
                ),
            )

    # --- Sorgu --------------------------------------------------------------
    def get(self, key: str, *, tenant: str) -> IdempotencyRecord | None:
        row = self._db.query_one(
            "SELECT * FROM idempotency WHERE tenant = ? AND key = ?", (tenant, key)
        )
        return self._row_to_record(row) if row else None

    def pending(self, *, tenant: str, limit: int = 50) -> list[IdempotencyRecord]:
        """Mutabakat bekleyen kayitlar."""
        rows = self._db.query(
            "SELECT * FROM idempotency WHERE tenant = ? AND status IN ('reserved','unknown') "
            "ORDER BY updated_at DESC LIMIT ?",
            (tenant, limit),
        )
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: Any) -> IdempotencyRecord:
        keys = row.keys()
        return IdempotencyRecord(
            key=row["key"],
            tenant=row["tenant"],
            tool=row["tool"],
            payload_sha256=row["payload_sha256"],
            status=row["status"],
            business_object_id=row["business_object_id"] or "",
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            reason=row["reason"] or "",
            attempts=int(row["attempts"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            execution_id=row["execution_id"] or "",
            lease_owner=(row["lease_owner"] or "") if "lease_owner" in keys else "",
            lease_expires_at=row["lease_expires_at"] if "lease_expires_at" in keys else None,
        )


def build_idempotency_key(*parts: str) -> str:
    """`proje:senaryo:aksiyon:v1` bicimli deterministik anahtar uretir."""
    cleaned = [p.strip().replace(":", "-").replace(" ", "_") for p in parts if p and p.strip()]
    if not cleaned:
        raise ValueError("Idempotency anahtari icin en az bir parca gerekli.")
    return ":".join(cleaned)
