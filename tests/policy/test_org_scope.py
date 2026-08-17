"""Organizasyon kapsami (ABAC) guvenlik testleri.

Kontrol edilen iki kritik davranis:
  1. Ic ice alanlar (`items[*].plant`) artik taraniyor.
  2. Tesis/sirket kodu argumanda verilmediginde handler'in kullanacagi sistem
     varsayilani da actor kapsamina karsi denetleniyor.

Ozet kural: alani bos birakmak kapsam kontrolunu atlatmanin yolu degildir.
"""

from __future__ import annotations

import pytest

from robotics_agent.contracts import (
    ORG_WILDCARD,
    SCOPE_SAP_PREPARE,
    SCOPE_SAP_READ,
    ActorContext,
    ExecutionContext,
    RiskTier,
)
from robotics_agent.core import OrgDefaults, PolicyDecisionPoint
from robotics_agent.sap import build_backend
from robotics_agent.tools import ToolContext, load_all_tools


class _Spec:
    def __init__(
        self,
        *,
        name="sap_stock_overview",
        risk_tier=RiskTier.R0,
        required_scopes=(SCOPE_SAP_READ,),
        applies_org_defaults=True,
    ) -> None:
        self.name = name
        self.risk_tier = risk_tier
        self.required_scopes = required_scopes
        self.approval_policy = "none"
        self.approve_scope = "sap.pr.approve"
        self.idempotent = False
        self.applies_org_defaults = applies_org_defaults


@pytest.fixture
def pdp() -> PolicyDecisionPoint:
    return PolicyDecisionPoint(
        forced_dry_run=True,
        org_defaults=OrgDefaults(plant="1100", company_code="1000", purchasing_org="1000"),
    )


def _execution(actor: ActorContext) -> ExecutionContext:
    return ExecutionContext(actor=actor, system_alias="S4-TEST", dry_run=True)


@pytest.fixture
def restricted() -> ActorContext:
    """Yalniz 2200 tesisine yetkili actor.

    Sirket kodu / satinalma organizasyonu bilerek serbest birakildi; boylece
    testler tek bir degiskeni (tesis) izole eder. Sirket kodu kisiti ayri bir
    testte dogrulanir.
    """
    return ActorContext(
        subject="kisitli@firma.test",
        tenant="100",
        roles=("PURCHASER",),
        plants=frozenset({"2200"}),
        company_codes=frozenset({ORG_WILDCARD}),
        purchasing_orgs=frozenset({ORG_WILDCARD}),
        auth_method="test",
    )


# --- Varsayilan deger kapisi ------------------------------------------------
def test_omitted_plant_falls_back_to_default_and_is_checked(pdp, restricted):
    """Tesis verilmezse sistem varsayilani (1100) denetlenir ve reddedilir."""
    decision = pdp.evaluate(_Spec(), {"material_ids": ["X"]}, _execution(restricted))
    assert decision.denial_code == "ORG_SCOPE"
    assert "1100" in " ".join(decision.reasons)
    assert "acikca belirtin" in " ".join(decision.reasons)


def test_explicit_authorized_plant_is_allowed(pdp, restricted):
    decision = pdp.evaluate(
        _Spec(), {"material_ids": ["X"], "plant": "2200"}, _execution(restricted)
    )
    assert decision.allowed


def test_default_is_allowed_for_matching_actor(pdp, purchaser):
    """1100'e yetkili actor icin varsayilan sorun degil."""
    decision = pdp.evaluate(_Spec(), {"material_ids": ["X"]}, _execution(purchaser))
    assert decision.allowed


def test_non_org_tool_is_not_blocked_by_defaults(pdp, restricted):
    """SAP organizasyonuna dokunmayan tool varsayilan kontrolune takilmaz."""
    spec = _Spec(
        name="sap_simulate_only",
        risk_tier=RiskTier.R1,
        required_scopes=("sap.simulate",),
        applies_org_defaults=False,
    )
    assert pdp.evaluate(spec, {"calculation": "cycle_time"}, _execution(restricted)).allowed


# --- Ic ice alanlar ---------------------------------------------------------
def test_nested_plant_in_items_is_detected(pdp, restricted):
    """`items[*].plant` ust seviye taramaya takilmiyordu; artik yakalaniyor."""
    decision = pdp.evaluate(
        _Spec(name="sap_pr_prepare", required_scopes=(SCOPE_SAP_PREPARE,), risk_tier=RiskTier.R2),
        {"items": [{"material_id": "X", "quantity": 1, "plant": "1100"}]},
        _execution(restricted),
    )
    assert decision.denial_code == "ORG_SCOPE"
    assert "1100" in " ".join(decision.reasons)


