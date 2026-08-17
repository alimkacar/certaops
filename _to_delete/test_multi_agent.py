"""Tek runtime, dinamik domain profilleri ve compatibility facade testleri."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fakes import FakeModelProvider

from robotics_agent.agent import SAPAgentRuntime, SAPMultiAgent
from robotics_agent.contracts import estimate_tokens
from robotics_agent.core import handoff_from_turn, plan_agents, profiles_for_packs
from robotics_agent.providers import FunctionCall, ModelResponse


def test_compatibility_facade_is_one_runtime(settings, purchaser):
    provider = FakeModelProvider()
    runtime = SAPMultiAgent(settings, actor=purchaser, model_provider=provider)

    assert issubclass(SAPMultiAgent, SAPAgentRuntime)
    assert isinstance(runtime, SAPAgentRuntime)
    assert runtime.provider is provider
    assert not hasattr(runtime, "_domain_agents")

    runtime.reset()
    runtime.close()
    assert provider.reset_count == 1
    assert provider.closed is True


def test_multi_domain_request_uses_one_provider_and_exact_profiles(settings, purchaser):
    provider = FakeModelProvider()
    runtime = SAPMultiAgent(settings, actor=purchaser, model_provider=provider)

    turn = runtime.chat("malzeme 360 ve WBS proje maliyeti raporu")

    assert provider.call_count == 1
    assert turn.active_packs == [
        "bootstrap",
        "project_finance",
        "master_data",
        "reporting",
    ]
    assert turn.active_agents == ["finance", "master_data"]
    assert profiles_for_packs(turn.active_packs) == ("finance", "master_data")
    assert runtime.last_plan.routing is runtime.last_routing
    assert runtime.last_plan.agents == profiles_for_packs(turn.active_packs)

    visible_functions = {declaration.name for declaration in provider.requests[0].tools}
    assert "sap_material_360" in visible_functions
    assert "sap_project_cost_status" in visible_functions
    assert "sap_generate_report" in visible_functions
    assert "sap_pr_submit" not in visible_functions


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
    from certaops import SAPAgentRuntime as PublicRuntime
    from certaops import SAPMultiAgent as PublicMultiAgent

    assert PublicRuntime is SAPAgentRuntime
    assert PublicMultiAgent is SAPMultiAgent


def test_hidden_tool_call_fails_closed_at_execution(settings, purchaser):
    provider = FakeModelProvider(
        [
            ModelResponse(
                function_calls=(FunctionCall("hidden-1", "sap_pr_submit", {}),),
                status="requires_action",
            ),
            ModelResponse(text="Gizli tool calistirilmadi.", status="completed"),
        ]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
    object.__setattr__(settings.agent, "direct_answers_enabled", False)

    turn = runtime.chat("malzeme 360 gorunumunu getir")

    assert "sap_pr_submit" not in {declaration.name for declaration in provider.requests[0].tools}
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].is_error is True
    assert json.loads(turn.tool_calls[0].result)["denial_code"] == "TOOL_NOT_VISIBLE"


def test_mutating_tool_error_is_conservatively_marked_for_review(settings, purchaser):
    provider = FakeModelProvider(
        [
            ModelResponse(
                function_calls=(FunctionCall("write-error-1", "sap_pr_submit", {}),),
                status="requires_action",
            ),
            ModelResponse(text="Yazma istegi reddedildi.", status="completed"),
        ]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
    object.__setattr__(settings.agent, "direct_answers_enabled", False)

    turn = runtime.chat("satinalma talebini SAP'a gonder")

    assert turn.tool_calls[0].is_error is True
    assert turn.needs_review is True


def test_duplicate_function_call_id_does_not_repeat_sap_tool(settings, purchaser):
    repeated = FunctionCall("same-id", "sap_connection_health", {})
    provider = FakeModelProvider(
        [
            ModelResponse(function_calls=(repeated,), status="requires_action"),
            ModelResponse(function_calls=(repeated,), status="requires_action"),
            ModelResponse(text="Baglanti kontrol edildi.", status="completed"),
        ]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
    object.__setattr__(settings.agent, "direct_answers_enabled", False)

    turn = runtime.chat("baglanti sagligini ayrintili kontrol et")

    assert provider.call_count == 3
    assert [call.name for call in turn.tool_calls] == ["sap_connection_health"]
    assert any(
        row["event"] == "tool.duplicate_suppressed"
        for row in runtime.ctx.audit.chain(turn.execution_id)
    )
    result_tokens = estimate_tokens(turn.tool_calls[0].result)
    assert runtime.telemetry.snapshot()["avg_result_tokens"] >= result_tokens * 2


def test_router_truncation_is_audited_and_warned(settings, purchaser):
    provider = FakeModelProvider(
        [ModelResponse(text="Secilen domainler islendi.", status="completed")]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
    object.__setattr__(settings.agent, "direct_answers_enabled", False)

    turn = runtime.chat("baglanti malzeme stok satinalma talebi fatura maliyet rapor")

    assert runtime.last_routing is not None
    assert runtime.last_routing.truncated is True
    assert runtime.last_routing.omitted_packs
    assert turn.needs_review is True
    assert "Router pack siniri" in turn.text
    assert any(
        row["event"] == "routing.truncated" for row in runtime.ctx.audit.chain(turn.execution_id)
    )


def test_same_response_stops_handlers_at_aggregate_result_budget(
    settings_factory, tmp_path, purchaser
):
    from robotics_agent.contracts import RiskTier
    from robotics_agent.tools.registry import REGISTRY, ToolSpec

    settings = settings_factory(
        tmp_path,
        **{
            "budget.single_result_tokens": 100,
            "budget.turn_result_tokens": 60,
        },
    )
    calls = {"read": 0, "write": 0}

    def large_read(ctx, **kwargs):  # noqa: ARG001
        calls["read"] += 1
        return {"nested": {"scalar": "x" * 4_000}}

    def skipped_write(ctx, **kwargs):  # noqa: ARG001
        calls["write"] += 1
        return {"business_object_id": "should-not-exist"}

    REGISTRY["_test_turn_budget_read"] = ToolSpec(
        name="_test_turn_budget_read",
        description="Aggregate turn result budget test tool." + " x" * 40,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=large_read,
        domain="diagnostics",
        risk_tier=RiskTier.R0,
        required_scopes=(),
        result_token_budget=100,
        org_scoped=False,
    )
    REGISTRY["_test_turn_budget_write"] = ToolSpec(
        name="_test_turn_budget_write",
        description="Aggregate budget must skip this mutating test tool." + " x" * 40,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=skipped_write,
        domain="diagnostics",
        risk_tier=RiskTier.R3,
        required_scopes=(),
        result_token_budget=100,
        org_scoped=False,
    )
    provider = FakeModelProvider(
        [
            ModelResponse(
                function_calls=(
                    FunctionCall("budget-1", "_test_turn_budget_read", {}),
                    FunctionCall("budget-2", "_test_turn_budget_write", {}),
                ),
                status="requires_action",
            ),
            ModelResponse(text="Butce sinirinda tamamlandi.", status="completed"),
        ]
    )
    try:
        runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
        object.__setattr__(settings.agent, "direct_answers_enabled", False)

        turn = runtime.chat("baglanti sagligini ayrintili kontrol et")

        assert calls == {"read": 1, "write": 0}
        assert json.loads(turn.tool_calls[1].result)["denial_code"] == (
            "TURN_RESULT_BUDGET_EXCEEDED"
        )
        assert json.loads(turn.tool_calls[1].result)["needs_review"] is True
        assert runtime.telemetry.snapshot()["avg_result_tokens"] <= 60
        assert turn.needs_review is True
        assert "tool-sonucu butcesi" in turn.text
    finally:
        REGISTRY.pop("_test_turn_budget_read", None)
        REGISTRY.pop("_test_turn_budget_write", None)


def test_unique_tool_call_limit_blocks_excess_handlers(settings_factory, tmp_path, purchaser):
    from robotics_agent.contracts import RiskTier
    from robotics_agent.tools.registry import REGISTRY, ToolSpec

    settings = settings_factory(tmp_path, **{"agent.max_tool_iterations": 3})
    calls = {"count": 0}

    def tiny_read(ctx, **kwargs):  # noqa: ARG001
        calls["count"] += 1
        return {"ok": True}

    REGISTRY["_test_unique_call_cap"] = ToolSpec(
        name="_test_unique_call_cap",
        description="Unique per-turn tool call cap test tool." + " x" * 40,
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=tiny_read,
        domain="diagnostics",
        risk_tier=RiskTier.R0,
        required_scopes=(),
        result_token_budget=50,
        org_scoped=False,
    )
    provider = FakeModelProvider(
        [
            ModelResponse(
                function_calls=tuple(
                    FunctionCall(f"unique-{index}", "_test_unique_call_cap", {})
                    for index in range(5)
                ),
                status="requires_action",
            ),
            ModelResponse(text="Cagri siniri uygulandi.", status="completed"),
        ]
    )
    try:
        runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
        object.__setattr__(settings.agent, "direct_answers_enabled", False)

        turn = runtime.chat("baglanti sagligini ayrintili kontrol et")

        assert calls["count"] == 3
        denial_codes = [json.loads(call.result).get("denial_code") for call in turn.tool_calls]
        assert denial_codes[-2:] == [
            "TURN_TOOL_CALL_LIMIT_EXCEEDED",
            "TURN_TOOL_CALL_LIMIT_EXCEEDED",
        ]
        assert turn.needs_review is True
        assert "benzersiz tool-cagrisi" in turn.text
    finally:
        REGISTRY.pop("_test_unique_call_cap", None)


def test_mutating_call_id_reuse_mismatch_requires_review(settings, purchaser):
    provider = FakeModelProvider(
        [
            ModelResponse(
                function_calls=(FunctionCall("write-1", "sap_pr_submit", {}),),
                status="requires_action",
            ),
            ModelResponse(
                function_calls=(
                    FunctionCall(
                        "write-1",
                        "sap_pr_submit",
                        {"idempotency_key": "degistirilmis"},
                    ),
                ),
                status="requires_action",
            ),
            ModelResponse(text="Cagri durduruldu.", status="completed"),
        ]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
    object.__setattr__(settings.agent, "direct_answers_enabled", False)

    turn = runtime.chat("satinalma talebi olustur ve submit et")

    assert turn.needs_review is True
    assert json.loads(turn.tool_calls[-1].result)["denial_code"] == ("CALL_ID_REUSE_MISMATCH")
    assert any(
        row["event"] == "tool.call_id_reuse_mismatch"
        for row in runtime.ctx.audit.chain(turn.execution_id)
    )


def test_user_and_model_text_are_dlp_cleaned_before_persistence(settings, purchaser):
    secret = "Bearer abcdefghijklmnopqrstuvwxyz123456"
    email = "finance-owner@example.test"
    object.__setattr__(settings.sap, "base_url", "https://private-sap.example.test")
    provider = FakeModelProvider(
        [ModelResponse(text=f"Yanitta {email} ve {secret}", status="completed")]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, model_provider=provider)
    object.__setattr__(settings.agent, "direct_answers_enabled", False)

    turn = runtime.chat(f"Bu kaydi {email} icin incele; Authorization: {secret}")

    sent = json.dumps(provider.requests[0].messages, ensure_ascii=False)
    assert email not in sent
    assert secret not in sent
    assert purchaser.subject not in provider.requests[0].system_instruction
    assert "private-sap.example.test" not in provider.requests[0].system_instruction
    # Client politikasi D2 e-postayi yetkili actor'a gosterebilir; D3 secret
    # ise hicbir kosulda cikamaz. Model transcripti daha dar model sink'idir.
    assert secret not in turn.text
    persisted = json.dumps(runtime.messages, ensure_ascii=False)
    assert email not in persisted and secret not in persisted
