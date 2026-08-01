"""Onay kaniti ve dogrulamasi.

"Kullanici sohbette evet dedi" onay kaniti degildir.
Gecerli bir onay kaydi sunlari tasir:

  - onaylayan kimligi ve rolu
  - onaylanan payload'in sha256 ozeti (baska payload'a tasinamaz)
  - gecerlilik suresi (expiry) ve tek kullanimlik nonce
  - workflow ornek kimligi (SAP Build Process Automation baglantisi)

`issue()` uretimde BPA callback'i tarafindan cagrilir. Bu repoda ayni imza
yerel/gelistirme akisi icin de kullanilir; fark yalnizca cagiranin kim oldugudur
(bkz. `adapters.bpa`).
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..contracts import ActorContext, RiskTier
from .audit import sha256_of
from .store import StateDatabase

log = logging.getLogger(__name__)


def payload_hash(payload: Any) -> str:
    """Onaya sunulan payload'in kanonik sha256 ozeti."""
    return sha256_of(payload)


class ApprovalError(RuntimeError):
    """Onay bulunamadi, gecersiz, suresi gecmis veya baska payload icin verilmis."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"error": str(self), "approval_code": self.code}


@dataclass(frozen=True)
class Approver:
    subject: str
    roles: tuple[str, ...]
    decided_at: str
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "roles": list(self.roles),
            "decided_at": self.decided_at,
            "comment": self.comment,
        }


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    tenant: str
    tool: str
    payload_sha256: str
    workflow_instance_id: str
    requested_by: str
    approvers: tuple[Approver, ...]
    nonce: str
    granted_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_by_execution: str = ""
    scope: dict[str, Any] | None = None

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "payload_sha256": self.payload_sha256,
            "workflow_instance_id": self.workflow_instance_id,
            "requested_by": self.requested_by,
            "approvers": [a.to_dict() for a in self.approvers],
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "consumed": self.consumed,
            "consumed_at": self.consumed_at.isoformat() if self.consumed_at else None,
            "scope": self.scope or {},
        }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ApprovalStore:
    """Kalici onay deposu."""

    def __init__(self, db: StateDatabase, *, default_ttl_minutes: int = 60) -> None:
        self._db = db
        self._ttl = timedelta(minutes=max(1, default_ttl_minutes))

    # --- Verme --------------------------------------------------------------
    def issue(
        self,
        *,
        tool: str,
        payload: Any,
        tenant: str,
        approvers: Sequence[ActorContext],
        requested_by: str = "",
        workflow_instance_id: str = "",
        ttl_minutes: int | None = None,
        comment: str = "",
        scope: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """Onay kaydi olusturur.

        `payload` onaylanan tam istek govdesidir; hash'i kaydedilir ve yurutmede
        birebir ayni payload beklenir.
        """
        if not approvers:
            raise ApprovalError("Onay kaydi en az bir onaylayan gerektirir.", code="NO_APPROVER")

        now = datetime.now(timezone.utc)
        ttl = timedelta(minutes=ttl_minutes) if ttl_minutes else self._ttl
        record = ApprovalRecord(
            approval_id=f"apr_{secrets.token_urlsafe(14)}",
            tenant=tenant,
            tool=tool,
            payload_sha256=payload_hash(payload),
            workflow_instance_id=workflow_instance_id or f"wf-local-{secrets.token_hex(6)}",
            requested_by=requested_by,
            approvers=tuple(
                Approver(subject=a.subject, roles=a.roles, decided_at=now.isoformat(), comment=comment)
                for a in approvers
            ),
            nonce=secrets.token_urlsafe(12),
            granted_at=now,
            expires_at=now + ttl,
            scope=scope or {},
        )
        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO approvals (approval_id, tenant, tool, payload_sha256,
                    workflow_instance_id, requested_by, approvers_json, nonce,
                    granted_at, expires_at, scope_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.approval_id,
                    record.tenant,
                    record.tool,
                    record.payload_sha256,
                    record.workflow_instance_id,
                    record.requested_by,
                    json.dumps([a.to_dict() for a in record.approvers], ensure_ascii=False),
                    record.nonce,
                    record.granted_at.isoformat(),
                    record.expires_at.isoformat(),
                    json.dumps(record.scope or {}, ensure_ascii=False),
                ),
            )
        log.info("Onay kaydi olusturuldu: %s (%s)", record.approval_id, tool)
        return record

    # --- Okuma --------------------------------------------------------------
    def get(self, approval_id: str) -> ApprovalRecord | None:
        row = self._db.query_one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row: Any) -> ApprovalRecord:
        approvers = tuple(
            Approver(
                subject=a.get("subject", ""),
                roles=tuple(a.get("roles", ())),
                decided_at=a.get("decided_at", ""),
                comment=a.get("comment", ""),
            )
            for a in json.loads(row["approvers_json"] or "[]")
        )
        return ApprovalRecord(
            approval_id=row["approval_id"],
            tenant=row["tenant"],
            tool=row["tool"],
            payload_sha256=row["payload_sha256"],
            workflow_instance_id=row["workflow_instance_id"],
            requested_by=row["requested_by"],
            approvers=approvers,
            nonce=row["nonce"],
            granted_at=_parse_dt(row["granted_at"]) or datetime.now(timezone.utc),
            expires_at=_parse_dt(row["expires_at"]) or datetime.now(timezone.utc),
            consumed_at=_parse_dt(row["consumed_at"]),
            consumed_by_execution=row["consumed_by_execution"] or "",
            scope=json.loads(row["scope_json"] or "{}"),
        )

    # --- Dogrulama ----------------------------------------------------------
    def validate(
        self,
        approval_id: str,
        *,
        tool: str,
        payload: Any,
        actor: ActorContext,
        risk_tier: RiskTier,
        approve_scope: str,
    ) -> ApprovalRecord:
        """Onayi tool + payload + actor + risk seviyesine karsi dogrular.

        Basarisiz her durum `ApprovalError` firlatir; policy gate bunu deny'e
        cevirir. Sessiz gecis yoktur.
        """
        record = self.get(approval_id)
        if record is None:
            raise ApprovalError(f"Onay kaydi bulunamadi: {approval_id}", code="NOT_FOUND")
        if record.tenant != actor.tenant:
            raise ApprovalError("Onay baska tenant icin verilmis.", code="TENANT_MISMATCH")
        if record.tool != tool:
            raise ApprovalError(
                f"Onay '{record.tool}' icin verilmis, cagrilan tool '{tool}'.", code="TOOL_MISMATCH"
            )
        if record.consumed:
            raise ApprovalError(
                f"Onay {approval_id} daha once kullanildi ({record.consumed_by_execution}).",
                code="ALREADY_CONSUMED",
            )
        if record.is_expired():
            raise ApprovalError(
                f"Onay {approval_id} {record.expires_at.isoformat()} tarihinde gecerliligini yitirdi.",
                code="EXPIRED",
            )

        expected = payload_hash(payload)
        if record.payload_sha256 != expected:
            raise ApprovalError(
                "Onaylanan payload ile yurutulen payload ayni degil. Degisiklik icin yeni onay alin.",
                code="PAYLOAD_MISMATCH",
            )

        # SoD: talebi hazirlayan kisi kendi talebini onaylayamaz.
        approver_subjects = {a.subject for a in record.approvers}
        if actor.subject in approver_subjects and len(approver_subjects) == 1:
            raise ApprovalError(
                "Gorevler ayrimi (SoD): yurutucu ile tek onaylayan ayni kisi olamaz.",
                code="SOD_VIOLATION",
            )
        missing_scope = [
            a.subject
            for a in record.approvers
            if approve_scope not in _scopes_of_roles(a.roles)
        ]
        if missing_scope:
            raise ApprovalError(
                f"Onaylayan(lar) {', '.join(missing_scope)} '{approve_scope}' yetkisine sahip degil.",
                code="APPROVER_NOT_AUTHORIZED",
            )
        if risk_tier.requires_dual_control and len(approver_subjects) < 2:
            raise ApprovalError(
                "R4 islemi cift onay gerektirir; kayitta tek onaylayan var.",
                code="DUAL_CONTROL_REQUIRED",
            )
        return record

    def consume(self, approval_id: str, *, execution_id: str) -> None:
        """Onayi tek kullanimlik hale getirir (replay engeli)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db.write() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals SET consumed_at = ?, consumed_by_execution = ?
                WHERE approval_id = ? AND consumed_at IS NULL
                """,
                (now, execution_id, approval_id),
            )
            if cursor.rowcount == 0:
                raise ApprovalError(
                    f"Onay {approval_id} zaten kullanilmis veya yok.", code="ALREADY_CONSUMED"
                )

    def purge_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._db.write() as conn:
            cursor = conn.execute(
                "DELETE FROM approvals WHERE expires_at < ? AND consumed_at IS NULL", (now,)
            )
            return cursor.rowcount


def _scopes_of_roles(roles: Iterable[str]) -> frozenset[str]:
    # Dairesel import olmasin diye burada import edilir.
    from ..contracts.actor import scopes_for_roles

    return scopes_for_roles(roles)
