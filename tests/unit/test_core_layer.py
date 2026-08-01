"""Cekirdek katman testleri: idempotency, audit, evidence, butce, router.

Idempotency state machine, approval payload hash, korunan alan semasi ve token
butcesi davranisi bagimsiz olarak dogrulanir.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from robotics_agent.contracts import (
    PROTECTED_KEYS,
    ActorContext,
    Evidence,
    EvidenceAccessDenied,
    EvidenceStore,
    RiskTier,
    ToolResult,
    enforce_result_budget,
    estimate_tokens,
    page_limit,
    project,
    resolve_detail,
)
from robotics_agent.core import (
    AGENT_SPECS,
    ApprovalStore,
    AuditLedger,
    BeginOutcome,
    IdempotencyConflict,
    IdempotencyStore,
    MemorySessionStore,
    SQLiteSessionStore,
    agent_catalogue,
    build_idempotency_key,
    domains_for_packs,
    get_state_db,
    normalize_pack_keys,
    payload_hash,
    plan_agents,
    redact,
    route,
    schema_token_report,
    summarize_intent,
)


# --- Idempotency -----------------------------------------------------------
@pytest.fixture
def idem(tmp_path) -> IdempotencyStore:
    return IdempotencyStore(get_state_db(tmp_path / "state.sqlite3"))


def test_first_begin_reserves(idem):
    result = idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    assert result.outcome is BeginOutcome.NEW
    assert result.record.status == "reserved"
    assert result.may_execute
    assert result.record.lease_owner == "e1"


def test_same_execution_retry_is_allowed(idem):
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    result = idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    assert result.outcome is BeginOutcome.RETRY
    assert result.may_execute
    assert result.record.attempts == 2


def test_concurrent_execution_is_blocked_by_lease(idem):
    """Ikinci worker aktif lease varken ayni islemi yeniden yazmamalidir."""
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    result = idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e2")
    assert result.outcome is BeginOutcome.IN_PROGRESS
    assert not result.may_execute
    assert result.record.lease_owner == "e1"


def test_expired_lease_is_taken_over_as_recovered(tmp_path):
    """Lease suresi gectiyse sahiplik devralinir ama once mutabakat gerekir."""
    store = IdempotencyStore(get_state_db(tmp_path / "s.sqlite3"), lease_seconds=30)
    store.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with store._db.write() as conn:  # noqa: SLF001 - testte durum kurgusu
        conn.execute(
            "UPDATE idempotency SET lease_expires_at = ? WHERE key = 'k1'", (past,)
        )
    result = store.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e2")
    assert result.outcome is BeginOutcome.RECOVERED
    assert result.may_execute is False or result.record.needs_reconciliation


def test_same_key_with_different_payload_conflicts(idem):
    """Ayni anahtarla farkli is icerigi yazmak sessizce kabul edilmemeli."""
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h1", execution_id="e1")
    with pytest.raises(IdempotencyConflict):
        idem.begin("k1", tenant="100", tool="t", payload_sha256="h2", execution_id="e2")


def test_completed_record_carries_business_object(idem):
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    idem.complete("k1", tenant="100", business_object_id="10000431", result={"ok": True})
    record = idem.get("k1", tenant="100")
    assert record.is_completed and record.business_object_id == "10000431"
    assert record.needs_reconciliation is False


def test_unknown_status_needs_reconciliation(idem):
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    idem.mark_unknown("k1", tenant="100", reason="timeout")
    record = idem.get("k1", tenant="100")
    assert record.status == "unknown" and record.needs_reconciliation
    assert idem.pending(tenant="100")


def test_tenant_isolation(idem):
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    assert idem.get("k1", tenant="200") is None
    # Ayni anahtar baska tenant'ta cakismaz.
    result = idem.begin("k1", tenant="200", tool="t", payload_sha256="x", execution_id="e2")
    assert result.outcome is BeginOutcome.NEW


def test_completed_begin_reports_completed(idem):
    idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e1")
    idem.complete("k1", tenant="100", business_object_id="B1", result={"ok": True})
    result = idem.begin("k1", tenant="100", tool="t", payload_sha256="h", execution_id="e2")
    assert result.outcome is BeginOutcome.COMPLETED
    assert not result.may_execute
    assert result.record.business_object_id == "B1"


def test_build_idempotency_key_is_deterministic():
    assert build_idempotency_key("R-2026-021", "reduktor eksigi", "pr", "v1") == (
        "R-2026-021:reduktor_eksigi:pr:v1"
    )
    with pytest.raises(ValueError):
        build_idempotency_key("", "  ")


# --- Onay hash'i -----------------------------------------------------------
def test_payload_hash_is_key_order_independent():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


def test_payload_hash_changes_with_value():
    assert payload_hash({"qty": 1}) != payload_hash({"qty": 2})


def test_approval_store_rejects_issue_without_approver(tmp_path):
    from robotics_agent.core import ApprovalError

    store = ApprovalStore(get_state_db(tmp_path / "s.sqlite3"))
    with pytest.raises(ApprovalError):
        store.issue(tool="t", payload={}, tenant="100", approvers=[])


def test_approval_can_only_be_consumed_once(tmp_path, approver):
    from robotics_agent.core import ApprovalError

    store = ApprovalStore(get_state_db(tmp_path / "s.sqlite3"))
    record = store.issue(tool="t", payload={"a": 1}, tenant="100", approvers=[approver])
    store.consume(record.approval_id, execution_id="e1")
    with pytest.raises(ApprovalError):
        store.consume(record.approval_id, execution_id="e2")


# --- Audit defteri ---------------------------------------------------------
@pytest.fixture
def ledger(tmp_path) -> AuditLedger:
    return AuditLedger(get_state_db(tmp_path / "audit.sqlite3"))


def test_audit_chain_is_verifiable(ledger, purchaser):
    for index in range(5):
        ledger.append("tool.completed", actor=purchaser, tool=f"t{index}", outcome="ok")
    assert ledger.verify()["valid"] is True
    assert ledger.verify()["entries"] == 5


def test_tampered_audit_entry_is_detected(tmp_path, purchaser):
    db = get_state_db(tmp_path / "audit.sqlite3")
    ledger = AuditLedger(db)
    ledger.append("tool.completed", actor=purchaser, tool="t1", outcome="ok")
    ledger.append("tool.completed", actor=purchaser, tool="t2", outcome="ok")

    # Kaydi dogrudan veritabaninda degistir.
    with db.write() as conn:
        conn.execute("UPDATE audit_entries SET outcome = 'denied' WHERE seq = 1")
        row = conn.execute("SELECT body_json FROM audit_entries WHERE seq = 1").fetchone()
        body = json.loads(row["body_json"])
        body["outcome"] = "denied"
        conn.execute(
            "UPDATE audit_entries SET body_json = ? WHERE seq = 1",
            (json.dumps(body, ensure_ascii=False),),
        )

    result = AuditLedger(db).verify()
    assert result["valid"] is False
    assert result["broken_at"] == 1


def test_audit_redacts_secrets(ledger, purchaser):
    ledger.append(
        "tool.completed",
        actor=purchaser,
        tool="t",
        outcome="ok",
        detail={"sap_password": "hunter2", "api_key": "abc", "material_id": "ROB-1"},
    )
    entry = ledger.recent(limit=1)[0]
    detail = entry["detail"]
    assert detail["sap_password"] == "[REDACTED]"
    assert detail["api_key"] == "[REDACTED]"
    assert detail["material_id"] == "ROB-1"  # is verisi korunur


def test_audit_records_model_and_prompt_version(ledger, purchaser):
    ledger.append(
        "tool.completed", actor=purchaser, tool="t", outcome="ok",
        model="claude-sonnet-5", prompt_version="v1-abc123",
    )
    entry = ledger.recent(limit=1)[0]
    assert entry["model"] == "claude-sonnet-5"
    assert entry["prompt_version"] == "v1-abc123"


def test_audit_checkpoint_tracks_head(ledger, purchaser):
    ledger.append("a", actor=purchaser, outcome="ok")
    ledger.append("b", actor=purchaser, outcome="ok")
    checkpoint = ledger.checkpoint()
    assert checkpoint["seq"] == 2
    assert checkpoint["head_hash"] == ledger.verify()["head"]


def test_audit_partial_verify_is_cheap(ledger, purchaser):
    for _ in range(20):
        ledger.append("x", actor=purchaser, outcome="ok")
    partial = ledger.verify(limit=5)
    assert partial["valid"] is True
    assert "son 5" in partial["scope"]


def test_redact_truncates_large_values():
    result = redact({"text": "x" * 2000})
    assert result["text"].startswith("[TRUNCATED chars=2000")


def test_audit_second_writer_does_not_break_chain(tmp_path, purchaser):
    """Iki ayri defter ornegi ayni veritabanina yazsa da zincir gecerli kalir."""
    db = get_state_db(tmp_path / "audit.sqlite3")
    first = AuditLedger(db)
    second = AuditLedger(db)
    first.append("a", actor=purchaser, outcome="ok")
    second.append("b", actor=purchaser, outcome="ok")
    first.append("c", actor=purchaser, outcome="ok")
    assert AuditLedger(db).verify()["valid"] is True


# --- Evidence store --------------------------------------------------------
def test_evidence_roundtrip(purchaser):
    store = EvidenceStore(ttl_minutes=5)
    evidence = Evidence(source_system="S4", source_api="test", record_count=3)
    handle = store.put({"rows": [1, 2, 3]}, actor=purchaser, tool="t", evidence=evidence)
    fetched = store.get(handle, actor=purchaser)
    assert fetched["payload"]["rows"] == [1, 2, 3]
    assert fetched["evidence"]["source_api"] == "test"


def test_evidence_is_tenant_bound(purchaser):
    store = EvidenceStore()
    handle = store.put({"x": 1}, actor=purchaser, tool="t", evidence=Evidence("S4", "t"))
    other = ActorContext(subject="baska", tenant="999", roles=("VIEWER",))
    with pytest.raises(EvidenceAccessDenied):
        store.get(handle, actor=other)


def test_evidence_handle_is_unguessable(purchaser):
    store = EvidenceStore()
    handles = {
        store.put({"i": i}, actor=purchaser, tool="t", evidence=Evidence("S4", "t"))
        for i in range(20)
    }
    assert len(handles) == 20
    assert all(len(h) > 20 for h in handles)


def test_missing_evidence_raises_keyerror(purchaser):
    with pytest.raises(KeyError):
        EvidenceStore().get("ev_yok", actor=purchaser)


def test_evidence_marks_estimated_fields():
    evidence = Evidence("S4", "supplier_score", estimated_fields=("overall_score",))
    payload = evidence.to_dict()
    assert payload["estimated"] is True
    assert payload["estimated_fields"] == ["overall_score"]


# --- Token butcesi ---------------------------------------------------------
def test_small_result_is_untouched():
    payload = {"a": 1, "rows": [1, 2, 3]}
    outcome = enforce_result_budget(payload, max_tokens=1000)
    assert not outcome.trimmed and outcome.payload == payload


def test_large_result_is_trimmed_and_marked():
    payload = {"rows": [{"material_id": f"M{i}", "text": "x" * 80} for i in range(200)]}
    outcome = enforce_result_budget(payload, max_tokens=300, evidence_id="ev_1")
    assert outcome.trimmed
    assert outcome.final_tokens < outcome.original_tokens
    assert outcome.payload["_meta"]["truncated"] is True
    assert outcome.payload["_meta"]["evidence_id"] == "ev_1"
    assert len(outcome.payload["rows"]) < 200


def test_protected_keys_survive_trimming():
    payload = {
        "requisition_id": "10000431",
        "total_value": 43600.0,
        "currency": "EUR",
        "warnings": ["onay gerekli"],
        "rows": [{"x": "y" * 100} for _ in range(300)],
    }
    outcome = enforce_result_budget(payload, max_tokens=200)
    assert outcome.trimmed
    for key in ("requisition_id", "total_value", "currency", "warnings"):
        assert key in outcome.payload, key
    assert outcome.payload["total_value"] == 43600.0


def test_protected_key_list_covers_critical_fields():
    for key in ("requisition_id", "approval_id", "etag", "idempotency_key", "policy_decision"):
        assert key in PROTECTED_KEYS


def test_projection_keeps_protected_fields():
    rows = [{"material_id": "M1", "noise": "z", "total_value": 5.0}]
    projected = project(rows, ["material_id"])
    assert "noise" not in projected[0]
    assert projected[0]["total_value"] == 5.0


def test_detail_and_page_limits():
    assert resolve_detail("FULL") == "full"
    assert resolve_detail("bilinmeyen") == "standard"
    assert page_limit("summary", None, default=20) < page_limit("standard", None, default=20)
    assert page_limit("full", None, default=20) > page_limit("standard", None, default=20)
    assert page_limit("standard", 500) == 200  # ust sinir


def test_tool_result_payload_shape():
    result = ToolResult(data={"a": 1}, detail="summary", total_count=10, returned_count=3)
    result.warn("dikkat").note("bilgi")
    payload = result.to_payload()
    assert payload["warnings"] == ["dikkat"]
    assert payload["_meta"]["total_count"] == 10
    assert payload["_meta"]["detail"] == "summary"


def test_estimate_tokens_is_monotonic():
    assert estimate_tokens("kisa") < estimate_tokens("kisa" * 100)


# --- Router ----------------------------------------------------------------
def test_router_selects_procurement_for_supplier_question(purchaser):
    decision = route("HD-GEAR icin tedarikci fiyatlarini TCO bazinda karsilastir", purchaser)
    assert "procurement_read" in decision.packs
    assert decision.packs[0] == "bootstrap"
    assert not decision.fallback


def test_orchestrator_selects_finance_agent_for_wbs_question(purchaser):
    plan = plan_agents("R-2026-014 WBS proje maliyeti ve EAC durumunu getir", purchaser)
    assert plan.agents == ("finance",)


def test_router_expands_write_pack_dependencies(purchaser):
    decision = route("Satinalma talebi olustur", purchaser)
    assert "procurement_write" in decision.packs
    # Yazma pack'i ATP/tedarikci okumasini da acar: taslak fiyatlandirma ve
    # termin dogrulamasi bunlara dayanir.
    assert "procurement_read" in decision.packs
    # Malzeme arama ayri ana-veri agent'inda kalir.
    assert "master_data" not in decision.packs


def test_router_never_opens_write_pack_for_unauthorized_actor(viewer):
    decision = route("Satinalma talebi olustur ve onayla", viewer)
    assert "procurement_write" not in decision.packs


def test_router_falls_back_without_keyword_match(purchaser):
    decision = route("merhaba", purchaser)
    assert decision.fallback
    assert "procurement_write" not in decision.packs


def test_normalize_pack_keys_drops_unknown():
    assert normalize_pack_keys(["master_data", "uydurma_pack"]) == ("master_data",)


def test_agent_catalogue_has_isolated_sap_domains():
    catalogue = agent_catalogue()
    assert {row["agent"] for row in catalogue} == set(AGENT_SPECS)
    assert set(AGENT_SPECS) == {
        "platform", "master_data", "supply_chain", "procurement", "finance"
    }
    assert all("engineering" not in spec.packs for spec in AGENT_SPECS.values())


def test_procurement_plan_deduplicates_supply_chain_dependency(purchaser):
    plan = plan_agents("ATP kontrol et ve satinalma talebi olustur", purchaser)
    assert plan.agents == ("procurement",)


def test_unknown_intent_goes_to_platform_agent(purchaser):
    assert plan_agents("merhaba", purchaser).agents == ("platform",)


def test_domains_for_packs_includes_dependencies():
    domains = domains_for_packs(("procurement_write",))
    assert {"procurement_write", "procurement_read", "planning"} <= domains
    assert "master_data" not in domains


def test_write_pack_stays_within_schema_budget(purchaser, settings):
    """Bir turdaki aktif tool semalari 3.000 token butcesini asmamalidir."""
    from robotics_agent.tools import anthropic_tool_definitions, load_all_tools, visible_tool_names

    load_all_tools()
    for packs in (
        ("bootstrap",),
        ("bootstrap", "procurement_read"),
        ("bootstrap", "procurement_write", "procurement_read"),
        ("bootstrap", "project_finance", "reporting"),
    ):
        names = visible_tool_names(domains_for_packs(packs), purchaser)
        report = schema_token_report(
            anthropic_tool_definitions(names), budget=settings.budget.schema_tokens_per_turn
        )
        assert report["within_budget"], f"{packs} -> {report['schema_tokens']} token"


def test_bootstrap_pack_stays_small():
    """Her turda yuklenen bootstrap paketi 4-6 tool sinirinda kalmalidir."""
    from robotics_agent.contracts import ActorContext
    from robotics_agent.tools import load_all_tools, visible_tool_names

    load_all_tools()
    admin = ActorContext(subject="a", tenant="100", roles=("PLATFORM_ADMIN", "AUDITOR"))
    names = visible_tool_names(domains_for_packs(("bootstrap",)), admin)
    assert 3 <= len(names) <= 6, names


def test_schema_token_report_flags_budget_breach():
    definitions = [{"name": f"t{i}", "description": "x" * 4000} for i in range(5)]
    report = schema_token_report(definitions, budget=1000)
    assert report["within_budget"] is False
    assert report["largest"][0]["tokens"] > 0


def test_summarize_intent_keeps_keywords_not_full_text():
    """Telemetriye prompt yazilmaz; yalniz anahtar kelime izi kalir."""
    message = "HD-GEAR-CSF25-100 icin 24 adet tedarikci karsilastirmasi yap, acele!"
    summary = summarize_intent(message)
    assert "tedarikci" in summary
    # Tam metin degil: noktalama, kisa kelimeler ve orijinal bicim korunmaz.
    assert message not in summary
    assert "!" not in summary and "-" not in summary
    assert len(summary) < len(message)


def test_summarize_intent_is_word_capped():
    summary = summarize_intent(" ".join(f"kelime{i}" for i in range(40)), max_words=5)
    assert len(summary.split()) == 5


# --- Oturum deposu ---------------------------------------------------------
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_session_roundtrip(backend, tmp_path, purchaser):
    store = (
        MemorySessionStore(ttl_hours=1)
        if backend == "memory"
        else SQLiteSessionStore(get_state_db(tmp_path / "s.sqlite3"), ttl_hours=1)
    )
    record, created = store.get_or_create(None, actor=purchaser)
    assert created
    record.messages = [{"role": "user", "content": "merhaba"}]
    record.turn_count = 1
    store.save(record)

    loaded, created_again = store.get_or_create(record.session_id, actor=purchaser)
    assert not created_again
    assert loaded.messages[0]["content"] == "merhaba"
    assert loaded.turn_count == 1


def test_session_is_not_visible_to_other_tenant(tmp_path, purchaser):
    store = SQLiteSessionStore(get_state_db(tmp_path / "s.sqlite3"), ttl_hours=1)
    record = store.create(actor=purchaser)
    other = ActorContext(subject="x", tenant="999", roles=("VIEWER",))
    assert store.load(record.session_id, actor=other) is None
    assert store.delete(record.session_id, actor=other) is False


def test_sqlite_session_survives_new_store_instance(tmp_path, purchaser):
    """Restart benzeri senaryo: yeni store ornegi ayni oturumu gormeli."""
    db_path = tmp_path / "s.sqlite3"
    first = SQLiteSessionStore(get_state_db(db_path), ttl_hours=1)
    record = first.create(actor=purchaser)
    record.messages = [{"role": "user", "content": "kalici"}]
    first.save(record)

    second = SQLiteSessionStore(get_state_db(db_path), ttl_hours=1)
    loaded = second.load(record.session_id, actor=purchaser)
    assert loaded is not None and loaded.messages[0]["content"] == "kalici"


# --- Risk seviyeleri -------------------------------------------------------
def test_risk_tier_semantics():
    assert not RiskTier.R0.is_mutating
    assert not RiskTier.R2.requires_approval
    assert RiskTier.R3.requires_approval
    assert RiskTier.R4.requires_dual_control
    assert not RiskTier.R3.requires_dual_control
    assert RiskTier.R3.level == 3
