"""Dinamik risk motorunun guvenlik ve kabul kriteri testleri.

Kontrol edilen risk kabul kriterleri:

  - Tool'un statik seviyesi runtime tarafindan dusurulemez.
  - Tutar model argumanindan degil SAP/prepare snapshot'indan alinir.
  - Esik alti beyanla approval atlatma testi basarisiz olur.
  - Bulk ve geri donussuz islem en az R4 olur.
  - R4 gecerli iki ayri approver olmadan calismaz.
  - Risk karari aciklanabilir boyutlar ve puanlarla audit'te bulunur.
"""

from __future__ import annotations

import pytest

from robotics_agent.contracts import (
    SCOPE_PR_APPROVE,
    SCOPE_PR_WRITE,
    ActorContext,
    ExecutionContext,
    RiskTier,
)
from robotics_agent.core import (
    OBLIGATION_DUAL_CONTROL,
    OBLIGATION_EXPLAIN_ESCALATION,
    OBLIGATION_MASK_SENSITIVE,
    OBLIGATION_REASSESS_AFTER_PRICING,
    ApprovalStore,
    PolicyDecisionPoint,
    PolicyOutcome,
    approval_payload_for,
    get_state_db,
)
from robotics_agent.privacy import DataClass, DataPolicy
from robotics_agent.risk import (
    READ_ONLY,
    ImpactProfile,
    ImpactSignals,
    MutationKind,
    Reversibility,
    RiskObligations,
    score_impact,
    tier_for_score,
)


class _Spec:
    """Risk ve veri politikasi alanlarini iceren minimal test tool sozlesmesi."""

    def __init__(
        self,
        *,
        name="sap_pr_submit",
        risk_tier=RiskTier.R3,
        required_scopes=(SCOPE_PR_WRITE,),
        approval_policy="always",
        idempotent=True,
        impact_profile=None,
        data_policy=None,
    ) -> None:
        self.name = name
        self.risk_tier = risk_tier
        self.required_scopes = required_scopes
        self.approve_scope = SCOPE_PR_APPROVE
        self.approval_policy = approval_policy
        self.idempotent = idempotent
        self.applies_org_defaults = False
        self.impact_profile = impact_profile or ImpactProfile(
            mutation=MutationKind.WRITE,
            reversible=Reversibility.COMPENSATING,
            financial_fields=("total_value",),
            record_count_field="items",
        )
        self.data_policy = data_policy or DataPolicy()


@pytest.fixture
def store(tmp_path) -> ApprovalStore:
    return ApprovalStore(get_state_db(tmp_path / "risk.sqlite3"), default_ttl_minutes=30)


@pytest.fixture
def pdp(store) -> PolicyDecisionPoint:
    return PolicyDecisionPoint(
        approvals=store, forced_dry_run=False, approval_threshold=25_000.0
    )


def _execution(actor: ActorContext) -> ExecutionContext:
    return ExecutionContext(actor=actor, system_alias="S4-TEST", dry_run=False)


# --- Skor bandlari ---------------------------------------------------------
@pytest.mark.parametrize(
    ("score", "tier"),
    [(0, RiskTier.R0), (9, RiskTier.R0), (10, RiskTier.R1), (24, RiskTier.R1),
     (25, RiskTier.R2), (44, RiskTier.R2), (45, RiskTier.R3), (69, RiskTier.R3),
     (70, RiskTier.R4), (100, RiskTier.R4)],
)
def test_score_maps_to_declared_bands(score, tier):
    assert tier_for_score(score) is tier


# --- Statik taban korunur --------------------------------------------------
def test_runtime_can_never_lower_the_declared_tier():
    """Runtime degerlendirmesi tool'un bildirdigi statik risk tabanini dusuremez."""
    assessment = score_impact(READ_ONLY, ImpactSignals(), declared_tier=RiskTier.R3)
    assert assessment.runtime_tier is RiskTier.R0
    assert assessment.effective_tier is RiskTier.R3


def test_declared_read_tool_cannot_hide_a_write(pdp):
    """Bir R3 tool kendini `mutation=read` bildirerek skorlamadan kacamaz."""
    problems = READ_ONLY.validate(risk_tier_level=3)
    assert problems and "mutation" in problems[0]


# --- Tutar dogrulamasi -----------------------------------------------------
def test_low_declared_value_gives_no_discount():
    """Esik alti beyan skoru DUSURMEZ; yalniz dogrulanmis tutar yukseltir."""
    profile = ImpactProfile(
        mutation=MutationKind.WRITE,
        reversible=Reversibility.COMPENSATING,
        financial_fields=("total_value",),
    )
    lying = score_impact(
        profile, ImpactSignals.from_arguments(profile, {"total_value": 1.0}),
        declared_tier=RiskTier.R3,
    )
    silent = score_impact(profile, ImpactSignals(), declared_tier=RiskTier.R3)
    # Dusuk beyan, hic beyan etmemekten daha iyi bir sonuc vermez.
    assert lying.effective_tier is silent.effective_tier is RiskTier.R3
    assert not lying.value_verified


