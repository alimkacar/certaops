"""Provider egress ve terminal durumlari icin runtime guvenlik testleri."""

from __future__ import annotations

import json

import pytest

from certaops.providers import FakeModelProvider, FunctionCall, ModelResponse
from certaops.runtime import SAPAgentRuntime


def _disable_direct_answers(settings) -> None:
    object.__setattr__(settings.agent, "direct_answers_enabled", False)


def test_persisted_history_is_dlp_cleaned_again_at_provider_egress(settings, purchaser) -> None:
    secret = "Bearer abcdefghijklmnopqrstuvwxyz123456"
    iban = "TR330006100519786457841326"
    provider = FakeModelProvider([ModelResponse(text="Tamam.", status="completed")])
    runtime = SAPAgentRuntime(settings, actor=purchaser, provider=provider)
    _disable_direct_answers(settings)
    # Eski veya disaridan yuklenmis oturum kaydi daha once DLP'den gecmemis
    # olabilir. Provider siniri bu kayda guvenmemelidir.
    runtime.messages = [
        {"role": "user", "content": f"Onceki secret: {secret}"},
        {
            "role": "assistant",
            "content": {
                "type": "text",
                "text": f"Onceki IBAN: {iban}",
                "authorization": "hunter2",
            },
        },
    ]

    runtime.chat("Onceki konusmayi guvenli bicimde ozetle")

    egress = json.dumps(provider.requests[0].messages, ensure_ascii=False, default=str)
    assert secret not in egress
    assert iban not in egress
    assert "hunter2" not in egress


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete"])
def test_unsuccessful_provider_status_never_executes_partial_function_calls(
    status: str, settings, purchaser
) -> None:
    provider = FakeModelProvider(
        [
            ModelResponse(
                text="Kismi provider yaniti.",
                function_calls=(FunctionCall("partial-1", "sap_connection_health", {}),),
                status=status,
            )
        ]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, provider=provider)
    _disable_direct_answers(settings)

    turn = runtime.chat("Baglanti sagligini kontrol et")

    assert provider.call_count == 1
    assert turn.tool_calls == []
    assert turn.needs_review is True
    assert turn.stop_reason == status
    assert "tamamlanmis sayilmadi" in turn.text


def test_text_callback_receives_only_the_final_client_safe_text(settings, purchaser) -> None:
    secret = "Bearer abcdefghijklmnopqrstuvwxyz123456"
    provider = FakeModelProvider(
        [ModelResponse(text=f"Ham provider metni: {secret}", status="completed")]
    )
    runtime = SAPAgentRuntime(
        settings,
        actor=purchaser,
        provider=provider,
        stream=True,
    )
    _disable_direct_answers(settings)
    fragments: list[str] = []

    turn = runtime.chat("Guvenli bir yanit ver", on_text=fragments.append)

    assert provider.requests[0].stream is False
    assert fragments == [turn.text]
    assert secret not in turn.text
    assert secret not in "".join(fragments)


def test_max_tokens_truncation_never_executes_partial_function_calls(
    settings, purchaser
) -> None:
    """Token siniri yuzunden kesilen yanit `status=completed` ile gelir.

    Yalniz `status`a bakan bir kontrol bunu kacirir; argumanlar yarim
    kalmis olabilecegi icin hicbir cagri calistirilmamalidir.
    """
    provider = FakeModelProvider(
        [
            ModelResponse(
                text="Kesilmis yanit.",
                function_calls=(FunctionCall("trunc-1", "sap_connection_health", {}),),
                status="completed",
                stop_reason="max_tokens",
            )
        ]
    )
    runtime = SAPAgentRuntime(settings, actor=purchaser, provider=provider)
    _disable_direct_answers(settings)

    turn = runtime.chat("Baglanti sagligini kontrol et")

    assert turn.tool_calls == []
    assert turn.needs_review is True
    assert turn.stop_reason == "max_tokens"
    assert "tamamlanmis sayilmadi" in turn.text
