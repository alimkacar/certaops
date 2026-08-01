"""Kalici durum icin ortak SQLite baglantisi.

Neden SQLite: onay kaydi, idempotency, oturum, audit ve evidence process yeniden
baslatildiginda kaybolmamali ve birden fazla uvicorn worker'i ayni gercekligi
gormeli. Uretimde PostgreSQL/HANA'ya gecis icin yalniz bu modul degisir.

Eszamanlilik garantileri:
  - WAL modu + `BEGIN IMMEDIATE`: yazma kilidi hemen alinir, boylece
    "oku-kontrol et-yaz" dizisi iki worker arasinda bolunmez. Bu kilit
    **process'ler arasi** calisir; `threading.Lock` calismaz.
  - `busy_timeout`: kilit bekleyen worker hemen hata almak yerine bekler.
  - Idempotency lease, onay rezervasyonu ve audit zincir basi ayni transaction
    icinde guncellenebilir.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

# Kilit bekleme suresi: paralel worker'lar "database is locked" yerine bekler.
BUSY_TIMEOUT_MS = 15_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id           TEXT PRIMARY KEY,
    tenant                TEXT NOT NULL,
    tool                  TEXT NOT NULL,
    payload_sha256        TEXT NOT NULL,
    workflow_instance_id  TEXT NOT NULL DEFAULT '',
    requested_by          TEXT NOT NULL DEFAULT '',
    approvers_json        TEXT NOT NULL DEFAULT '[]',
    nonce                 TEXT NOT NULL,
    granted_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    consumed_at           TEXT,
    consumed_by_execution TEXT,
    scope_json            TEXT NOT NULL DEFAULT '{}',
    -- Yazmadan ONCE alinan rezervasyon, ayni onayin iki eszamanli yurutmeyi
    -- yetkilendirmesini engeller.
    reserved_by_execution TEXT,
    reserved_at           TEXT,
    reservation_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS idempotency (
    key                TEXT NOT NULL,
    tenant             TEXT NOT NULL,
    tool               TEXT NOT NULL,
    payload_sha256     TEXT NOT NULL,
    status             TEXT NOT NULL,
    business_object_id TEXT NOT NULL DEFAULT '',
    result_json        TEXT,
    reason             TEXT NOT NULL DEFAULT '',
    attempts           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    execution_id       TEXT NOT NULL DEFAULT '',
    -- Lease: rezervasyonu hangi yurutme tutuyor ve ne zamana kadar gecerli.
    lease_owner        TEXT NOT NULL DEFAULT '',
    lease_expires_at   TEXT,
    PRIMARY KEY (tenant, key)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT NOT NULL,
    tenant       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    turn_count   INTEGER NOT NULL DEFAULT 0,
    messages_json TEXT NOT NULL DEFAULT '[]',
    active_packs_json TEXT NOT NULL DEFAULT '[]',
    -- Optimistic locking: iki worker ayni oturumu kaydederse son yazan
    -- digerinin turunu silmez.
    version      INTEGER NOT NULL DEFAULT 1,
    -- Oturum sahipligi tenant + subject ile baglanir.
    PRIMARY KEY (tenant, subject, session_id)
);

CREATE TABLE IF NOT EXISTS audit_entries (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_hash     TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    event          TEXT NOT NULL,
    execution_id   TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    tenant         TEXT NOT NULL DEFAULT '',
    tool           TEXT NOT NULL DEFAULT '',
    outcome        TEXT NOT NULL DEFAULT '',
    body_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id  TEXT PRIMARY KEY,
    tenant       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    tool         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limit (
    bucket_key  TEXT NOT NULL,
    window_id   INTEGER NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket_key, window_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen);
CREATE INDEX IF NOT EXISTS idx_idem_status ON idempotency(status);
CREATE INDEX IF NOT EXISTS idx_audit_execution ON audit_entries(execution_id);
CREATE INDEX IF NOT EXISTS idx_audit_correlation ON audit_entries(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_entries(tenant, seq);
CREATE INDEX IF NOT EXISTS idx_evidence_expiry ON evidence(expires_at);
"""

# Onceki surumlerden gelen veritabanlarina eklenen sutunlar.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("approvals", "reserved_by_execution", "TEXT"),
    ("approvals", "reserved_at", "TEXT"),
    ("approvals", "reservation_expires_at", "TEXT"),
    ("idempotency", "lease_owner", "TEXT NOT NULL DEFAULT ''"),
    ("idempotency", "lease_expires_at", "TEXT"),
    ("sessions", "version", "INTEGER NOT NULL DEFAULT 1"),
)


class StateDatabase:
    """Tek dosyalik durum veritabani. Thread- ve process-safe yazma saglar."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    # --- Baglanti -----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        # :memory: her baglantida yeni veritabani yaratir; paylasilan tek baglanti sart.
        if str(self.path) == ":memory:":
            if self._shared is None:
                self._shared = self._connect()
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self.connection
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            self._initialized = True

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Eski veritabanlarina eksik sutunlari ekler."""
        for table, column, definition in _MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:  # tablo yok; schema zaten olusturur
                continue
            if column not in existing:
                log.info("Durum semasi guncelleniyor: %s.%s", table, column)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Seri hale getirilmis yazma islemi.

        BEGIN IMMEDIATE yazma kilidini hemen alir; boylece "oku-kontrol et-yaz"
        dizisi iki worker (ve iki process) arasinda bolunmez.
        """
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params))

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._shared is not None:
            self._shared.close()
            self._shared = None


_DB_CACHE: dict[str, StateDatabase] = {}
_CACHE_LOCK = threading.Lock()


def get_state_db(path: Path | str) -> StateDatabase:
    """Ayni dosya icin tek StateDatabase ornegi."""
    key = str(path)
    with _CACHE_LOCK:
        db = _DB_CACHE.get(key)
        if db is None:
            db = StateDatabase(path)
            _DB_CACHE[key] = db
        return db


def reset_state_db_cache() -> None:
    """Testler arasinda temiz baslangic icin."""
    with _CACHE_LOCK:
        for db in _DB_CACHE.values():
            db.close()
        _DB_CACHE.clear()