def test_verified_value_escalates_the_tier():
    """Dogrulanmis SAP tutari etkili risk seviyesini yukseltebilir."""
    profile = ImpactProfile(
        mutation=MutationKind.WRITE,
        reversible=Reversibility.COMPENSATING,
        financial_fields=("total_value",),
        external_commitment=True,
    )
    signals = ImpactSignals.from_arguments(profile, {"total_value": 100.0})
    before = score_impact(profile, signals, declared_tier=RiskTier.R3)
    after = score_impact(
        profile,
        signals.verified_with(total_value=2_400_000.0, currency="EUR", record_count=120),
        declared_tier=RiskTier.R3,
    )
    assert before.effective_tier is RiskTier.R3
    assert after.effective_tier is RiskTier.R4
    assert after.value_verified and after.escalated


def test_reassess_uses_sap_value_not_arguments(pdp):
    spec = _Spec()
    purchaser = ActorContext(
        subject="alici@firma.test", tenant="100", roles=("PURCHASER",), auth_method="test"
    )
    decision = pdp.evaluate(spec, {"total_value": 10.0, "items": [{}]}, _execution(purchaser))
    # Onaysiz cagri zaten reddedilir; onemli olan yukumlulugun uretilmis olmasi.
    assert decision.impact is not None and not decision.impact.value_verified

    reassessed = pdp.reassess(
        spec, decision, total_value=3_000_000.0, currency="EUR", record_count=200
    )
    assert reassessed.value_verified
    assert reassessed.effective_tier is RiskTier.R4


