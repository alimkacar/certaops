"""Audit zincir checkpoint'inin harici hedefe aktarilmasi.

Rehber Madde 10: "Audit loglarinin uygulama yoneticileri tarafindan
degistirilememesini saglayin." Yerel defter tamper-evident'tir ama immutable
degildir; harici bir append-only hedefe yazilan zincir ozeti, defterin
tamaminin yeniden uretilmesini tespit edilebilir kilar.

Test edilen sozlesme:
  1. Esik dolunca checkpoint otomatik disa aktarilir.
  2. Checkpoint zincir basini (seq + hash) tasir.
  3. Disa aktarma hatasi audit yazmasini GERI ALMAZ.
  4. Hedef yapilandirilmamissa durum acikca "configured: false" bildirilir.
  5. Defter yeniden yazilirsa checkpoint ile uyusmazlik ortaya cikar.
"""

from __future__ import annotations

import json

import pytest

from robotics_agent.contracts import ActorContext, ExecutionContext
from robotics_agent.core import FileCheckpointExporter
from robotics_agent.core.audit import AuditLedger
from robotics_agent.core.store import get_state_db


@pytest.fixture
def actor() -> ActorContext:
    return ActorContext(subject="denetci@firma.test", tenant="100", auth_method="test")


@pytest.fixture
def execution(actor) -> ExecutionContext:
    return ExecutionContext(actor=actor, channel="test", system_alias="S4-TEST")


def make_ledger(tmp_path, *, exporter=None, every: int = 0) -> AuditLedger:
    return AuditLedger(
        get_state_db(tmp_path / "state.sqlite3"),
        checkpoint_exporter=exporter,
        checkpoint_every=every,
    )


def test_checkpoint_exported_at_interval(tmp_path, execution):
    target = tmp_path / "worm" / "checkpoints.jsonl"
    ledger = make_ledger(tmp_path, exporter=FileCheckpointExporter(target), every=3)

    for index in range(6):
        ledger.append(f"tool.completed.{index}", execution=execution, outcome="ok")

    lines = [json.loads(line) for line in target.read_text().splitlines() if line.strip()]
    assert len(lines) == 2, "3 kayitta bir, 6 kayitta iki checkpoint bekleniyor"
    assert [row["seq"] for row in lines] == [3, 6]
    assert all(len(row["head_hash"]) == 64 for row in lines)


def test_checkpoint_matches_ledger_head(tmp_path, execution):
    target = tmp_path / "checkpoints.jsonl"
    ledger = make_ledger(tmp_path, exporter=FileCheckpointExporter(target), every=2)
    ledger.append("a", execution=execution, outcome="ok")
    ledger.append("b", execution=execution, outcome="ok")

    exported = json.loads(target.read_text().splitlines()[-1])
    verified = ledger.verify()

    assert exported["head_hash"] == verified["head"]
    assert exported["seq"] == verified["entries"]


def test_manual_export_returns_payload(tmp_path, execution):
    target = tmp_path / "checkpoints.jsonl"
    ledger = make_ledger(tmp_path, exporter=FileCheckpointExporter(target))
    ledger.append("a", execution=execution, outcome="ok")

    payload = ledger.export_checkpoint()

    assert payload is not None
    assert payload["seq"] == 1
    assert target.exists()


def test_export_without_exporter_returns_none(tmp_path, execution):
    ledger = make_ledger(tmp_path)
    ledger.append("a", execution=execution, outcome="ok")
    assert ledger.export_checkpoint() is None


def test_export_failure_does_not_lose_audit_entry(tmp_path, execution):
    class BrokenExporter:
        def export(self, checkpoint):  # noqa: ANN001, ARG002
            raise OSError("harici hedefe ulasilamiyor")

    ledger = make_ledger(tmp_path, exporter=BrokenExporter(), every=1)
    entry = ledger.append("tool.completed", execution=execution, outcome="ok")

    assert entry.seq == 1, "export hatasi audit yazmasini geri almamali"
    assert ledger.verify()["valid"] is True


def test_status_reports_missing_external_target(tmp_path, execution):
    ledger = make_ledger(tmp_path)
    ledger.append("a", execution=execution, outcome="ok")

    status = ledger.checkpoint_status()

    assert status["configured"] is False
    assert "tespit edilemez" in status["note"], "sinir gizlenmemeli"


def test_status_reports_lag_since_last_export(tmp_path, execution):
    target = tmp_path / "checkpoints.jsonl"
    ledger = make_ledger(tmp_path, exporter=FileCheckpointExporter(target), every=2)
    for index in range(5):
        ledger.append(f"e{index}", execution=execution, outcome="ok")

    status = ledger.checkpoint_status()

    assert status["configured"] is True
    assert status["last_exported_seq"] == 4
    assert status["entries_since_export"] == 1


def test_rewritten_ledger_diverges_from_checkpoint(tmp_path, execution):
    """Zincirin bastan uretilmesi checkpoint ile karsilastirilinca gorunur olur."""
    target = tmp_path / "checkpoints.jsonl"
    ledger = make_ledger(tmp_path, exporter=FileCheckpointExporter(target), every=2)
    ledger.append("gercek-1", execution=execution, outcome="ok")
    ledger.append("gercek-2", execution=execution, outcome="ok")
    trusted = json.loads(target.read_text().splitlines()[-1])

    # Saldirgan defteri siler ve farkli iceriklerle yeniden uretir.
    db = get_state_db(tmp_path / "state.sqlite3")
    with db.write() as conn:
        conn.execute("DELETE FROM audit_entries")
    rebuilt = make_ledger(tmp_path)
    rebuilt.append("sahte-1", execution=execution, outcome="ok")
    rebuilt.append("sahte-2", execution=execution, outcome="ok")

    # Yeniden uretilen zincir kendi icinde tutarlidir...
    assert rebuilt.verify()["valid"] is True
    # ...ama harici checkpoint'teki hash ile uyusmaz.
    assert rebuilt.checkpoint()["head_hash"] != trusted["head_hash"]


def test_settings_expose_checkpoint_configuration(settings):
    object.__setattr__(settings.state, "audit_checkpoint_path", "/tmp/cp.jsonl")
    assert settings.state.checkpoint_enabled is True
    object.__setattr__(settings.state, "audit_checkpoint_path", "")
    assert settings.state.checkpoint_enabled is False
