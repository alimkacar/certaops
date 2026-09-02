"""Kalici oturum durumu.

Sahiplik: oturum `(tenant, subject, session_id)` uclusuyle baglanir. Ayni
tenant'taki baska bir kullanici, session ID'sini bilse bile baskasinin
konusmasini yukleyemez, listeleyemez veya silemez. Tenant genelinde islem
yapmak `session.admin` kapsami ister.

Eszamanlilik: her kayitta `version` ve sureli bir tur lease'i vardir. Lease
hydrate -> model/tool -> save zincirinden once alinir; ikinci worker model veya
SAP calistirmadan `SessionBusy` alir. `UPDATE ... WHERE version = ?` kontrolu de
lease kaybi ya da depo disi yazmalara karsi son savunmadir.

Onemli: konusma gecmisi kaynak kayit degildir. Onay, idempotency ve audit kendi
depolarindadir; buradaki mesajlar yalniz diyalog surekliligi icindir.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import TOOL_TIMEOUT_CEILING_SECONDS, Settings
from ..contracts import ActorContext
from ..security_at_rest import decrypt_if_needed, maybe_cipher
from .store import StateDatabase, get_state_db

log = logging.getLogger(__name__)

# Tenant genelinde oturum listeleme/silme yetkisi.
SCOPE_SESSION_ADMIN = "session.admin"


class SessionConflict(RuntimeError):
    """Oturum baska bir yazici tarafindan guncellenmis (optimistic locking)."""


class SessionBusy(RuntimeError):
    """Oturumda baska bir istek model/tool turu calistiriyor."""


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


@dataclass
class _SessionTurnLease:
    """Store icindeki tek bir hydrate -> chat -> save sahipligi."""

    record: SessionRecord
    created: bool
    token: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SessionStore(ABC):
    """Oturum deposu sozlesmesi."""

    def __init__(
        self,
        *,
        ttl_hours: int = 24,
        max_sessions: int = 500,
        turn_lease_seconds: int = 900,
    ) -> None:
        self.ttl = timedelta(hours=max(1, ttl_hours))
        self.max_sessions = max(1, max_sessions)
        self.turn_lease_seconds = max(30, turn_lease_seconds)

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

    @abstractmethod
    def _acquire_turn(
        self, session_id: str | None, *, actor: ActorContext
    ) -> _SessionTurnLease: ...

    @abstractmethod
    def _commit_turn(self, lease: _SessionTurnLease) -> None: ...

    @abstractmethod
    def _release_turn(self, lease: _SessionTurnLease) -> None: ...

    @contextmanager
    def turn(
        self, session_id: str | None, *, actor: ActorContext
    ) -> Iterator[tuple[SessionRecord, bool]]:
        """Bir session turunu model/tool calismadan once tek-yazici yap.

        Context basariyla kapanirsa kayit lease token'i ile atomik kaydedilir;
        hata halinde lease serbest birakilir ve kismi transcript yazilmaz.
        """
        lease = self._acquire_turn(session_id, actor=actor)
        try:
            yield lease.record, lease.created
        except BaseException:
            self._release_turn(lease)
            raise
        else:
            try:
                self._commit_turn(lease)
            except BaseException:
                self._release_turn(lease)
                raise

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
                raise SessionOwnershipError(f"Oturum {session_id} baska bir kullaniciya ait.")
        return self.create(actor=actor, session_id=session_id), True


class MemorySessionStore(SessionStore):
    """Test ve tek surecli calisma icin."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._records: dict[tuple[str, str, str], SessionRecord] = {}
        self._guard = threading.RLock()
        self._turn_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._active_leases: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def _key(session_id: str, actor: ActorContext) -> tuple[str, str, str]:
        return (actor.tenant, actor.subject, session_id)

    def load(self, session_id: str, *, actor: ActorContext) -> SessionRecord | None:
        key = self._key(session_id, actor)
        with self._guard:
            record = self._records.get(key)
            if record is None:
                return None
            if _now() - record.last_seen > self.ttl and key not in self._active_leases:
                self._records.pop(key, None)
                return None
            # Cagiranin mutable degisiklikleri depodaki optimistic-lock
            # snapshot'ini degistirmemeli.
            return deepcopy(record)

    def exists_for_other_owner(self, session_id: str, *, actor: ActorContext) -> bool:
        with self._guard:
            return any(
                key[2] == session_id and (key[0], key[1]) != (actor.tenant, actor.subject)
                for key in self._records
            )

    def save(self, record: SessionRecord) -> None:
        key = (record.tenant, record.subject, record.session_id)
        with self._guard:
            if key in self._active_leases:
                raise SessionBusy(f"Oturum {record.session_id} su anda isleniyor.")
            current = self._records.get(key)
            if current is not None and current.version != record.version:
                raise SessionConflict(
                    f"Oturum {record.session_id} bu arada guncellendi "
                    f"(beklenen v{record.version}, mevcut v{current.version})."
                )
            record.last_seen = _now()
            if current is not None:
                record.version += 1
            else:
                record.version = 1
            self._records[key] = deepcopy(record)
            self._evict_oldest_unleased()

    def delete(self, session_id: str, *, actor: ActorContext) -> bool:
        with self._guard:
            if actor.has_scope(SCOPE_SESSION_ADMIN):
                keys = [
                    key for key in self._records if key[0] == actor.tenant and key[2] == session_id
                ]
            else:
                keys = [self._key(session_id, actor)]
            if any(key in self._active_leases for key in keys):
                raise SessionBusy(f"Oturum {session_id} su anda isleniyor.")
            deleted = False
            for key in keys:
                deleted = self._records.pop(key, None) is not None or deleted
                if key not in self._active_leases:
                    self._turn_locks.pop(key, None)
            return deleted

    def list(self, *, actor: ActorContext, limit: int = 50) -> list[SessionRecord]:
        with self._guard:
            admin = actor.has_scope(SCOPE_SESSION_ADMIN)
            rows = [
                deepcopy(r)
                for (tenant, subject, _), r in self._records.items()
                if tenant == actor.tenant and (admin or subject == actor.subject)
            ]
            rows.sort(key=lambda r: r.last_seen, reverse=True)
            return rows[:limit]

    def purge_expired(self) -> int:
        cutoff = _now() - self.ttl
        with self._guard:
            expired = [
                key
                for key, value in self._records.items()
                if value.last_seen < cutoff and key not in self._active_leases
            ]
            for key in expired:
                self._records.pop(key, None)
                self._turn_locks.pop(key, None)
            return len(expired)

    def _acquire_turn(self, session_id: str | None, *, actor: ActorContext) -> _SessionTurnLease:
        chosen_id = session_id or uuid.uuid4().hex[:16]
        key = self._key(chosen_id, actor)
        with self._guard:
            turn_lock = self._turn_locks.setdefault(key, threading.Lock())
        if not turn_lock.acquire(blocking=False):
            raise SessionBusy(f"Oturum {chosen_id} icin baska bir tur calisiyor.")

        token = uuid.uuid4().hex
        try:
            with self._guard:
                if any(
                    other[2] == chosen_id and (other[0], other[1]) != (actor.tenant, actor.subject)
                    for other in self._records
                ):
                    raise SessionOwnershipError(f"Oturum {chosen_id} baska bir kullaniciya ait.")
                record = self._records.get(key)
                if record is not None and _now() - record.last_seen > self.ttl:
                    self._records.pop(key, None)
                    record = None
                created = record is None
                if created:
                    now = _now()
                    record = SessionRecord(
                        session_id=chosen_id,
                        tenant=actor.tenant,
                        subject=actor.subject,
                        created_at=now,
                        last_seen=now,
                    )
                    self._records[key] = deepcopy(record)
                self._active_leases[key] = token
                return _SessionTurnLease(deepcopy(record), created, token)
        except BaseException:
            turn_lock.release()
            raise

    def _commit_turn(self, lease: _SessionTurnLease) -> None:
        record = lease.record
        key = (record.tenant, record.subject, record.session_id)
        turn_lock: threading.Lock | None = None
        try:
            with self._guard:
                if self._active_leases.get(key) != lease.token:
                    raise SessionConflict(
                        f"Oturum {record.session_id} tur lease'i artik gecerli degil."
                    )
                current = self._records.get(key)
                if current is None or current.version != record.version:
                    current_version = current.version if current else "silinmis"
                    raise SessionConflict(
                        f"Oturum {record.session_id} bu arada guncellendi "
                        f"(beklenen v{record.version}, mevcut {current_version})."
                    )
                record.last_seen = _now()
                record.version += 1
                self._records[key] = deepcopy(record)
                self._active_leases.pop(key, None)
                turn_lock = self._turn_locks.get(key)
                self._evict_oldest_unleased()
        finally:
            if turn_lock is not None:
                turn_lock.release()

    def _release_turn(self, lease: _SessionTurnLease) -> None:
        record = lease.record
        key = (record.tenant, record.subject, record.session_id)
        turn_lock: threading.Lock | None = None
        with self._guard:
            if self._active_leases.get(key) == lease.token:
                self._active_leases.pop(key, None)
                turn_lock = self._turn_locks.get(key)
        if turn_lock is not None and turn_lock.locked():
            turn_lock.release()

    def _evict_oldest_unleased(self) -> None:
        overflow = len(self._records) - self.max_sessions
        if overflow <= 0:
            return
        candidates = sorted(
            (
                (key, record)
                for key, record in self._records.items()
                if key not in self._active_leases
            ),
            key=lambda item: item[1].last_seen,
        )
        for key, _ in candidates[:overflow]:
            self._records.pop(key, None)
            self._turn_locks.pop(key, None)