def test_escalation_blocker_requires_two_approvers(pdp, store):
    spec = _Spec()
    purchaser = ActorContext(
        subject="alici@firma.test", tenant="100", roles=("PURCHASER",), auth_method="test"
    )
    approver = ActorContext(
        subject="onay@firma.test", tenant="100", roles=("APPROVER",), auth_method="test"
    )
    arguments = {"items": [{"material_id": "X", "quantity": 1}]}
    record = store.issue(
        tool="sap_pr_submit",
        payload=approval_payload_for(arguments),
        tenant="100",
        approvers=[approver],
        requested_by=purchaser.subject,
    )
    decision = pdp.evaluate(
        spec, {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.outcome is PolicyOutcome.ALLOW

    escalated = pdp.reassess(spec, decision, total_value=5_000_000.0, currency="EUR")
    blocker = pdp.escalation_blocker(decision, escalated)
    assert blocker is not None and "iki ayri onaylayan" in blocker


# --- Bulk / geri donussuz --------------------------------------------------
@pytest.mark.parametrize(
    "profile",
    [
        ImpactProfile(mutation=MutationKind.BULK_WRITE, reversible=Reversibility.COMPENSATING),
        ImpactProfile(mutation=MutationKind.WRITE, reversible=Reversibility.IRREVERSIBLE),
        ImpactProfile(mutation=MutationKind.DESTRUCTIVE, reversible=Reversibility.IRREVERSIBLE),
    ],
)
def test_bulk_and_irreversible_operations_are_at_least_r4(profile):
    """Bulk ve geri donussuz islemler en az R4 olarak degerlendirilir."""
    assessment = score_impact(profile, ImpactSignals(), declared_tier=RiskTier.R2)
    assert assessment.effective_tier is RiskTier.R4


def test_destructive_cannot_declare_itself_easily_reversible():
    problems = ImpactProfile(
        mutation=MutationKind.DESTRUCTIVE, reversible=Reversibility.EASY
    ).validate(risk_tier_level=4)
    assert any("destructive" in p for p in problems)


# --- Veri sinifi ayri eksendir ---------------------------------------------
def test_data_class_does_not_change_risk_tier():
    """Veri sinifi yazma riskinden bagimsizdir; salt okunur R0 tool D3 okuyabilir."""
    assessment = score_impact(
        READ_ONLY, ImpactSignals(data_class=DataClass.D3), declared_tier=RiskTier.R0
    )
    assert assessment.effective_tier is RiskTier.R0
    obligations = RiskObligations.derive(assessment)
    # Yazma onayi gerekmez ama maskeleme ve export kontrolu zorunlu olur.
    assert not obligations.dual_control
    assert obligations.masking_required and obligations.export_blocked


def test_privacy_obligations_reach_the_policy_decision(pdp):
    spec = _Spec(
        name="sap_read_sensitive",
        risk_tier=RiskTier.R0,
        required_scopes=(),
        approval_policy="none",
        idempotent=False,
        impact_profile=READ_ONLY,
        data_policy=DataPolicy(fields={"iban": DataClass.D3}),
    )
    actor = ActorContext(
        subject="a@firma.test", tenant="100", roles=("PURCHASER",), auth_method="test"
    )
    decision = pdp.evaluate(spec, {}, _execution(actor))
    assert decision.requires(OBLIGATION_MASK_SENSITIVE)
    assert decision.requires("block_export")


# --- Aciklanabilirlik ------------------------------------------------------
def test_risk_decision_is_explainable_with_dimensions():
    """Risk karari boyutlari ve puanlariyla aciklanabilir ve audit edilebilir."""
    profile = ImpactProfile(
        mutation=MutationKind.WRITE,
        reversible=Reversibility.COMPENSATING,
        external_commitment=True,
        financial_fields=("total_value",),
    )
    assessment = score_impact(
        profile,
        ImpactSignals(total_value=120_000, currency="EUR", value_verified=True, record_count=40),
        declared_tier=RiskTier.R3,
    )
    payload = assessment.to_dict()
    assert set(payload["dimensions"]) == {
        "action", "financial", "breadth", "reversibility", "commitment", "master_data",
    }
    assert payload["score"] == sum(payload["dimensions"].values())
    assert payload["value_verified"] is True
    assert payload["reasons"]


def test_policy_decision_carries_impact_into_audit(pdp):
    spec = _Spec(
        name="sap_read", risk_tier=RiskTier.R0, required_scopes=(),
        approval_policy="none", idempotent=False, impact_profile=READ_ONLY,
    )
    actor = ActorContext(
        subject="a@firma.test", tenant="100", roles=("VIEWER",), auth_method="test"
    )
    payload = pdp.evaluate(spec, {}, _execution(actor)).to_dict()
    assert payload["impact"]["effective_tier"] == "R0"
    assert "dimensions" in payload["impact"]


def test_escalation_obligations_are_emitted(pdp):
    """Runtime yukseltmesi gerekce ve yeniden degerlendirme yukumlulugu uretir."""
    spec = _Spec()
    purchaser = ActorContext(
        subject="alici@firma.test", tenant="100", roles=("PURCHASER",), auth_method="test"
    )
    decision = pdp.evaluate(spec, {"items": [{"material_id": "X"}]}, _execution(purchaser))
    assert decision.denial_code == "APPROVAL_REQUIRED"  # onaysiz yazma reddedilir

    # Onay verildiginde yukumlulukler gorunur olur.
    from robotics_agent.core import approval_payload_for as canonical

    approver = ActorContext(
        subject="onay@firma.test", tenant="100", roles=("APPROVER",), auth_method="test"
    )
    arguments = {"items": [{"material_id": "X"}]}
    record = pdp.approvals.issue(
        tool="sap_pr_submit", payload=canonical(arguments), tenant="100",
        approvers=[approver], requested_by=purchaser.subject,
    )
    allowed = pdp.evaluate(
        spec, {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert allowed.requires(OBLIGATION_REASSESS_AFTER_PRICING)


def test_report_mode_records_but_does_not_enforce(store):
    """`AGENT_RISK_SCORING_MODE=report`: skor audit'e yazilir, seviye degismez."""
    pdp = PolicyDecisionPoint(
        approvals=store, forced_dry_run=False, risk_mode="report"
    )
    spec = _Spec(
        risk_tier=RiskTier.R2,
        approval_policy="none",
        idempotent=False,
        impact_profile=ImpactProfile(
            mutation=MutationKind.BULK_WRITE, reversible=Reversibility.IRREVERSIBLE
        ),
    )
    actor = ActorContext(
        subject="a@firma.test", tenant="100", roles=("PURCHASER",), auth_method="test"
    )
    decision = pdp.evaluate(spec, {}, _execution(actor))
    # enforce modunda R4 olurdu; report modunda bildirilen seviye korunur.
    assert decision.risk_tier is RiskTier.R2
    assert decision.impact.effective_tier is RiskTier.R4  # audit yine de gorur


def test_dual_control_obligation_on_r4(pdp, store):
    spec = _Spec(
        name="sap_bulk_write",
        risk_tier=RiskTier.R4,
        approval_policy="dual",
        impact_profile=ImpactProfile(
            mutation=MutationKind.BULK_WRITE, reversible=Reversibility.IRREVERSIBLE
        ),
    )
    purchaser = ActorContext(
        subject="alici@firma.test", tenant="100", roles=("PURCHASER",), auth_method="test"
    )
    a1 = ActorContext(subject="o1@firma.test", tenant="100", roles=("APPROVER",))
    a2 = ActorContext(subject="o2@firma.test", tenant="100", roles=("APPROVER",))
    arguments = {"items": [{"material_id": "X"}]}
    record = store.issue(
        tool="sap_bulk_write", payload=approval_payload_for(arguments), tenant="100",
        approvers=[a1, a2], requested_by=purchaser.subject,
    )
    decision = pdp.evaluate(
        spec, {**arguments, "approval_id": record.approval_id}, _execution(purchaser)
    )
    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.requires(OBLIGATION_DUAL_CONTROL)
    assert decision.requires(OBLIGATION_EXPLAIN_ESCALATION) or not decision.escalated
