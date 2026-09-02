"""Basarili gorev basina maliyet, gorev sonucu ve reasoning kademelendirmesi.

Rehber Madde 2 ve Madde 12. Test edilen sozlesme:

  1. Tur sonucu deterministik ve fail-closed siniflandirilir.
  2. Maliyet **basarili gorev sayisina** bolunur; yarim kalan turlarin
     ucuzlugu iyilesme sayilmaz.
  3. Fiyat girilmemisse maliyet raporlanmaz, "0" gibi gorunmez.
  4. Reasoning seviyesi yalniz YUKARI kademelendirilir.
"""

from __future__ import annotations

from robotics_agent.config import AgentSettings, Settings
from robotics_agent.observability import (
    CostModel,
    TaskOutcome,
    TelemetryCollector,
    ToolInvocationMetric,
    TurnMetrics,
)


def make_turn(
    *,
    execution_id: str = "exec-1",
    needs_review: bool = False,
    failed: bool = False,
    output_tokens: int = 1_000,
    uncached_input_tokens: int = 10_000,
) -> TurnMetrics:
    return TurnMetrics(
        execution_id=execution_id,
        correlation_id="corr-1",
        tenant="100",
        needs_review=needs_review,
        failed=failed,
        output_tokens=output_tokens,
        uncached_input_tokens=uncached_input_tokens,
    )


def ok_call(tool: str = "sap_stock_overview", **kwargs) -> ToolInvocationMetric:
    defaults = dict(
        tool=tool, domain="planning", risk_tier="R0", outcome="ok",
        duration_ms=100.0, result_tokens=200,
    )
    defaults.update(kwargs)
    return ToolInvocationMetric(**defaults)


def denied_call(tool: str = "sap_pr_submit", code: str = "MISSING_SCOPE") -> ToolInvocationMetric:
    return ToolInvocationMetric(
        tool=tool, domain="procurement_write", risk_tier="R3", outcome="denied",
        duration_ms=5.0, result_tokens=40, denial_code=code,
    )


# --- Gorev sonucu siniflandirmasi -------------------------------------------


def test_clean_turn_is_success():
    turn = make_turn()
    turn.record_tool(ok_call())
    assert turn.classify_outcome() is TaskOutcome.SUCCESS


def test_needs_review_is_not_success():
    turn = make_turn(needs_review=True)
    turn.record_tool(ok_call())
    assert turn.classify_outcome() is TaskOutcome.NEEDS_REVIEW


def test_exception_is_error_not_needs_review():
    turn = make_turn(needs_review=True, failed=True)
    assert turn.classify_outcome() is TaskOutcome.ERROR


def test_turn_denied_end_to_end_is_denied():
    turn = make_turn()
    turn.record_tool(denied_call())
    assert turn.classify_outcome() is TaskOutcome.DENIED


def test_recovered_turn_after_denial_is_success():
    """Yetkisiz deneme sonrasi izinli tool ile isi bitirmek BASARIDIR.

    'Reddedildi' ile 'basarisiz' ayni sey degildir: ret dogru davranis
    olabilir. Turun tamami reddedilmisse basari sayilmaz.
    """
    turn = make_turn()
    turn.record_tool(denied_call())
    turn.record_tool(ok_call())
    assert turn.classify_outcome() is TaskOutcome.SUCCESS


# --- Maliyet ----------------------------------------------------------------


def test_unpriced_model_reports_no_cost():
    model = CostModel()
    assert model.priced is False
    assert model.estimate(uncached_input=1_000_000, output=1_000_000) == 0.0

    turn = make_turn()
    payload = turn.to_dict(model)
    assert payload["cost"] == {"priced": False}, "fiyat yokken maliyet 0 gibi gorunmemeli"


def test_priced_model_estimates_per_component():
    model = CostModel(
        currency="USD",
        input_per_mtok=0.30,
        output_per_mtok=2.50,
        cache_read_per_mtok=0.03,
        cache_write_per_mtok=0.375,
    )
    cost = model.estimate(
        uncached_input=1_000_000, cache_read=1_000_000, cache_write=1_000_000, output=1_000_000
    )
    assert cost == round(0.30 + 0.03 + 0.375 + 2.50, 6)


def test_cost_per_successful_task_divides_by_success_only():
    """Yarim kalan turun tokeni maliyete girer ama payda basarilar kadardir."""
    model = CostModel(input_per_mtok=1.0, output_per_mtok=1.0)
    collector = TelemetryCollector(cost_model=model)

    good = make_turn(execution_id="ok-1", uncached_input_tokens=1_000_000, output_tokens=0)
    good.record_tool(ok_call())
    collector.finish_turn(good)

    wasted = make_turn(execution_id="rev-1", needs_review=True,
                       uncached_input_tokens=1_000_000, output_tokens=0)
    collector.finish_turn(wasted)

    report = collector.effectiveness()
    assert report["successful_tasks"] == 1
    assert report["task_success_rate_pct"] == 50.0
    # Toplam 2.0 birim harcandi, 1 basarili gorev var.
    assert report["cost"]["cost_per_successful_task"] == 2.0


