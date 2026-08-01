"""Kanit (evidence) sozlesmesi ve erisim kontrollu evidence store.

Tam SAP payload'i konusmaya eklenmez. Model ozet gorur, tam kayit tenant/actor
bazli yetkilendirilen opak bir handle ile store'da tutulur.
Handle tahmin edilemez (`secrets.token_urlsafe`).

Kayit `(tenant, subject)` ile baglanir.
Ayni tenant'taki baska bir kullanici handle'i ele gecirse bile payload'a
erisemez. Paylasim gerekiyorsa `shared_with` listesine acikca eklenmelidir.
"""

from __future__ import annotations

import json
import secrets
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .actor import ActorContext


@dataclass(frozen=True)
class Evidence:
    """Bir tool sonucunun nereden, ne zaman ve hangi kalitede geldigi."""

    source_system: str
    source_api: str
    read_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    business_object: str = ""
    etag: str = ""
    record_count: int = 0
    # Gercek SAP verisi yerine tahmin/fallback kullanilan alanlar acikca isaretlenir.
    estimated_fields: tuple[str, ...] = ()
    truncated: bool = False
    correlation_id: str = ""
    notes: tuple[str, ...] = ()

    @property
    def freshness_seconds(self) -> float:
        return round((datetime.now(timezone.utc) - self.read_at).total_seconds(), 3)

    @property
    def has_estimates(self) -> bool:
        return bool(self.estimated_fields)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_system": self.source_system,
            "source_api": self.source_api,
            "read_at": self.read_at.isoformat(),
        }
        if self.business_object:
            payload["business_object"] = self.business_object
        if self.etag:
            payload["etag"] = self.etag
        if self.record_count:
            payload["record_count"] = self.record_count
        if self.estimated_fields:
            payload["estimated_fields"] = list(self.estimated_fields)
            payload["estimated"] = True
        if self.truncated:
            payload["truncated"] = True
        if self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Evidence:
        read_at = payload.get("read_at")
        return cls(
            source_system=payload.get("source_system", ""),
            source_api=payload.get("source_api", ""),
            read_at=datetime.fromisoformat(read_at) if read_at else datetime.now(timezone.utc),
            business_object=payload.get("business_object", ""),
            etag=payload.get("etag", ""),
            record_count=int(payload.get("record_count", 0) or 0),
            estimated_fields=tuple(payload.get("estimated_fields", ())),
            truncated=bool(payload.get("truncated", False)),
            correlation_id=payload.get("correlation_id", ""),
            notes=tuple(payload.get("notes", ())),
        )


class EvidenceAccessDenied(PermissionError):
    """Baska tenant/actor'a ait evidence handle'i istendi."""


@dataclass
class _EvidenceEntry:
    evidence_id: str
    tenant: str
    subject: str
    tool: str
    payload: Any
    evidence: Evidence
    created_at: datetime
    expires_at: datetime
    shared_with: tuple[str, ...] = ()

    def readable_by(self, actor: ActorContext) -> bool:
        if self.tenant != actor.tenant:
            return False
        return actor.subject == self.subject or actor.subject in self.shared_with


class BaseEvidenceStore(ABC):
    """Evidence deposu sozlesmesi."""

    @abstractmethod
    def put(
        self,
        payload: Any,
        *,
        actor: ActorContext,
        tool: str,
        evidence: Evidence,
        shared_with: Iterable[str] = (),
    ) -> str: ...

    @abstractmethod
    def get(self, evidence_id: str, *, actor: ActorContext) -> dict[str, Any]: ...

    @abstractmethod
    def stats(self) -> dict[str, int]: ...

    @staticmethod
    def _new_id() -> str:
        return f"ev_{secrets.token_urlsafe(18)}"

    @staticmethod
    def _to_payload(entry: _EvidenceEntry) -> dict[str, Any]:
        return {
            "evidence_id": entry.evidence_id,
            "tool": entry.tool,
            "created_at": entry.created_at.isoformat(),
            "expires_at": entry.expires_at.isoformat(),
            "evidence": entry.evidence.to_dict(),
            "payload": entry.payload,
        }


