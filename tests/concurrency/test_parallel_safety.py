"""Eszamanlilik ve tek-yazma guvenlik testleri.

Kontrol edilen kabul kriterleri:

  - Ayni idempotency key ile N paralel istekte **tam bir** SAP belgesi olusur.
  - Ayni approval ID ve farkli idempotency key'lerle paralel cagrida **tam bir**
    yurutme yetkilendirilir.
  - En az dort yazici ayni anda audit yazdiginda zincir gecerli kalir.
  - Iki worker ayni oturumu kaydederse tur kaybolmaz (optimistic locking).

Testler hem thread hem de **ayri process** duzeyinde calisir: `threading.Lock`
ile cozulmus gibi gorunen bir yaris, process sinirinda geri gelir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from robotics_agent.contracts import ActorContext
from robotics_agent.core import (
    ApprovalReservationConflict,
    ApprovalStore,
    AuditLedger,
    BeginOutcome,
    IdempotencyStore,
    MemorySessionStore,
    SessionBusy,
    SessionConflict,
    SQLiteSessionStore,
    get_state_db,
)

PARALLEL = 24
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --- Idempotency: tek yazma garantisi --------------------------------------
def test_parallel_begin_grants_exactly_one_executor(tmp_path):
    """N paralel cagriden yalniz biri yazma hakki alir."""
    store = IdempotencyStore(get_state_db(tmp_path / "s.sqlite3"))

    def attempt(index: int) -> BeginOutcome:
        return store.begin(
            "proje:kalem:pr:v1",
            tenant="100",
            tool="sap_pr_submit",
            payload_sha256="h",
            execution_id=f"exec-{index}",
        ).outcome

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        outcomes = list(pool.map(attempt, range(PARALLEL)))

    assert outcomes.count(BeginOutcome.NEW) == 1, outcomes
    # Kalanlarin tamami "baskasi calisiyor" demeli; hicbiri yazamaz.
    assert all(o is BeginOutcome.IN_PROGRESS for o in outcomes if o is not BeginOutcome.NEW)


def test_parallel_begin_across_processes(tmp_path):
    """Ayri process'lerde de tek yazici: SQLite BEGIN IMMEDIATE process-safe."""
    db_path = tmp_path / "s.sqlite3"
    get_state_db(db_path)  # semayi olustur

    script = textwrap.dedent(
        """
        import json, sys
        sys.path.insert(0, %r)
        from robotics_agent.core import IdempotencyStore, get_state_db
        store = IdempotencyStore(get_state_db(sys.argv[1]))
        result = store.begin(
            "k:parallel:v1", tenant="100", tool="t", payload_sha256="h",
            execution_id=sys.argv[2],
        )
        print(json.dumps({"outcome": result.outcome.value}))
        """
    ) % str(PROJECT_ROOT / "src")

    script_path = tmp_path / "worker.py"
    script_path.write_text(script, encoding="utf-8")

    procs = [
        subprocess.Popen(
            [sys.executable, str(script_path), str(db_path), f"exec-{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "AGENT_STATE_DIR": str(tmp_path)},
        )
        for i in range(6)
    ]
    outcomes = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
        outcomes.append(json.loads(out.decode())["outcome"])

    assert outcomes.count("new") == 1, outcomes
    assert set(outcomes) <= {"new", "in_progress"}


# --- Onay rezervasyonu ------------------------------------------------------
def test_same_approval_authorizes_exactly_one_execution(tmp_path, approver):
    """Ayni onay farkli idempotency key'lerle iki yazmayi yetkilendiremez."""
    db = get_state_db(tmp_path / "s.sqlite3")
    approvals = ApprovalStore(db)
    idem = IdempotencyStore(db)
    record = approvals.issue(
        tool="sap_pr_submit", payload={"items": []}, tenant="100", approvers=[approver]
    )

    results: list[str] = []

    def attempt(index: int) -> str:
        try:
            outcome = idem.begin(
                f"kalem-{index}:pr:v1",
                tenant="100",
                tool="sap_pr_submit",
                payload_sha256=f"h{index}",
                execution_id=f"exec-{index}",
                approval_id=record.approval_id,
            )
            return outcome.outcome.value
        except ApprovalReservationConflict:
            return "approval_conflict"

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        results = list(pool.map(attempt, range(PARALLEL)))

    # Yalniz bir yurutme onayi rezerve edebilir.
    assert results.count("new") == 1, results
    assert results.count("approval_conflict") == PARALLEL - 1


def test_consumed_approval_cannot_be_reserved(tmp_path, approver):
    db = get_state_db(tmp_path / "s.sqlite3")
    approvals = ApprovalStore(db)
    idem = IdempotencyStore(db)
    record = approvals.issue(tool="t", payload={"a": 1}, tenant="100", approvers=[approver])
    approvals.consume(record.approval_id, execution_id="exec-1")

    with pytest.raises(ApprovalReservationConflict):
        idem.begin(
            "k:v1",
            tenant="100",
            tool="t",
            payload_sha256="h",
            execution_id="exec-2",
            approval_id=record.approval_id,
        )


def test_released_approval_can_be_reserved_again(tmp_path, approver):
    """Yazma kanitlanmadan cikildiysa rezervasyon birakilir."""
    db = get_state_db(tmp_path / "s.sqlite3")
    approvals = ApprovalStore(db)
    idem = IdempotencyStore(db)
    record = approvals.issue(tool="t", payload={"a": 1}, tenant="100", approvers=[approver])
    idem.begin(
        "k1:v1",
        tenant="100",
        tool="t",
        payload_sha256="h1",
        execution_id="exec-1",
        approval_id=record.approval_id,
    )
    idem.release_approval(record.approval_id, execution_id="exec-1")

    result = idem.begin(
        "k2:v1",
        tenant="100",
        tool="t",
        payload_sha256="h2",
        execution_id="exec-2",
        approval_id=record.approval_id,
    )
    assert result.outcome is BeginOutcome.NEW


# --- Audit zinciri ----------------------------------------------------------
def test_parallel_audit_writes_keep_chain_valid(tmp_path, purchaser):
    """Dort+ yazici es zamanli yazsa da hash zinciri gecerli kalir."""
    db = get_state_db(tmp_path / "audit.sqlite3")
    ledgers = [AuditLedger(db) for _ in range(4)]

    def write(index: int) -> None:
        ledgers[index % len(ledgers)].append(
            "tool.completed", actor=purchaser, tool=f"t{index}", outcome="ok"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    verification = AuditLedger(db).verify()
    assert verification["valid"] is True, verification
    assert verification["entries"] == 40


def test_parallel_audit_across_processes(tmp_path, purchaser):
    db_path = tmp_path / "audit.sqlite3"
    get_state_db(db_path)

    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %r)
        from robotics_agent.contracts import ActorContext
        from robotics_agent.core import AuditLedger, get_state_db
        ledger = AuditLedger(get_state_db(sys.argv[1]))
        actor = ActorContext(subject=sys.argv[2], tenant="100", roles=("VIEWER",))
        for i in range(10):
            ledger.append("tool.completed", actor=actor, tool=f"t{i}", outcome="ok")
        """
    ) % str(PROJECT_ROOT / "src")
    script_path = tmp_path / "audit_worker.py"
    script_path.write_text(script, encoding="utf-8")

    procs = [
        subprocess.Popen(
            [sys.executable, str(script_path), str(db_path), f"user{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "AGENT_STATE_DIR": str(tmp_path)},
        )
        for i in range(4)
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, err.decode()

    verification = AuditLedger(get_state_db(db_path)).verify()
    assert verification["valid"] is True, verification
    assert verification["entries"] == 40


# --- Oturum tutarliligi -----------------------------------------------------
def test_concurrent_session_save_detects_lost_update(tmp_path, purchaser):
    """Iki worker ayni oturumu kaydederse ikincisi cakismayi gorur."""
    store = SQLiteSessionStore(get_state_db(tmp_path / "s.sqlite3"), ttl_hours=1)
    record = store.create(actor=purchaser)

    first = store.load(record.session_id, actor=purchaser)
    second = store.load(record.session_id, actor=purchaser)

    first.messages = [{"role": "user", "content": "birinci tur"}]
    store.save(first)

    second.messages = [{"role": "user", "content": "ikinci tur"}]
    with pytest.raises(SessionConflict):
        store.save(second)

    # Ilk turun verisi kaybolmamis olmali.
    reloaded = store.load(record.session_id, actor=purchaser)
    assert reloaded.messages[0]["content"] == "birinci tur"


def test_parallel_session_saves_have_single_winner(tmp_path, purchaser):
    store = SQLiteSessionStore(get_state_db(tmp_path / "s.sqlite3"), ttl_hours=1)
    created = store.create(actor=purchaser)
    snapshots = [store.load(created.session_id, actor=purchaser) for _ in range(8)]

    def save(index: int) -> str:
        record = snapshots[index]
        record.messages = [{"role": "user", "content": f"tur-{index}"}]
        try:
            store.save(record)
            return "ok"
        except SessionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, range(8)))

    assert results.count("ok") == 1, results


def test_memory_session_snapshots_detect_lost_update(purchaser):
    """load(), depodaki mutable kaydi cagirana sizdirmamali."""
    store = MemorySessionStore(ttl_hours=1)
    created = store.create(actor=purchaser)
    first = store.load(created.session_id, actor=purchaser)
    second = store.load(created.session_id, actor=purchaser)
    assert first is not None and second is not None and first is not second

    first.messages = [{"role": "user", "content": "birinci"}]
    store.save(first)
    second.messages = [{"role": "user", "content": "ikinci"}]
    with pytest.raises(SessionConflict):
        store.save(second)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_session_turn_lease_blocks_before_parallel_work(backend, tmp_path, purchaser):
    """Ikinci ayni-session turu ilk callback bitmeden store'da kesilir."""
    store = (
        MemorySessionStore(ttl_hours=1)
        if backend == "memory"
        else SQLiteSessionStore(get_state_db(tmp_path / "lease.sqlite3"), ttl_hours=1)
    )
    created = store.create(actor=purchaser)
    entered = Event()
    release = Event()

    def first_turn() -> None:
        with store.turn(created.session_id, actor=purchaser) as (record, _):
            entered.set()
            assert release.wait(timeout=5)
            record.turn_count += 1

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_turn)
        assert entered.wait(timeout=5)
        with pytest.raises(SessionBusy):
            with store.turn(created.session_id, actor=purchaser):
                pytest.fail("busy session context'ine girilmemeliydi")
        release.set()
        future.result(timeout=5)

    loaded = store.load(created.session_id, actor=purchaser)
    assert loaded is not None and loaded.turn_count == 1


