"""S/4HANA read-only urun profilinin uctan uca guvenlik degismezleri."""

from __future__ import annotations

import json

import pytest

from robotics_agent.adapters.sap import SAPError
from robotics_agent.contracts import ActorContext
from robotics_agent.core.router import PACKS, domains_for_packs
from robotics_agent.sap import build_backend
from robotics_agent.sap.models import PurchaseRequisitionItem
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools, visible_tool_names
from robotics_agent.tools.registry import REGISTRY


@pytest.fixture(autouse=True)
def _tools_loaded():
    load_all_tools()


def _all_domains() -> frozenset[str]:
    return domains_for_packs(tuple(PACKS))


def test_read_only_is_the_default_product_profile(settings):
    assert settings.sap.read_only is True
    assert settings.posture()["read_only"] is True


def test_read_only_catalog_never_exposes_mutating_tools(settings):
    actor = ActorContext.local_operator(
        subject="lead@firma.test",
        tenant=settings.sap.tenant,
        roles=("BUYER_LEAD", "AUDITOR"),
        company_code=settings.sap.company_code,
        plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )
    names = visible_tool_names(_all_domains(), actor, settings=settings)

    assert "sap_pr_submit" not in names
    assert "sap_pr_prepare" in names
    assert all(not REGISTRY[name].risk_tier.is_mutating for name in names)


def test_policy_denies_guessed_write_tool_before_handler(settings, purchaser):
    sap = build_backend(settings)
    ctx = ToolContext(settings=settings, sap=sap, actor=purchaser)
    payload, is_error = execute_tool(
        "sap_pr_submit",
        {
            "items": [{"material_id": "SFT-SCN-270", "quantity": 1}],
            "idempotency_key": "readonly:guess:v1",
        },
        ctx,
    )

    assert is_error
    assert json.loads(payload)["denial_code"] == "READ_ONLY_MODE"
    assert sap.find_purchase_requisition_by_reference("readonly:guess:v1") is None


def test_prepare_stays_useful_without_creating_approval_task(settings, purchaser):
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    payload, is_error = execute_tool(
        "sap_pr_prepare",
        {
            "items": [
                {
                    "material_id": "HD-GEAR-CSF25-100",
                    "quantity": 30,
                    "wbs_element": "R-2026-021-1",
                }
            ]
        },
        ctx,
    )
    result = json.loads(payload)

    assert not is_error, payload
    assert result["written_to_sap"] is False
    assert result["submission_enabled"] is False
    assert result["requires_human_approval"] is False
    assert result["approval_required_if_write_enabled"] is True
    assert "approval_task" not in result
    assert "sap_pr_submit" not in result["next_step"]


def test_backend_direct_write_is_blocked_in_simulator_too(settings):
    sap = build_backend(settings)
    draft = sap.prepare_purchase_requisition(
        [PurchaseRequisitionItem(material_id="SFT-SCN-270", quantity=1)]
    )
    with pytest.raises(SAPError) as exc:
        sap.submit_purchase_requisition(draft, external_reference="readonly:direct:v1")
    assert exc.value.code == "READ_ONLY_MODE"


def test_write_profile_cannot_be_marked_production_ready(settings_factory, tmp_path):
    settings = settings_factory(
        tmp_path,
        **{
            "app_env": "production",
            "sap.read_only": False,
            "sap.dry_run": False,
        },
    )
    assert any("SAP_READ_ONLY=false" in item for item in settings.production_blockers())
