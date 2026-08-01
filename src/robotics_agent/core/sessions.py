"""Kalici oturum durumu.

Sahiplik: oturum `(tenant, subject, session_id)` uclusuyle baglanir. Ayni
tenant'taki baska bir kullanici, session ID'sini bilse bile baskasinin
konusmasini yukleyemez, listeleyemez veya silemez. Tenant genelinde islem
yapmak `session.admin` kapsami ister.

Eszamanlilik: her kayitta `version` vardir ve guncelleme
`UPDATE ... WHERE version = ?` ile yapilir. Iki worker ayni oturumu ayni anda
kaydederse ikincisi `SessionConflict` alir; son yazan digerinin turunu sessizce
silmez.

Onemli: konusma gecmisi kaynak kayit degildir. Onay, idempotency ve audit kendi
depolarindadir; buradaki mesajlar yalniz diyalog surekliligi icindir.
"""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Settings
from ..contracts import ActorContext
from .store import StateDatabase, get_state_db

log = logging.getLogger(__name__)

# Tenant genelinde oturum listeleme/silme yetkisi.
SCOPE_SESSION_ADMIN = "session.admin"


class SessionConflict(RuntimeError):
    """Oturum baska bir yazici tarafindan guncellenmis (optimistic locking)."""


class SessionOwnershipError(PermissionError):
    """Oturum baska bir kullaniciya ait."""


@dataclass
class SessionRecord:
    session_id: str
    tenant: str
    subject: str
    created_at: datetime
    last_seen: datetime
    turn_count: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_packs: list[str] = field(default_factory=list)
    version: int = 1

    def owned_by(self, actor: ActorContext) -> bool:
        return self.tenant == actor.tenant and self.subject == actor.subject

    def to_summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "subject": self.subject,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "turn_count": self.turn_count,
            "message_count": len(self.messages),
            "active_packs": list(self.active_packs),
            "version": self.version,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SessionStore(ABC):
    """Oturum deposu sozlesmesi."""

    def __init__(self, *, ttl_hours: int = 24, max_sessions: int = 500) -> None:
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self.max_sessions = max(1, max_sessions)

    @abstractmethod
    def load(self, session_id: str, *, actor: ActorContext) -> SessionRecord | None: ...

    @abstractmethod
    def save(self, record: SessionRecord) -> None: ...

    @abstractmethod
    def delete(self, session_id: str, *, actor: ActorContext) -> bool: ...

    @abstractmethod
    def list(self, *, actor: ActorContext, limit: int = 50) -> list[SessionRecord]: ...

    @abstractmethod
    def purge_expired(self) -> int: ...

    @abstractmethod
    def exists_for_other_owner(self, session_id: str, *, actor: ActorContext) -> bool:
        """Bu ID baska bir kullaniciya mi ait? (cakisma tespiti)"""

    def create(self, *, actor: ActorContext, session_id: str | None = None) -> SessionRecord:
        now = _now()
        record = SessionRecord(
            session_id=session_id or uuid.uuid4().hex[:16],
            tenant=actor.tenant,
            subject=actor.subject,
            created_at=now,
            last_seen=now,
        )
        self.save(record)
        return record

    def get_or_create(
        self, session_id: str | None, *, actor: ActorContext
    ) -> tuple[SessionRecord, bool]:
        """(kayit, yeni_mi).

        Verilen ID baska bir kullaniciya aitse yeni kayit acilmaz;
        `SessionOwnershipError` firlatilir. Boylece istemci baskasinin ID'siyle
        kayit uzerine yazamaz.
        """
        if session_id:
            existing = self.load(session_id, actor=actor)
            if existing is not None:
                return existing, False
            if self.exists_for_other_owner(session_id, actor=actor):
                raise SessionOwnershipError(
                    f"Oturum {session_id} baska bir kullaniciya ait."
                )
        return self.create(actor=actor, session_id=session_id), True