def test_session_is_not_readable_by_other_user_in_same_tenant(tmp_path, purchaser):
    """Ayni tenant'taki baska kullanici bir oturumu goremez veya silemez."""
    store = SQLiteSessionStore(get_state_db(tmp_path / "s.sqlite3"), ttl_hours=1)
    record = store.create(actor=purchaser)
    other = ActorContext(subject="baska@firma.test", tenant=purchaser.tenant, roles=("VIEWER",))

    assert store.load(record.session_id, actor=other) is None
    assert store.delete(record.session_id, actor=other) is False
    assert store.list(actor=other) == []
    # Sahibi icin hala erisilebilir.
    assert store.load(record.session_id, actor=purchaser) is not None


def test_session_id_of_other_user_cannot_be_hijacked(tmp_path, purchaser):
    """Baskasinin ID'siyle get_or_create yeni kayit acamaz."""
    from robotics_agent.core import SessionOwnershipError

    store = SQLiteSessionStore(get_state_db(tmp_path / "s.sqlite3"), ttl_hours=1)
    record = store.create(actor=purchaser)
    attacker = ActorContext(
        subject="saldirgan@firma.test", tenant=purchaser.tenant, roles=("VIEWER",)
    )
    with pytest.raises(SessionOwnershipError):
        store.get_or_create(record.session_id, actor=attacker)


# --- Paylasilan rate limit --------------------------------------------------
def test_shared_rate_limit_is_not_divided_per_worker(tmp_path):
    """Toplam rate limit tum worker'lar arasinda paylasilan sayimla uygulanir."""
    from robotics_agent.channels.auth import SharedRateLimiter

    db = get_state_db(tmp_path / "s.sqlite3")
    limiters = [SharedRateLimiter(5, db) for _ in range(3)]

    allowed = 0
    for index in range(15):
        ok, _ = limiters[index % 3].check("tenant:user")
        allowed += 1 if ok else 0
    assert allowed == 5