class EvidenceStore(BaseEvidenceStore):
    """Bellekte tutulan, TTL'li ve (tenant, subject) bagli kanit deposu.

    Tek surecli calismada (CLI, demo, test) yeterlidir. Coklu worker'da handle
    baska bir worker'a giderse bulunamaz; bu yuzden API kurulumunda
    `SQLiteEvidenceStore` kullanilir.
    """

    def __init__(self, *, ttl_minutes: int = 120, max_entries: int = 500) -> None:
        self._ttl = timedelta(minutes=max(1, ttl_minutes))
        self._max_entries = max(10, max_entries)
        self._entries: dict[str, _EvidenceEntry] = {}
        self._lock = threading.Lock()

    def put(
        self,
        payload: Any,
        *,
        actor: ActorContext,
        tool: str,
        evidence: Evidence,
        shared_with: Iterable[str] = (),
    ) -> str:
        now = datetime.now(timezone.utc)
        evidence_id = self._new_id()
        entry = _EvidenceEntry(
            evidence_id=evidence_id,
            tenant=actor.tenant,
            subject=actor.subject,
            tool=tool,
            payload=payload,
            evidence=evidence,
            created_at=now,
            expires_at=now + self._ttl,
            shared_with=tuple(shared_with),
        )
        with self._lock:
            self._purge_locked(now)
            if len(self._entries) >= self._max_entries:
                oldest = min(self._entries.values(), key=lambda e: e.created_at)
                self._entries.pop(oldest.evidence_id, None)
            self._entries[evidence_id] = entry
        return evidence_id

    def get(self, evidence_id: str, *, actor: ActorContext) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._purge_locked(now)
            entry = self._entries.get(evidence_id)
        if entry is None:
            raise KeyError(evidence_id)
        # Sahiplik ihlali "bulunamadi" degil "reddedildi" olarak ayrilir;
        # log tarafinda anomali tespiti icin ayirt edilebilir olmali.
        if not entry.readable_by(actor):
            raise EvidenceAccessDenied(evidence_id)
        return self._to_payload(entry)

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._purge_locked(datetime.now(timezone.utc))
            return {"entries": len(self._entries), "max_entries": self._max_entries}

    def _purge_locked(self, now: datetime) -> None:
        expired = [k for k, v in self._entries.items() if v.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


class SQLiteEvidenceStore(BaseEvidenceStore):
    """Worker'lar arasi paylasilan, kalici evidence deposu.

    Coklu uvicorn worker'inda bir turda uretilen handle, sonraki turda baska bir
    worker'a dusse bile cozulebilir.
    """

    def __init__(self, db: Any, *, ttl_minutes: int = 120, max_entries: int = 500) -> None:
        self._db = db
        self._ttl = timedelta(minutes=max(1, ttl_minutes))
        self._max_entries = max(10, max_entries)

    def put(
        self,
        payload: Any,
        *,
        actor: ActorContext,
        tool: str,
        evidence: Evidence,
        shared_with: Iterable[str] = (),
    ) -> str:
        now = datetime.now(timezone.utc)
        evidence_id = self._new_id()
        body = {"payload": payload, "shared_with": list(shared_with)}
        with self._db.write() as conn:
            conn.execute("DELETE FROM evidence WHERE expires_at <= ?", (now.isoformat(),))
            conn.execute(
                """
                INSERT INTO evidence (evidence_id, tenant, subject, tool, payload_json,
                    evidence_json, created_at, expires_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    evidence_id,
                    actor.tenant,
                    actor.subject,
                    tool,
                    json.dumps(body, ensure_ascii=False, default=str),
                    json.dumps(evidence.to_dict(), ensure_ascii=False),
                    now.isoformat(),
                    (now + self._ttl).isoformat(),
                ),
            )
            count = conn.execute("SELECT COUNT(*) AS c FROM evidence").fetchone()["c"]
            if count > self._max_entries:
                conn.execute(
                    "DELETE FROM evidence WHERE evidence_id IN ("
                    "  SELECT evidence_id FROM evidence ORDER BY created_at ASC LIMIT ?"
                    ")",
                    (count - self._max_entries,),
                )
        return evidence_id

    def get(self, evidence_id: str, *, actor: ActorContext) -> dict[str, Any]:
        row = self._db.query_one(
            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
        )
        if row is None:
            raise KeyError(evidence_id)
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= datetime.now(timezone.utc):
            raise KeyError(evidence_id)
        body = json.loads(row["payload_json"] or "{}")
        entry = _EvidenceEntry(
            evidence_id=row["evidence_id"],
            tenant=row["tenant"],
            subject=row["subject"],
            tool=row["tool"],
            payload=body.get("payload"),
            evidence=Evidence.from_dict(json.loads(row["evidence_json"] or "{}")),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
            shared_with=tuple(body.get("shared_with", ())),
        )
        if not entry.readable_by(actor):
            raise EvidenceAccessDenied(evidence_id)
        return self._to_payload(entry)

    def stats(self) -> dict[str, int]:
        row = self._db.query_one("SELECT COUNT(*) AS c FROM evidence")
        return {"entries": int(row["c"]) if row else 0, "max_entries": self._max_entries}
