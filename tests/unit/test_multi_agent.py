"""SAP multi-agent izolasyonu ve handoff sozlesmesi testleri."""

from __future__ import annotations

import json
from types import SimpleNamespace

from robotics_agent.agent import AgentTurn, SAPDomainAgent, SAPMultiAgent, ToolCall
from robotics_agent.core import AGENT_SPECS, handoff_from_turn, plan_agents


def _with_api_key(settings):
    object.__setattr__(settings.agent, "api_key", "test-key")
    return settings


def test_domain_agents_see_only_fixed_tool_sets(settings, purchaser):
    settings = _with_api_key(settings)
    master = SAPDomainAgent(settings, actor=purchaser, agent_spec=AGENT_SPECS["master_data"])
    procurement = SAPDomainAgent(
        settings, actor=purchaser, agent_spec=AGENT_SPECS["procurement"]
    )
    finance = SAPDomainAgent(settings, actor=purchaser, agent_spec=AGENT_SPECS["finance"])

    assert "sap_material_360" in master._visible_names()  # noqa: SLF001
    assert "sap_pr_submit" not in master._visible_names()  # noqa: SLF001
    assert "sap_pr_submit" in procurement._visible_names()  # noqa: SLF001
    assert "sap_generate_report" in finance._visible_names()  # noqa: SLF001
    assert "sap_pr_submit" not in finance._visible_names()  # noqa: SLF001


def test_multi_agent_health_exposes_architecture(settings, purchaser):
    agent = SAPMultiAgent(_with_api_key(settings), actor=purchaser)
    health = agent.health()
    assert health["architecture"] == "certaops"
    assert health["registered_agents"] == 5
    assert {row["agent"] for row in health["agents"]} == set(AGENT_SPECS)


def test_handoff_extracts_only_bounded_evidence_and_business_ids():
    call = SimpleNamespace(
        result=json.dumps(
            {
                "business_object_id": "10000431",
                "_meta": {"evidence_id": "ev_123"},
                "warnings": ["Kontrol gerekli"],
                "large_payload": "x" * 5000,
            }
        )
    )
    handoff = handoff_from_turn(
        from_agent="supply_chain",
        to_agent="procurement",
        objective="ATP eksigi icin PR hazirla",
        correlation_id="corr-1",
        text="ozet",
        tool_calls=[call],
        needs_review=False,
    )
    payload = handoff.to_dict()
    assert payload["schema"] == "sap-agent-handoff/v1"
    assert payload["evidence_ids"] == ["ev_123"]
    assert payload["business_objects"] == ["10000431"]
    assert "large_payload" not in payload


def test_unauthorized_actor_cannot_route_to_procurement_write(viewer):
    plan = plan_agents("Satinalma talebi olustur ve SAP'a gonder", viewer)
    assert "procurement" not in plan.agents


def test_public_package_exports_new_names():
    from certaops import SAPDomainAgent as PublicDomainAgent
    from certaops import SAPMultiAgent as PublicMultiAgent

    assert PublicDomainAgent is SAPDomainAgent
    assert PublicMultiAgent is SAPMultiAgent


def test_orchestrator_runs_agents_in_order_with_structured_handoff(settings, purchaser):
    agent = SAPMultiAgent(_with_api_key(settings), actor=purchaser)
    received: dict[str, str] = {}

    class FakeDomainAgent:
        def __init__(self, key: str) -> None:
            self.key = key

        def chat(self, message: str, **_kwargs) -> AgentTurn:
            received[self.key] = message
            calls = []
            if self.key == "finance":
                calls.append(
                    ToolCall(
                        name="sap_wbs_budget_status",
                        arguments={"wbs": "PRJ-1000"},
                        result=json.dumps(
                            {
                                "business_object_id": "PRJ-1000",
                                "_meta": {"evidence_id": "ev-finance-1"},
                                "private_payload": "aktarilmamali",
                            }
                        ),
                        is_error=False,
                    )
                )
            return AgentTurn(
                text=f"{self.key} sonucu",
                tool_calls=calls,
                execution_id=f"exec-{self.key}",
                correlation_id=f"corr-{self.key}",
                stop_reason="end_turn",
            )

    agent._domain_agents["finance"] = FakeDomainAgent("finance")  # type: ignore[assignment]  # noqa: SLF001
    agent._domain_agents["master_data"] = FakeDomainAgent("master_data")  # type: ignore[assignment]  # noqa: SLF001

    turn = agent.chat("malzeme 360 ve WBS proje maliyeti raporu")

    assert turn.active_agents == ["finance", "master_data"]
    assert [row["agent"] for row in turn.agent_trace] == ["finance", "master_data"]
    assert "sap-agent-handoff/v1" in received["master_data"]
    assert "ev-finance-1" in received["master_data"]
    assert "PRJ-1000" in received["master_data"]
    assert "private_payload" not in received["master_data"]
    assert "SAP Proje Finans" in turn.text
    assert "SAP Ana Veri" in turn.text