def test_deeply_nested_plant_is_detected(pdp, restricted):
    decision = pdp.evaluate(
        _Spec(),
        {"payload": {"header": {"lines": [{"plant": "1100"}]}}},
        _execution(restricted),
    )
    assert decision.denial_code == "ORG_SCOPE"


def test_nested_authorized_plant_is_allowed(pdp, restricted):
    decision = pdp.evaluate(
        _Spec(name="sap_pr_prepare", required_scopes=(SCOPE_SAP_PREPARE,), risk_tier=RiskTier.R2),
        {"items": [{"material_id": "X", "quantity": 1, "plant": "2200"}]},
        _execution(restricted),
    )
    assert decision.allowed


def test_mixed_plants_deny_if_any_is_unauthorized(pdp, restricted):
    decision = pdp.evaluate(
        _Spec(name="sap_pr_prepare", required_scopes=(SCOPE_SAP_PREPARE,), risk_tier=RiskTier.R2),
        {
            "items": [
                {"material_id": "A", "quantity": 1, "plant": "2200"},
                {"material_id": "B", "quantity": 1, "plant": "1100"},
            ]
        },
        _execution(restricted),
    )
    assert decision.denial_code == "ORG_SCOPE"


def test_company_code_and_purchasing_org_are_checked(pdp):
    """Tesis disindaki organizasyon alanlari da ayni kurala tabidir."""
    actor = ActorContext(
        subject="baska-sirket@firma.test",
        tenant="100",
        roles=("PURCHASER",),
        plants=frozenset({ORG_WILDCARD}),
        company_codes=frozenset({"2000"}),
        purchasing_orgs=frozenset({"2000"}),
    )
    for key, bad in (("company_code", "1000"), ("purchasing_org", "1000")):
        decision = pdp.evaluate(_Spec(), {key: bad}, _execution(actor))
        assert decision.denial_code == "ORG_SCOPE", key

    # Varsayilanlar da ayni sekilde denetlenir: hicbiri verilmezse reddedilir.
    assert pdp.evaluate(_Spec(), {}, _execution(actor)).denial_code == "ORG_SCOPE"


def test_excessively_deep_arguments_are_rejected(pdp, restricted):
    payload: dict = {"plant": "2200"}
    node = payload
    for _ in range(15):
        node["child"] = {}
        node = node["child"]
    decision = pdp.evaluate(_Spec(), payload, _execution(restricted))
    assert decision.denial_code == "ORG_SCOPE"
    assert "cok derin" in " ".join(decision.reasons)


def test_wildcard_actor_passes_any_plant(pdp):
    actor = ActorContext(
        subject="genel@firma.test",
        tenant="100",
        roles=("PURCHASER",),
        plants=frozenset({ORG_WILDCARD}),
        company_codes=frozenset({ORG_WILDCARD}),
        purchasing_orgs=frozenset({ORG_WILDCARD}),
    )
    assert pdp.evaluate(_Spec(), {"plant": "9999"}, _execution(actor)).allowed


def test_actor_without_org_scope_is_denied_for_sap_tools(pdp):
    """Bos kapsam 'her sey serbest' degil, 'hicbir sey' demektir."""
    actor = ActorContext(subject="bos@firma.test", tenant="100", roles=("PURCHASER",))
    assert not actor.has_any_org_scope
    decision = pdp.evaluate(_Spec(), {"material_ids": ["X"]}, _execution(actor))
    assert decision.denial_code == "ORG_SCOPE"


# --- Uctan uca: gercek tool cagrisi -----------------------------------------
def test_restricted_actor_cannot_read_default_plant_through_tool(settings, restricted, run_tool):
    load_all_tools()
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=restricted)
    result = run_tool(
        "sap_stock_overview", ctx, material_ids=["ROB-6AX-20-1800"], expect_error=True
    )
    assert result["denial_code"] == "ORG_SCOPE"


def test_restricted_actor_cannot_slip_plant_into_pr_items(settings, restricted, run_tool):
    load_all_tools()
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=restricted)
    result = run_tool(
        "sap_pr_prepare",
        ctx,
        items=[{"material_id": "SFT-SCN-270", "quantity": 4, "plant": "1100"}],
        expect_error=True,
    )
    assert result["denial_code"] == "ORG_SCOPE"
    assert result["remediation"]


def test_registered_sap_tools_declare_org_scope():
    """SAP verisine dokunan her tool org-scoped olmali (fail-closed varsayilan)."""
    from robotics_agent.tools import REGISTRY

    load_all_tools()
    for spec in REGISTRY.values():
        touches_sap = SCOPE_SAP_READ in spec.required_scopes or spec.risk_tier.is_mutating
        if touches_sap:
            assert spec.applies_org_defaults, spec.name
