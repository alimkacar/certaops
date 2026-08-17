"""Provider-neutral model contracts.

The rest of the agent runtime depends on these small value objects instead of
provider SDK response classes.  Provider-specific state (for example Gemini
thought signatures) deliberately does not cross this boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class FunctionDeclaration:
    """A JSON-schema function exposed to a model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the provider-neutral declaration used by runtime budgets."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(frozen=True)
class FunctionCall:
    """A provider-neutral request to execute one local function."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Compatibility alias for SDKs that call this field ``id``."""
        return self.call_id


@dataclass(frozen=True)
class FunctionResult:
    """The result of a manually executed function call."""

    call_id: str
    name: str
    result: Any
    is_error: bool = False


@dataclass(frozen=True)
class TokenUsage:
    """Normalised token counters; unavailable counters remain zero."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thought_tokens: int = 0
    tool_use_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cached_input_tokens(self) -> int:
        """Google-compatible name for tokens read from a provider cache."""
        return self.cache_read_tokens

    @property
    def cache_read_input_tokens(self) -> int:
        """Anthropic-compatible alias used by legacy accounting code."""
        return self.cache_read_tokens

    @property
    def cache_creation_input_tokens(self) -> int:
        """Anthropic-compatible alias used by legacy accounting code."""
        return self.cache_write_tokens


@dataclass(frozen=True)
class ModelRequest:
    """One provider invocation.

    ``new_turn`` starts a new user turn.  Calls with ``new_turn=False`` may
    continue the provider's short-lived tool loop by supplying
    ``function_results``.  Long-lived conversation history belongs to the
    agent runtime and is supplied again in ``messages`` on each new turn.
    """

    new_turn: bool = True
    messages: tuple[Any, ...] = ()
    function_results: tuple[FunctionResult, ...] = ()
    tools: tuple[FunctionDeclaration, ...] = ()
    system_instruction: str = ""
    model: str = ""
    max_output_tokens: int = 0
    thinking_level: str = ""
    stream: bool = False


@dataclass(frozen=True)
class ModelResponse:
    """Provider-neutral text, function calls, and accounting metadata."""

    text: str = ""
    function_calls: tuple[FunctionCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    status: str = ""
    stop_reason: str = ""
    provider: str = ""
    model: str = ""
    backend: str = ""

    @property
    def needs_action(self) -> bool:
        return bool(self.function_calls)


TextCallback = Callable[[str], None]


@runtime_checkable
class ModelProvider(Protocol):
    """Minimal interface implemented by every model provider."""

    provider_name: str

    def generate(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse: ...

    def complete(
        self, request: ModelRequest, *, on_text: TextCallback | None = None
    ) -> ModelResponse: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class ModelProviderError(RuntimeError):
    """A sanitised provider failure safe to expose to the runtime."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable


class ModelConfigurationError(ModelProviderError):
    """Invalid provider configuration detected before a remote request."""


class ModelProtocolError(ModelProviderError):
    """A provider response did not satisfy the neutral contract."""


def resolve_agent_settings(settings: Any) -> Any:
    """Accept either the root ``Settings`` object or ``AgentSettings`` itself."""
    return getattr(settings, "agent", settings)
