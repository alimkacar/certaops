"""Policy gate yetkilendirme ve guvenli yazma testleri.

Kontrol edilen davranislar:
  - deny-by-default: kapsam yoksa handler'a ulasilmaz
  - ABAC: argumandaki tesis actor'un yetki alanini genisletemez
  - R3 icin onay zorunlulugu; payload degisirse onay gecersiz
  - onay tek kullanimlik (replay engeli)
  - SoD: yurutucu tek onaylayanla ayni kisi olamaz
  - yazma penceresi kapaliyken R3 reddedilir
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from robotics_agent.contracts import (
    SCOPE_PR_APPROVE,
    SCOPE_PR_WRITE,
    ActorContext,
    ExecutionContext,
    RiskTier,
)
from robotics_agent.core import (
    OBLIGATION_IDEMPOTENCY,
    OBLIGATION_READ_AFTER_WRITE,
    OBLIGATION_VERIFY_VALUE,
    ApprovalStore,
    PolicyDecisionPoint,
    PolicyOutcome,
    approval_payload_for,
    get_state_db,
)
from robotics_agent.tools import REGISTRY, load_all_tools


class _Spec:
    """Test icin minimal tool sozlesmesi."""

    def __init__(
        self,
        *,
        name="sap_pr_submit",
        risk_tier=RiskTier.R3,
        required_scopes=(SCOPE_PR_WRITE,),
        approval_policy="always",
        idempotent=True,
    ) -> None:
        self.name = name
        self.risk_tier = risk_tier
        self.required_scopes = required_scopes
        self.approval_policy = approval_policy
        self.approve_scope = SCOPE_PR_APPROVE
        self.idempotent = idempotent


@pytest.fixture
def store(tmp_path) -> ApprovalStore:
    return ApprovalStore(get_state_db(tmp_path / "state.sqlite3"), default_ttl_minutes=30)


@pytest.fixture
def pdp(store) -> PolicyDecisionPoint:
    return PolicyDecisionPoint(approvals=store, forced_dry_run=False, approval_threshold=25_000.0)


def _execution(actor: ActorContext) -> ExecutionContext:
    return ExecutionContext(actor=actor, system_alias="S4-TEST", dry_run=False)


# --- Deny by default -------------------------------------------------------
def test_anonymous_actor_is_denied(pdp):
    decision = pdp.evaluate(_Spec(), {}, _execution(ActorContext.anonymous()))
    assert decision.outcome is PolicyOutcome.DENY
    assert decision.denial_code == "AUTH_REQUIRED"


def test_missing_scope_is_denied(pdp, engineer):
    # ENGINEER'in sap.pr.write kapsami yok.
    decision = pdp.evaluate(_Spec(), {}, _execution(engineer))
    assert decision.denial_code == "MISSING_SCOPE"
    assert SCOPE_PR_WRITE in decision.missing_scopes


def test_read_tool_allowed_for_viewer(pdp, viewer):
    spec = _Spec(
        name="sap_search_materials",
        risk_tier=RiskTier.R0,
        required_scopes=("sap.read",),
        approval_policy="none",
        idempotent=False,
    )
    decision = pdp.evaluate(spec, {}, _execution(viewer))
    assert decision.allowed


# --- ABAC ------------------------------------------------------------------
def test_plant_outside_actor_scope_is_denied(pdp, viewer):
    spec = _Spec(
        name="sap_stock_overview",
        risk_tier=RiskTier.R0,
        required_scopes=("sap.read",),
        approval_policy="none",
        idempotent=False,
    )
    # viewer yalniz 2200 tesisine yetkili.
    decision = pdp.evaluate(spec, {"plant": "1100"}, _execution(viewer))
    assert decision.denial_code == "ORG_SCOPE"
    assert "1100" in " ".join(decision.reasons)


def test_plant_inside_actor_scope_is_allowed(pdp, viewer):
    spec = _Spec(
        name="sap_stock_overview",
        risk_tier=RiskTier.R0,
        required_scopes=("sap.read",),
        approval_policy="none",
        idempotent=False,
    )
    assert pdp.evaluate(spec, {"plant": "2200"}, _execution(viewer)).allowed


# --- Onay ------------------------------------------------------------------
def test_r3_without_approval_is_denied(pdp, purchaser):
    decision = pdp.evaluate(_Spec(), {"items": []}, _execution(purchaser))
    assert decision.denial_code == "APPROVAL_REQUIRED"


def test_valid_approval_allows_and_sets_obligations(pdp, store, purchaser, approver):
    arguments = {"items": [{"material_id": "X", "quantity": 1}]}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
        requested_by=purchaser.subject,
    )
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.allowed
    assert decision.requires(OBLIGATION_READ_AFTER_WRITE)
    assert decision.requires(OBLIGATION_IDEMPOTENCY)


def test_approval_is_bound_to_payload(pdp, store, purchaser, approver):
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for({"items": [{"material_id": "X", "quantity": 1}]}),
        tenant="100",
        approvers=[approver],
    )
    # Miktar degistirildi: ayni onay kullanilamaz.
    decision = pdp.evaluate(
        _Spec(),
        {"items": [{"material_id": "X", "quantity": 99}], "approval_id": record.approval_id},
        _execution(purchaser),
    )
    assert decision.denial_code == "APPROVAL_INVALID"
    assert "payload" in " ".join(decision.reasons).lower()


def test_idempotency_key_is_not_part_of_approval_hash(pdp, store, purchaser, approver):
    """Teknik retry anahtari is icerigi degildir; onayi gecersiz kilmamali."""
    arguments = {"items": [{"material_id": "X", "quantity": 1}]}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
    )
    decision = pdp.evaluate(
        _Spec(),
        {**arguments, "approval_id": record.approval_id, "idempotency_key": "a:b:c:v1"},
        _execution(purchaser),
    )
    assert decision.allowed


def test_consumed_approval_cannot_be_replayed(pdp, store, purchaser, approver):
    arguments = {"items": [{"material_id": "X", "quantity": 1}]}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
    )
    store.consume(record.approval_id, execution_id="exec-1")
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    # Tuketilmis onay ayri bir koda ayrilir: cagirici basarili bir yazmayi
    # tekrar deniyor olabilir; dogru yonlendirme mutabakattir, yeni onay degil.
    assert decision.denial_code == "APPROVAL_CONSUMED"
    assert "kullanildi" in " ".join(decision.reasons)
    assert "sap_reconcile_execution" in decision.as_error()["remediation"]


def test_expired_approval_is_rejected(store, purchaser, approver):
    pdp = PolicyDecisionPoint(approvals=store, forced_dry_run=False)
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
        ttl_minutes=1,
    )
    # Kaydin suresini elle gecmise cek.
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with store._db.write() as conn:  # noqa: SLF001 - testte dogrudan durum kurgusu
        conn.execute(
            "UPDATE approvals SET expires_at = ? WHERE approval_id = ?", (past, record.approval_id)
        )
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.denial_code == "APPROVAL_INVALID"


def test_sod_blocks_self_approval(pdp, store, approver):
    """Onaylayan kisi ayni islemi yurutemez."""
    executor = ActorContext(
        subject=approver.subject,
        tenant="100",
        roles=("APPROVER", "PURCHASER"),
        plants=frozenset({"1100"}),
    )
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
    )
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(executor)
    )
    assert decision.denial_code == "APPROVAL_INVALID"
    assert "SoD" in " ".join(decision.reasons)


def test_approver_without_approve_scope_is_rejected(pdp, store, purchaser):
    """PURCHASER onaylayan olarak kabul edilmez (SoD rol ayrimi)."""
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[purchaser],
    )
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.denial_code == "APPROVAL_INVALID"


def test_r4_requires_two_approvers(pdp, store, purchaser, approver):
    spec = _Spec(risk_tier=RiskTier.R4, approval_policy="dual")
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
    )
    decision = pdp.evaluate(
        spec, {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.denial_code == "APPROVAL_INVALID"
    assert "cift onay" in " ".join(decision.reasons)


def test_cross_tenant_approval_is_rejected(pdp, store, purchaser, approver):
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="200",
        approvers=[approver],
    )
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.denial_code == "APPROVAL_INVALID"


# --- Esik ve pencere -------------------------------------------------------
def test_threshold_policy_defers_to_verified_value(pdp, purchaser):
    """Tutar bilinmiyorsa policy gecirir ama dogrulama yukumlulugu koyar."""
    spec = _Spec(approval_policy="threshold")
    decision = pdp.evaluate(spec, {"items": []}, _execution(purchaser))
    assert decision.allowed
    assert decision.requires(OBLIGATION_VERIFY_VALUE)


def test_verified_value_over_threshold_without_approval_is_flagged(pdp, purchaser):
    spec = _Spec(approval_policy="threshold")
    decision = pdp.evaluate(spec, {"items": []}, _execution(purchaser))
    violation = pdp.require_approval_for_value(decision, value=50_000.0, currency="EUR")
    assert violation and "onay kaydi yok" in violation


def test_value_above_approval_scope_limit_is_flagged(pdp, store, purchaser, approver):
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
        scope={"max_value": 30_000.0},
    )
    decision = pdp.evaluate(
        _Spec(approval_policy="threshold"),
        {**arguments, "approval_id": record.approval_id},
        _execution(purchaser),
    )
    assert decision.allowed
    violation = pdp.require_approval_for_value(decision, value=45_000.0, currency="EUR")
    assert violation and "ust sinirin" in violation


def test_declared_value_below_threshold_skips_approval(pdp, purchaser):
    spec = _Spec(approval_policy="threshold")
    decision = pdp.evaluate(spec, {"total_value": 100.0}, _execution(purchaser))
    assert decision.allowed


def test_closed_write_window_blocks_r3(store, purchaser):
    # Gecmiste kapanan bir pencere: 00:00-00:01
    pdp = PolicyDecisionPoint(
        approvals=store, write_window="00:00-00:01", forced_dry_run=False
    )
    now = datetime.now(timezone.utc)
    if now.hour == 0 and now.minute <= 1:  # pragma: no cover - gunun dakikasina bagli
        pytest.skip("Test penceresi su anda acik.")
    decision = pdp.evaluate(_Spec(), {"items": []}, _execution(purchaser))
    assert decision.denial_code == "WINDOW_CLOSED"


def test_forced_dry_run_is_reported_as_obligation(store, purchaser, approver):
    pdp = PolicyDecisionPoint(approvals=store, forced_dry_run=True)
    arguments = {"items": []}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
    )
    decision = pdp.evaluate(
        _Spec(), {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.allowed
    assert "dry_run_forced" in decision.obligations


# --- Kayit defteri tutarliligi --------------------------------------------
def test_every_registered_mutating_tool_declares_full_contract():
    load_all_tools()
    for spec in REGISTRY.values():
        if not spec.risk_tier.is_mutating:
            continue
        assert spec.required_scopes, spec.name
        assert spec.approval_policy in {"threshold", "always", "dual"}, spec.name
        assert spec.idempotent, spec.name
        assert "idempotency_key" in spec.input_schema["properties"], spec.name


def test_unknown_tool_is_denied(pdp, purchaser):
    decision = pdp.evaluate(None, {}, _execution(purchaser), tool_name="sap_delete_everything")
    assert decision.denial_code == "UNKNOWN_TOOL"