class SQLiteSessionStore(SessionStore):
    """Restart ve coklu worker'a dayanikli oturum deposu."""

    def __init__(self, db: StateDatabase, cipher: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._db = db
        # `AGENT_SESSION_ENCRYPTION` acikken kurulan AES-GCM zarfi. Konusma
        # gecmisi SAP is verisi ve kullanici sorulari tasir; diskte sifrelenen
        # alan `messages_json`dur. Sahiplik/TTL sutunlari sorgulanabilir kalir.
        self._cipher = cipher

    def _encode_messages(self, messages: Any) -> str:
        body = json.dumps(messages, ensure_ascii=False, default=str)
        return self._cipher.encrypt(body) if self._cipher is not None else body

    def load(self, session_id: str, *, actor: ActorContext) -> SessionRecord | None:
        row = self._db.query_one(
            "SELECT * FROM sessions WHERE session_id = ? AND tenant = ? AND subject = ?",
            (session_id, actor.tenant, actor.subject),
        )
        if row is None:
            return None
        record = self._row_to_record(row)
        now = _now()
        if now - record.last_seen > self.ttl and not self._lease_is_active(row, now):
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
            self._encode_messages(record.messages),
            json.dumps(record.active_packs, ensure_ascii=False),
        )
        with self._db.write() as conn:
            existing = conn.execute(
                "SELECT version, turn_lease_owner, turn_lease_expires_at "
                "FROM sessions WHERE tenant = ? AND subject = ? AND session_id = ?",
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
                        record.session_id,
                        record.tenant,
                        record.subject,
                        record.created_at.isoformat(),
                        *payload,
                    ),
                )
                record.version = 1
            else:
                if self._lease_is_active(existing, now):
                    raise SessionBusy(f"Oturum {record.session_id} su anda isleniyor.")
                # Optimistic locking: araya baska bir yazma girdiyse reddet.
                cursor = conn.execute(
                    """
                    UPDATE sessions
                       SET last_seen = ?, turn_count = ?, messages_json = ?,
                           active_packs_json = ?, version = version + 1,
                           turn_lease_owner = '', turn_lease_expires_at = NULL
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
                    "  SELECT rowid FROM sessions "
                    "   WHERE COALESCE(turn_lease_owner, '') = '' "
                    "      OR turn_lease_expires_at IS NULL "
                    "      OR turn_lease_expires_at <= ? "
                    "   ORDER BY last_seen ASC LIMIT ?"
                    ")",
                    (now.isoformat(), count - self.max_sessions),
                )
        record.last_seen = now

    def delete(self, session_id: str, *, actor: ActorContext) -> bool:
        now = _now()
        with self._db.write() as conn:
            if actor.has_scope(SCOPE_SESSION_ADMIN):
                rows = conn.execute(
                    "SELECT turn_lease_owner, turn_lease_expires_at FROM sessions "
                    "WHERE session_id = ? AND tenant = ?",
                    (session_id, actor.tenant),
                ).fetchall()
                if any(self._lease_is_active(row, now) for row in rows):
                    raise SessionBusy(f"Oturum {session_id} su anda isleniyor.")
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE session_id = ? AND tenant = ?",
                    (session_id, actor.tenant),
                )
            else:
                row = conn.execute(
                    "SELECT turn_lease_owner, turn_lease_expires_at FROM sessions "
                    "WHERE session_id = ? AND tenant = ? AND subject = ?",
                    (session_id, actor.tenant, actor.subject),
                ).fetchone()
                if row is not None and self._lease_is_active(row, now):
                    raise SessionBusy(f"Oturum {session_id} su anda isleniyor.")
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
        now = _now()
        cutoff = (now - self.ttl).isoformat()
        with self._db.write() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE last_seen < ? AND ("
                "COALESCE(turn_lease_owner, '') = '' "
                "OR turn_lease_expires_at IS NULL OR turn_lease_expires_at <= ?)",
                (cutoff, now.isoformat()),
            )
            return cursor.rowcount

    def _acquire_turn(self, session_id: str | None, *, actor: ActorContext) -> _SessionTurnLease:
        chosen_id = session_id or uuid.uuid4().hex[:16]
        token = uuid.uuid4().hex
        now = _now()
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=self.turn_lease_seconds)).isoformat()
        created = False

        with self._db.write() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND tenant = ? AND subject = ?",
                (chosen_id, actor.tenant, actor.subject),
            ).fetchone()
            if row is None:
                other = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ? "
                    "AND NOT (tenant = ? AND subject = ?) LIMIT 1",
                    (chosen_id, actor.tenant, actor.subject),
                ).fetchone()
                if other is not None:
                    raise SessionOwnershipError(f"Oturum {chosen_id} baska bir kullaniciya ait.")
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, tenant, subject, created_at, last_seen,
                        turn_count, messages_json, active_packs_json, version,
                        turn_lease_owner, turn_lease_expires_at
                    ) VALUES (?,?,?,?,?,0,'[]','[]',1,?,?)
                    """,
                    (
                        chosen_id,
                        actor.tenant,
                        actor.subject,
                        now_iso,
                        now_iso,
                        token,
                        expires_at,
                    ),
                )
                created = True
            else:
                record = self._row_to_record(row)
                if now - record.last_seen > self.ttl:
                    if self._lease_is_active(row, now):
                        raise SessionBusy(f"Oturum {chosen_id} icin baska bir tur calisiyor.")
                    conn.execute(
                        "DELETE FROM sessions WHERE session_id = ? AND tenant = ? AND subject = ?",
                        (chosen_id, actor.tenant, actor.subject),
                    )
                    conn.execute(
                        """
                        INSERT INTO sessions (
                            session_id, tenant, subject, created_at, last_seen,
                            turn_count, messages_json, active_packs_json, version,
                            turn_lease_owner, turn_lease_expires_at
                        ) VALUES (?,?,?,?,?,0,'[]','[]',1,?,?)
                        """,
                        (
                            chosen_id,
                            actor.tenant,
                            actor.subject,
                            now_iso,
                            now_iso,
                            token,
                            expires_at,
                        ),
                    )
                    created = True
                else:
                    cursor = conn.execute(
                        """
                        UPDATE sessions
                           SET turn_lease_owner = ?, turn_lease_expires_at = ?
                         WHERE session_id = ? AND tenant = ? AND subject = ?
                           AND (COALESCE(turn_lease_owner, '') = ''
                                OR turn_lease_expires_at IS NULL
                                OR turn_lease_expires_at <= ?)
                        """,
                        (
                            token,
                            expires_at,
                            chosen_id,
                            actor.tenant,
                            actor.subject,
                            now_iso,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise SessionBusy(f"Oturum {chosen_id} icin baska bir tur calisiyor.")

            leased_row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND tenant = ? AND subject = ?",
                (chosen_id, actor.tenant, actor.subject),
            ).fetchone()
            assert leased_row is not None
            return _SessionTurnLease(self._row_to_record(leased_row), created, token)

    def _commit_turn(self, lease: _SessionTurnLease) -> None:
        record = lease.record
        now = _now()
        payload = (
            now.isoformat(),
            record.turn_count,
            self._encode_messages(record.messages),
            json.dumps(record.active_packs, ensure_ascii=False),
        )
        with self._db.write() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                   SET last_seen = ?, turn_count = ?, messages_json = ?,
                       active_packs_json = ?, version = version + 1,
                       turn_lease_owner = '', turn_lease_expires_at = NULL
                 WHERE tenant = ? AND subject = ? AND session_id = ?
                   AND version = ? AND turn_lease_owner = ?
                """,
                (
                    *payload,
                    record.tenant,
                    record.subject,
                    record.session_id,
                    record.version,
                    lease.token,
                ),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT version, turn_lease_owner FROM sessions "
                    "WHERE tenant = ? AND subject = ? AND session_id = ?",
                    (record.tenant, record.subject, record.session_id),
                ).fetchone()
                current_version = int(current["version"]) if current else "silinmis"
                raise SessionConflict(
                    f"Oturum {record.session_id} lease veya surum cakismasi "
                    f"(beklenen v{record.version}, mevcut {current_version})."
                )
        record.last_seen = now
        record.version += 1

    def _release_turn(self, lease: _SessionTurnLease) -> None:
        record = lease.record
        with self._db.write() as conn:
            conn.execute(
                """
                UPDATE sessions
                   SET turn_lease_owner = '', turn_lease_expires_at = NULL
                 WHERE tenant = ? AND subject = ? AND session_id = ?
                   AND turn_lease_owner = ?
                """,
                (record.tenant, record.subject, record.session_id, lease.token),
            )

    @staticmethod
    def _lease_is_active(row: Any, now: datetime) -> bool:
        keys = row.keys()
        if "turn_lease_owner" not in keys or not row["turn_lease_owner"]:
            return False
        expires_at = row["turn_lease_expires_at"]
        return bool(expires_at and _parse(expires_at) > now)

    def _row_to_record(self, row: Any) -> SessionRecord:
        keys = row.keys()
        return SessionRecord(
            session_id=row["session_id"],
            tenant=row["tenant"],
            subject=row["subject"],
            created_at=_parse(row["created_at"]),
            last_seen=_parse(row["last_seen"]),
            turn_count=int(row["turn_count"] or 0),
            messages=json.loads(decrypt_if_needed(self._cipher, row["messages_json"] or "[]")),
            active_packs=json.loads(row["active_packs_json"] or "[]"),
            version=int(row["version"]) if "version" in keys and row["version"] else 1,
        )


def build_session_store(settings: Settings) -> SessionStore:
    # Bir model istegi tum retry'lerde timeout olabilir; her iterasyonda buna
    # ek olarak izin verilen en uzun tool handler timeout'u yasanabilir. Lease
    # bu ust siniri asar; calisan ilk tur ikinci worker tarafindan devralinmaz.
    model_request_budget = settings.model.timeout_s * (settings.model.max_retries + 1)
    iteration_budget = max(
        settings.agent.max_tool_iterations,
        *(limit for _, limit in settings.agent.iteration_limits),
    )
    tool_request_budget = max(float(settings.sap.timeout), TOOL_TIMEOUT_CEILING_SECONDS)
    worst_case_turn_seconds = (
        (model_request_budget + tool_request_budget) * iteration_budget + model_request_budget + 60
    )
    kwargs = {
        "ttl_hours": settings.state.session_ttl_hours,
        "max_sessions": settings.state.max_sessions,
        "turn_lease_seconds": max(300, int(worst_case_turn_seconds)),
    }
    if settings.state.session_backend == "memory":
        return MemorySessionStore(**kwargs)
    # `maybe_cipher` ayar kapaliyken None doner, acik ama kurulamiyorsa
    # YUKSELIR. Bu yol bilerek fail-closed: sifreleme istendigi halde duz
    # metin yazan bir depo, hic sifreleme sunmamaktan daha kotudur.
    cipher = maybe_cipher(settings.privacy.session_encryption, purpose="session")
    return SQLiteSessionStore(get_state_db(settings.state.db_path), cipher=cipher, **kwargs)
