"""Saklama politikasi ve periyodik veri temizleme kabul testleri.

Kontrol edilen davranislar:
  - Suresi dolmus oturum/evidence gercekten silinir.
  - Yasal saklama kapsamindaki audit kaydina dokunulmaz.
  - Purge kucuk batch'lerle ilerler ve idempotenttir.
  - Purge raporu **veri icerigi tasimaz**.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from robotics_agent.core import get_state_db
from robotics_agent.privacy import RETENTION_POLICY, RetentionRule, RetentionSweeper


@pytest.fixture
def db(tmp_path):
    return get_state_db(tmp_path / "retention.sqlite3")


def _iso(delta_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


def _seed_session(db, session_id: str, *, age_minutes: int) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, tenant, subject, created_at, last_seen) "
            "VALUES (?,?,?,?,?)",
            (session_id, "100", "a@firma.test", _iso(-age_minutes), _iso(-age_minutes)),
        )


def _seed_evidence(db, evidence_id: str, *, expires_in_minutes: int) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO evidence (evidence_id, tenant, subject, tool, payload_json, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (evidence_id, "100", "a@firma.test", "t", "{}", _iso(-60), _iso(expires_in_minutes)),
        )


def _count(db, table: str) -> int:
    return db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]  # noqa: S608


# --- Politika tablosu ------------------------------------------------------
def test_every_rule_declares_a_retention_period():
    for rule in RETENTION_POLICY:
        assert rule.name and rule.table and rule.timestamp_column
        # Yasal saklama disindaki her kural aktif olmali.
        assert rule.legal_hold or rule.active, rule.name


def test_audit_is_under_legal_hold():
    audit = next(r for r in RETENTION_POLICY if r.name == "audit")
    assert audit.legal_hold and not audit.active


# --- Silme davranisi -------------------------------------------------------
def test_expired_sessions_are_purged(db):
    _seed_session(db, "eski", age_minutes=48 * 60)
    _seed_session(db, "taze", age_minutes=10)

    report = RetentionSweeper(db).sweep()
    assert report.deleted.get("session") == 1
    assert _count(db, "sessions") == 1


def test_expired_evidence_is_purged_by_absolute_expiry(db):
    _seed_evidence(db, "ev_gecmis", expires_in_minutes=-5)
    _seed_evidence(db, "ev_gecerli", expires_in_minutes=30)

    report = RetentionSweeper(db).sweep()
    assert report.deleted.get("evidence") == 1
    assert _count(db, "evidence") == 1


def test_audit_entries_are_never_purged(db):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO audit_entries (entry_hash, prev_hash, recorded_at, event, body_json) "
            "VALUES (?,?,?,?,?)",
            ("h1", "", _iso(-365 * 24 * 60), "tool.completed", "{}"),
        )
    RetentionSweeper(db).sweep()
    assert _count(db, "audit_entries") == 1


def test_sweep_is_idempotent(db):
    _seed_session(db, "eski", age_minutes=48 * 60)
    sweeper = RetentionSweeper(db)
    first = sweeper.sweep()
    second = sweeper.sweep()
    assert first.total_deleted == 1
    assert second.total_deleted == 0


def test_sweep_uses_small_batches(db):
    for index in range(12):
        _seed_session(db, f"eski-{index}", age_minutes=48 * 60)
    report = RetentionSweeper(db, batch_size=50, max_batches=5).sweep()
    assert report.deleted["session"] == 12
    assert report.batches >= 1
    assert _count(db, "sessions") == 0


def test_batch_limit_is_respected(db):
    """Tek sweep tum tabloyu kilitlemez; kalanlar sonraki turda silinir."""
    for index in range(300):
        _seed_session(db, f"eski-{index}", age_minutes=48 * 60)
    sweeper = RetentionSweeper(db, batch_size=50, max_batches=2)
    first = sweeper.sweep()
    assert first.deleted["session"] == 100
    assert _count(db, "sessions") == 200


# --- Rapor gizliligi -------------------------------------------------------
def test_purge_report_contains_no_data_content(db):
    _seed_evidence(db, "ev_gecmis", expires_in_minutes=-5)
    payload = str(RetentionSweeper(db).sweep().to_dict())
    assert "ev_gecmis" not in payload
    assert "a@firma.test" not in payload
    assert "deleted" in payload


def test_policy_report_is_human_readable(db):
    report = RetentionSweeper(db).policy_report()
    names = {row["data"] for row in report}
    assert {"session", "evidence", "audit"} <= names
    assert all("max_age_minutes" in row for row in report)


# --- Hata dayanikliligi ----------------------------------------------------
def test_missing_table_does_not_break_the_sweep(db):
    rule = RetentionRule(
        name="olmayan", max_age_minutes=10, table="olmayan_tablo", timestamp_column="ts"
    )
    report = RetentionSweeper(db, rules=(rule,)).sweep()
    assert report.total_deleted == 0 and not report.errors


def test_retention_rule_rejects_unsafe_sql_identifiers():
    with pytest.raises(ValueError, match="Gecersiz SQL tanimlayicisi"):
        RetentionRule(
            name="unsafe",
            max_age_minutes=10,
            table="sessions; DROP TABLE sessions",
            timestamp_column="last_seen",
        )


def test_audit_hook_receives_the_report(db):
    _seed_session(db, "eski", age_minutes=48 * 60)
    seen: list = []
    RetentionSweeper(db, audit_hook=seen.append).sweep()
    assert len(seen) == 1 and seen[0].total_deleted == 1