def test_tokens_per_successful_task_available_without_pricing():
    collector = TelemetryCollector()
    turn = make_turn(uncached_input_tokens=8_000, output_tokens=2_000)
    turn.record_tool(ok_call())
    collector.finish_turn(turn)

    report = collector.effectiveness()
    assert report["cost"]["priced"] is False
    assert report["tokens_per_successful_task"] == 10_000.0


def test_effectiveness_reports_guide_metrics():
    collector = TelemetryCollector()

    approved = make_turn(execution_id="w-1")
    approved.record_tool(ok_call("sap_pr_submit", approval_used=True, write_status="created"))
    collector.finish_turn(approved)

    duplicate = make_turn(execution_id="w-2")
    duplicate.record_tool(
        ok_call("sap_pr_submit", approval_used=True, write_status="duplicate_prevented")
    )
    collector.finish_turn(duplicate)

    blocked = make_turn(execution_id="d-1")
    blocked.record_tool(denied_call())
    collector.finish_turn(blocked)

    report = collector.effectiveness()
    assert report["turns"] == 3
    assert report["human_approval_rate_pct"] == round(2 / 3 * 100, 1)
    assert report["duplicate_write_rate_pct"] == 50.0
    assert report["unauthorized_attempt_rate_pct"] == round(1 / 3 * 100, 1)
    assert report["outcomes"]["denied"] == 1
    assert "p95_latency_s" in report


def test_empty_collector_is_safe():
    assert TelemetryCollector().effectiveness() == {"turns": 0}


# --- Reasoning kademelendirmesi ---------------------------------------------


def test_read_pack_stays_at_base_level():
    agent = AgentSettings()
    object.__setattr__(agent, "gemini_thinking_level", "low")
    assert agent.reasoning_level(("bootstrap", "procurement_read")) == "low"


def test_write_pack_escalates():
    agent = AgentSettings()
    object.__setattr__(agent, "gemini_thinking_level", "low")
    assert agent.reasoning_level(("bootstrap", "procurement_write")) == "medium"


def test_override_can_never_lower_base():
    """Taban seviye asagi cekilemez: yon bilincli olarak tek yonludur."""
    agent = AgentSettings()
    object.__setattr__(agent, "gemini_thinking_level", "high")
    object.__setattr__(agent, "reasoning_levels", (("procurement_read", "low"),))
    assert agent.reasoning_level(("bootstrap", "procurement_read")) == "high"


def test_highest_pack_wins_in_multi_pack_turn():
    agent = AgentSettings()
    object.__setattr__(agent, "gemini_thinking_level", "low")
    object.__setattr__(
        agent, "reasoning_levels", (("procurement_read", "medium"), ("p2p_finance", "high"))
    )
    level = agent.reasoning_level(("bootstrap", "procurement_read", "p2p_finance"))
    assert level == "high"


def test_invalid_override_falls_back_to_base():
    agent = AgentSettings()
    object.__setattr__(agent, "gemini_thinking_level", "low")
    object.__setattr__(agent, "reasoning_levels", (("procurement_write", "turbo"),))
    assert agent.reasoning_level(("procurement_write",)) == "low"


def test_unknown_pack_is_ignored():
    agent = AgentSettings()
    assert agent.reasoning_level(("does_not_exist",)) == agent.gemini_thinking_level


def test_default_settings_escalate_write_and_finance():
    settings = Settings()
    base = settings.agent.gemini_thinking_level
    escalated = settings.agent.reasoning_level(("bootstrap", "procurement_write"))
    assert escalated in {"medium", "high"}
    assert settings.agent.reasoning_level(("bootstrap", "master_data")) == base


def test_reasoning_tokens_are_billed_at_output_rate():
    """Muhakeme token'lari yanitta gorunmez ama cikti tarifesinden faturalanir.

    Yalniz `output_tokens` saymak maliyeti sistematik olarak dusuk gosterirdi;
    `medium` muhakemeye cikarilan yazma yollarinda fark buyuktur.
    """
    from robotics_agent.observability.telemetry import CostModel, TurnMetrics

    pricing = CostModel(input_per_mtok=0.75, output_per_mtok=3.75)
    without = TurnMetrics(
        execution_id="e1", correlation_id="c1",
        uncached_input_tokens=1_000_000, output_tokens=1_000_000,
    )
    with_thinking = TurnMetrics(
        execution_id="e2", correlation_id="c2",
        uncached_input_tokens=1_000_000, output_tokens=1_000_000,
        reasoning_tokens=1_000_000,
    )

    assert round(without.estimated_cost(pricing), 6) == 0.75 + 3.75
    assert round(with_thinking.estimated_cost(pricing), 6) == 0.75 + 3.75 + 3.75
    assert with_thinking.billed_tokens == 3_000_000
