"""Anthropic adapterinin tool secimi sozlesmesi."""

from dataclasses import replace
from types import SimpleNamespace

from certaops.providers import FunctionDeclaration, ModelMessage, ModelRequest
from certaops.providers.anthropic import AnthropicProvider


class _Messages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


class _Client:
    def __init__(self):
        self.messages = _Messages()


REQUEST = ModelRequest(
    system="sistem",
    messages=(ModelMessage(role="user", text="stok"),),
    functions=(
        FunctionDeclaration(
            name="sap_stock_overview",
            description="Stok",
            parameters={"type": "object", "properties": {}},
        ),
    ),
)


def test_required_tool_choice_any_olarak_gonderilir():
    client = _Client()
    provider = AnthropicProvider(client=client)

    provider.generate(replace(REQUEST, tool_choice="required"))

    sent = client.messages.calls[0]
    assert sent["tool_choice"] == {"type": "any"}
    assert sent["tools"][0]["name"] == "sap_stock_overview"


def test_none_tool_choice_declarationlari_gondermez():
    client = _Client()
    provider = AnthropicProvider(client=client)

    provider.generate(replace(REQUEST, tool_choice="none"))

    sent = client.messages.calls[0]
    assert "tools" not in sent
    assert "tool_choice" not in sent