class MemorySessionStore(SessionStore):
    """Test ve tek surecli calisma icin."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._records: dict[tuple[str, str, str], SessionRecord] = {}

    @staticmethod
    def _key(session_id: str, actor: ActorContext) -> tuple[str, str, str]:
        return (actor.tenant, actor.subject, session_id)

    def load(self, session_id: str, *, actor: ActorContext) -> SessionRecord | None:
        record = self._records.get(self._key(session_id, actor))
        if record is None:
            return None
        if _now() - record.last_seen > self.ttl:
            self._records.pop(self._key(session_id, actor), None)
            return None
        return record

    def exists_for_other_owner(self, session_id: str, *, actor: ActorContext) -> bool:
        return any(
            key[2] == session_id and (key[0], key[1]) != (actor.tenant, actor.subject)
            for key in self._records
        )

    def save(self, record: SessionRecord) -> None:
        key = (record.tenant, record.subject, record.session_id)
        current = self._records.get(key)
        if current is not None and current.version != record.version:
            raise SessionConflict(
                f"Oturum {record.session_id} bu arada guncellendi "
                f"(beklenen v{record.version}, mevcut v{current.version})."
            )
        record.last_seen = _now()
        record.version += 1
        self._records[key] = record
        if len(self._records) > self.max_sessions:
            oldest = min(self._records.items(), key=lambda kv: kv[1].last_seen)[0]
            self._records.pop(oldest, None)

    def delete(self, session_id: str, *, actor: ActorContext) -> bool:
        return self._records.pop(self._key(session_id, actor), None) is not None

    def list(self, *, actor: ActorContext, limit: int = 50) -> list[SessionRecord]:
        admin = actor.has_scope(SCOPE_SESSION_ADMIN)
        rows = [
            r
            for (tenant, subject, _), r in self._records.items()
            if tenant == actor.tenant and (admin or subject == actor.subject)
        ]
        rows.sort(key=lambda r: r.last_seen, reverse=True)
        return rows[:limit]

    def purge_expired(self) -> int:
        cutoff = _now() - self.ttl
        expired = [k for k, v in self._records.items() if v.last_seen < cutoff]
        for key in expired:
            self._records.pop(key, None)
        return len(expired)


class SQLiteSessionStore(SessionStore):
    """Restart ve coklu worker'a dayanikli oturum deposu."""

    def __init__(self, db: StateDatabase, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._db = db

    def load(self, session_id: str, *, actor: ActorContext) -> SessionRecord | None:
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE session_id = ? AND tenant = ? AND subject = ?",
            (session_id, actor.tenant, actor.subject),
        )
        if row is None:
            return None
        record = self._row_to_record(row)
        if _now() - record.last_seen > self.ttl:
            self.delete(session_id, actor=actor)
            return None
        return record

    def exists_for_other_owner(self, session_id: str, *, actor: ActorContext) -> bool:
        row = self._db.query_one(
            "SELECT 1 FROM sessions WHERE session_id = ? AND NOT (tenant = ? AND subject = ?) LIMIT 1",
            (session_id, actor.tenant, actor.subject),
        )
        return row is not None

    def save(self, record: SessionRecord) -> None:
        now = _now()
        payload = (
            now.isoformat(),
            record.turn_count,
            json.dumps(record.messages, ensure_ascii=False, default=str),
            json.dumps(record.active_packs, ensure_ascii=False),
        )
        with self._db.write() as conn:
            existing = conn.execute(
                "SELECT version FROM sessions WHERE tenant = ? AND subject = ? AND session_id = ?",
                (record.tenant, record.subject, record.session_id),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO sessions (session_id, tenant, subject, created_at, last_seen,
                        turn_count, messages_json, active_packs_json, version)
                    VALUES (?,?,?,?,?,?,?,?,1)
                    """,
                    (
                        record.session_id, record.tenant, record.subject,
                        record.created_at.isoformat(), *payload,
                    ),
                )
                record.version = 1
            else:
                # Optimistic locking: araya baska bir yazma girdiyse reddet.
                cursor = conn.execute(
                    """
                    UPDATE sessions
                       SET last_seen = ?, turn_count = ?, messages_json = ?,
                           active_packs_json = ?, version = version + 1
                     WHERE tenant = ? AND subject = ? AND session_id = ? AND version = ?
                    """,
                    (*payload, record.tenant, record.subject, record.session_id, record.version),
                )
                if cursor.rowcount == 0:
                    raise SessionConflict(
                        f"Oturum {record.session_id} bu arada guncellendi "
                        f"(beklenen v{record.version}, mevcut v{int(existing['version'])}). "
                        "Kaydi yeniden yukleyip tekrar deneyin."
                    )
                record.version += 1

            count = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
            if count > self.max_sessions:
                conn.execute(
                    "DELETE FROM sessions WHERE rowid IN ("
                    "  SELECT rowid FROM sessions ORDER BY last_seen ASC LIMIT ?"
                    ")",
                    (count - self.max_sessions,),
                )
        record.last_seen = now

    def delete(self, session_id: str, *, actor: ActorContext) -> bool:
        with self._db.write() as conn:
            if actor.has_scope(SCOPE_SESSION_ADMIN):
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND tenant = ?",
                    (session_id, actor.tenant),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND tenant = ? AND subject = ?",
                    (session_id, actor.tenant, actor.subject),
                )
            return cursor.rowcount > 0

    def list(self, *, actor: ActorContext, limit: int = 50) -> list[SessionRecord]:
        if actor.has_scope(SCOPE_SESSION_ADMIN):
            rows = self._db.query(
                "SELECT * FROM sessions WHERE tenant = ? ORDER BY last_seen DESC LIMIT ?",
                (actor.tenant, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM sessions WHERE tenant = ? AND subject = ? "
                "ORDER BY last_seen DESC LIMIT ?",
                (actor.tenant, actor.subject, limit),
            )
        return [self._row_to_record(r) for r in rows]

    def purge_expired(self) -> int:
        cutoff = (_now() - self.ttl).isoformat()
        with self._db.write() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE last_seen < ?", (cutoff,))
            return cursor.rowcount

    @staticmethod
    def _row_to_record(row: Any) -> SessionRecord:
        keys = row.keys()
        return SessionRecord(
            session_id=row["session_id"],
            tenant=row["tenant"],
            subject=row["subject"],
            created_at=_parse(row["created_at"]),
            last_seen=_parse(row["last_seen"]),
            turn_count=int(row["turn_count"] or 0),
            messages=json.loads(row["messages_json"] or "[]"),
            active_packs=json.loads(row["active_packs_json"] or "[]"),
            version=int(row["version"]) if "version" in keys and row["version"] else 1,
        )


def build_session_store(settings: Settings) -> SessionStore:
    kwargs = {
        "ttl_hours": settings.state.session_ttl_hours,
        "max_sessions": settings.state.max_sessions,
    }
    if settings.state.session_backend == "memory":
        return MemorySessionStore(**kwargs)
    return SQLiteSessionStore(get_state_db(settings.state.db_path), **kwargs)
