"""Claude API tabanli SAP multi-agent orkestratoru ve domain agent dongusu."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from .config import Settings, get_settings
from .contracts import ActorContext, ExecutionContext, estimate_tokens
from .core import (
    AGENT_SPECS,
    BOOTSTRAP_PACK,
    AgentSpec,
    RoutingDecision,
    agent_catalogue,
    domains_for_packs,
    handoff_from_turn,
    plan_agents,
    route,
    schema_token_report,
    summarize_intent,
)
from .observability import TelemetryCollector
from .prompts import build_domain_prompt, prompt_version
from .sap import get_backend
from .tools import (
    ToolContext,
    anthropic_tool_definitions,
    execute_tool,
    load_all_tools,
    registry_summary,
    visible_tool_names,
)

log = logging.getLogger(__name__)

_COMPACT_MARKER = "\n... [eski tool sonucu kisaltildi. Tam sonuc gerekiyorsa tool'u yeniden cagirin.]"


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str
    is_error: bool


@dataclass
class AgentTurn:
    """Bir kullanici mesajina karsilik uretilen tam yanit."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    compacted_chars: int = 0
    iterations: int = 0
    stop_reason: str = ""
    artifacts: list[str] = field(default_factory=list)
    active_packs: list[str] = field(default_factory=list)
    schema_tokens: int = 0
    policy_denials: int = 0
    execution_id: str = ""
    correlation_id: str = ""
    needs_review: bool = False
    active_agents: list[str] = field(default_factory=list)
    agent_trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Cache'ten okunan girdi tokenlarinin orani."""
        billed = self.input_tokens + self.cache_read_tokens
        return round(self.cache_read_tokens / billed * 100, 1) if billed else 0.0


class SAPDomainAgent:
    """Sabit bir SAP domaini ve dar tool setiyle calisan agent.

    Kullanim:
        agent = SAPDomainAgent(agent_spec=AGENT_SPECS["supply_chain"])
        turn = agent.chat("HD-GEAR-CSF25-100 icin ATP ve MRP durumunu getir")
        print(turn.text)
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        system_prompt: str | None = None,
        stream: bool = False,
        use_prompt_cache: bool = True,
        keep_full_tool_results: int | None = None,
        compacted_result_chars: int = 400,
        actor: ActorContext | None = None,
        channel: str = "cli",
        telemetry: TelemetryCollector | None = None,
        two_stage_routing: bool = True,
        agent_spec: AgentSpec | None = None,
    ) -> None:
        """
        use_prompt_cache        System prompt + tool semalarini prompt cache'e alir.
                                Pack degistiginde sema prefix'i degisir ve cache
                                yeniden yazilir; bu bilincli bir odunlesmedir.
        keep_full_tool_results  Bu sayidaki en son tool sonucu baglamda tam kalir;
                                daha eskiler kisaltilir. 0 = kisaltma kapali.
        two_stage_routing       True: yalniz bootstrap + ilgili domain pack yuklenir.
                                False: tum tool'lar her turda gorunur (eski davranis).
        """
        self.settings = settings or get_settings()
        self.use_prompt_cache = use_prompt_cache
        self.keep_full_tool_results = (
            self.settings.budget.keep_full_results
            if keep_full_tool_results is None
            else keep_full_tool_results
        )
        self.compacted_result_chars = compacted_result_chars
        self.two_stage_routing = two_stage_routing
        self.agent_spec = agent_spec
        problems = self.settings.validate()
        blocking = [p for p in problems if "ANTHROPIC_API_KEY" in p]
        if blocking:
            raise RuntimeError("; ".join(blocking))
        if problems:
            for problem in problems:
                log.warning("Konfigurasyon uyarisi: %s", problem)

        load_all_tools()
        self.client = Anthropic(api_key=self.settings.agent.api_key)
        self.actor = actor or ActorContext.local_operator(
            subject=self.settings.agent.local_subject,
            tenant=self.settings.sap.tenant,
            roles=self.settings.agent.local_roles,
        )
        self.channel = channel
        self.telemetry = telemetry or TelemetryCollector()
        self.ctx = ToolContext(
            settings=self.settings,
            sap=get_backend(self.settings),
            actor=self.actor,
        )
        prompt_spec = self.agent_spec or AGENT_SPECS["platform"]
        self.system_prompt = system_prompt or build_domain_prompt(
            self.settings, spec=prompt_spec, actor=self.actor
        )
        # Model ve prompt surumu audit'e yazilir: davranis degisikligi geriye
        # donuk olarak hangi surume ait, ayirt edilebilsin.
        self.prompt_version = prompt_version() if system_prompt is None else "custom"
        self.ctx.model = self.settings.agent.model
        self.ctx.prompt_version = self.prompt_version
        self.stream = stream
        self.messages: list[dict[str, Any]] = []
        self.active_packs: list[str] = list(
            self.agent_spec.packs if self.agent_spec else (BOOTSTRAP_PACK,)
        )
        self.last_routing: RoutingDecision | None = None

    # --- Tool gorunurlugu ---------------------------------------------------
    def _visible_names(self) -> list[str]:
        if self.agent_spec is not None:
            return visible_tool_names(domains_for_packs(self.agent_spec.packs), self.actor)
        if not self.two_stage_routing:
            return [name for name in visible_tool_names(_ALL_DOMAINS, self.actor)]
        domains = domains_for_packs(self.active_packs)
        return visible_tool_names(domains, self.actor)

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Bu turda modele gosterilecek sema listesi."""
        return anthropic_tool_definitions(self._visible_names())

    def _apply_routing(self, message: str) -> RoutingDecision:
        decision = route(message, self.actor)
        self.active_packs = list(decision.packs)
        self.last_routing = decision
        log.info(
            "router | packs=%s | fallback=%s | intent=%r",
            ",".join(decision.packs),
            decision.fallback,
            summarize_intent(message),
        )
        return decision

    def _absorb_pack_request(self) -> bool:
        """Eski tek-agent modunda istenen pack'leri aktive eder."""
        if self.agent_spec is not None:
            return False
        requested = getattr(self.ctx, "requested_packs", None)
        if not requested:
            return False
        added = [p for p in requested if p not in self.active_packs]
        if added:
            self.active_packs.extend(added)
        self.ctx.requested_packs = None
        return bool(added)

    # --- Ic yardimcilar -----------------------------------------------------
    def _cached_system(self) -> list[dict[str, Any]] | str:
        """System prompt'u prompt cache'e alinabilir blok olarak dondurur."""
        if not self.use_prompt_cache:
            return self.system_prompt
        return [
            {
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _cached_tools(self, definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Son tool tanimina cache isareti koyar; oncesindeki tum tanimlar da cache'lenir."""
        if not self.use_prompt_cache or not definitions:
            return definitions
        marked = [dict(t) for t in definitions]
        marked[-1]["cache_control"] = {"type": "ephemeral"}
        return marked

    def _create_message(
        self,
        definitions: list[dict[str, Any]],
        on_text: Callable[[str], None] | None,
        *,
        final_answer: bool = False,
    ):
        # Uzun ayrintiyi sohbete degil artifact/rapora tasimak icin nihai
        # turdaki max_tokens degeri yanit butcesiyle sinirlanir.
        max_tokens = (
            min(self.settings.agent.max_tokens, self.settings.budget.answer_tokens * 2)
            if final_answer
            else self.settings.agent.max_tokens
        )
        kwargs: dict[str, Any] = {
            "model": self.settings.agent.model,
            "max_tokens": max_tokens,
            "temperature": self.settings.agent.temperature,
            "system": self._cached_system(),
            "tools": self._cached_tools(definitions),
            "messages": self.messages,
        }
        if self.stream and on_text is not None:
            with self.client.messages.stream(**kwargs) as stream:
                for chunk in stream.text_stream:
                    on_text(chunk)
                return stream.get_final_message()
        return self.client.messages.create(**kwargs)

    def _compact_history(self) -> int:
        """Eski tool sonuclarini kisaltir; yenileri tam kalir.

        Uzun oturumlarda tool ciktilari baglamda birikir. Son N tool sonucu aynen
        korunur, daha eskiler bas kismi + kisaltma notu ile degistirilir. Model
        eski bir sonucun tamamina ihtiyac duyarsa tool'u yeniden cagirabilir.
        Kirpilan karakter sayisini dondurur.
        """
        if self.keep_full_tool_results <= 0:
            return 0

        blocks = [
            block
            for message in self.messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        stale = blocks[: max(0, len(blocks) - self.keep_full_tool_results)]

        trimmed = 0
        for block in stale:
            content = block.get("content")
            if not isinstance(content, str) or content.endswith(_COMPACT_MARKER):
                continue
            if len(content) <= self.compacted_result_chars:
                continue
            original = len(content)
            block["content"] = content[: self.compacted_result_chars] + _COMPACT_MARKER
            trimmed += original - len(block["content"])
        return trimmed

    def _iteration_limit(self) -> int:
        """Use-case bazli tool adimi siniri.

        Global 25 yerine aktif domain'e gore sinir uygulanir: bir satinalma
        akisinin serbestce 25 tool cagirmasi istenmez.
        """
        domain_packs = [p for p in self.active_packs if p != BOOTSTRAP_PACK]
        limits = [self.settings.agent.iteration_limit(p) for p in domain_packs]
        return max(limits) if limits else self.settings.agent.max_tool_iterations

    # --- Genel API ----------------------------------------------------------
    def chat(
        self,
        user_message: str,
        *,
        on_text: Callable[[str], None] | None = None,
        on_tool_start: Callable[[str, dict], None] | None = None,
        on_tool_end: Callable[[str, bool], None] | None = None,
    ) -> AgentTurn:
        """Kullanici mesajini isler, gerekli toollari calistirir ve nihai yaniti dondurur."""
        execution = ExecutionContext(
            actor=self.actor,
            system_alias=self.settings.sap.system_alias,
            channel=self.channel,
            dry_run=self.settings.sap.dry_run,
        )
        self.ctx.execution = execution
        metrics = self.telemetry.start_turn(
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            tenant=self.actor.tenant,
            channel=self.channel,
        )
        self.ctx.metrics = metrics

        if self.two_stage_routing and self.agent_spec is None:
            self._apply_routing(user_message)

        self.messages.append({"role": "user", "content": user_message})
        turn = AgentTurn(
            text="",
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
        )
        artifacts_before = len(self.ctx.artifacts)
        max_iterations = self._iteration_limit()

        budget_exhausted = False

        for iteration in range(1, max_iterations + 1):
            turn.iterations = iteration
            definitions = self.tools
            turn.schema_tokens = sum(estimate_tokens(d) for d in definitions)
            metrics.schema_tokens = turn.schema_tokens
            metrics.iterations = iteration
            metrics.active_packs = tuple(self.active_packs)

            # Tur sonuc butcesi dolduysa yeni tool cagrisina izin verilmez;
            # model eksik context ile mutating akisa devam etmemelidir.
            allow_tools = (
                metrics.tool_result_tokens < self.settings.budget.turn_result_tokens
            )
            response = self._create_message(
                definitions if allow_tools else [],
                on_text,
                final_answer=not allow_tools,
            )
            if not allow_tools and not budget_exhausted:
                budget_exhausted = True
                log.warning(
                    "turn sonuc butcesi doldu (%d/%d token); tool cagrilari kapatildi",
                    metrics.tool_result_tokens,
                    self.settings.budget.turn_result_tokens,
                )

            turn.input_tokens += response.usage.input_tokens
            turn.output_tokens += response.usage.output_tokens
            turn.cache_read_tokens += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            turn.cache_write_tokens += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            turn.stop_reason = response.stop_reason or ""
            metrics.uncached_input_tokens += response.usage.input_tokens
            metrics.cache_read_tokens += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            metrics.cache_write_tokens += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            metrics.output_tokens += response.usage.output_tokens

            assistant_content = [block.model_dump() for block in response.content]
            self.messages.append({"role": "assistant", "content": assistant_content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            if text_parts:
                turn.text = "\n".join(text_parts).strip()

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break

            tool_results: list[dict[str, Any]] = []
            for block in tool_uses:
                arguments = dict(block.input or {})
                if on_tool_start:
                    on_tool_start(block.name, arguments)

                payload, is_error = execute_tool(block.name, arguments, self.ctx)

                if on_tool_end:
                    on_tool_end(block.name, is_error)

                turn.tool_calls.append(
                    ToolCall(name=block.name, arguments=arguments, result=payload, is_error=is_error)
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": payload,
                        "is_error": is_error,
                    }
                )
                if '"needs_review":true' in payload:
                    turn.needs_review = True
                    metrics.needs_review = True

            self.messages.append({"role": "user", "content": tool_results})
            turn.compacted_chars += self._compact_history()
            metrics.compacted_chars = turn.compacted_chars
        else:
            turn.text += (
                f"\n\n[Uyari: {max_iterations} tool adimi limitine ulasildi. "
                "Islem yarim kalmis olabilir; sap_get_execution_audit ile son durumu kontrol edin "
                "veya soruyu daha kucuk parcalara bolun.]"
            )
            turn.needs_review = True
            metrics.needs_review = True

        if budget_exhausted:
            turn.text += (
                f"\n\n[Uyari: bu turda tool sonuclari icin ayrilan "
                f"{self.settings.budget.turn_result_tokens:,} token butcesi doldu. "
                "Yanit eldeki verilerle uretildi; devam eden bir islem varsa "
                "sap_get_execution_audit ile durumunu dogrulayin.]"
            )
            turn.needs_review = True
            metrics.needs_review = True

        turn.artifacts = self.ctx.artifacts[artifacts_before:]
        turn.active_packs = list(self.active_packs)
        turn.policy_denials = metrics.policy_denials
        self.telemetry.finish_turn(metrics)
        self.ctx.metrics = None
        return turn

    def reset(self) -> None:
        """Domain agent konusma gecmisini temizler."""
        self.messages.clear()
        self.active_packs = list(
            self.agent_spec.packs if self.agent_spec else (BOOTSTRAP_PACK,)
        )
        self.last_routing = None

    def describe_tools(self) -> list[dict[str, Any]]:
        return registry_summary()

    def health(self) -> dict[str, Any]:
        return {
            "model": self.settings.agent.model,
            "agent": self.agent_spec.key if self.agent_spec else "legacy-router",
            "visible_tool_count": len(self._visible_names()),
            "registered_tool_count": len(registry_summary()),
            "active_packs": list(self.active_packs),
            "sap": self.ctx.sap.ping(),
            "dry_run": self.settings.sap.dry_run,
            "output_dir": str(self.settings.output_dir),
            "prompt_cache": self.use_prompt_cache,
            "two_stage_routing": self.two_stage_routing,
            "keep_full_tool_results": self.keep_full_tool_results,
            "actor": self.actor.to_dict(include_scopes=True),
        }

    def budget_report(self) -> dict[str, Any]:
        """Aktif sema butcesi ve son turlarin telemetrisi."""
        return {
            "schema": schema_token_report(
                self.tools, budget=self.settings.budget.schema_tokens_per_turn
            ),
            "telemetry": self.telemetry.snapshot(),
            "limits": {
                "single_result_tokens": self.settings.budget.single_result_tokens,
                "turn_result_tokens": self.settings.budget.turn_result_tokens,
                "iteration_limit": self._iteration_limit(),
            },
        }

    def context_size(self) -> dict[str, int]:
        """Konusma gecmisinin yaklasik token maliyeti (1 token ~ 4 karakter)."""
        import json as _json

        history = len(_json.dumps(self.messages, ensure_ascii=False, default=str))
        fixed = len(self.system_prompt) + len(_json.dumps(self.tools, ensure_ascii=False))
        return {
            "history_tokens": history // 4,
            "fixed_tokens": fixed // 4,
            "total_tokens": (history + fixed) // 4,
            "message_count": len(self.messages),
        }


# Tum SAP domain'leri: yalniz geriye donuk tek-agent modu icin.
_ALL_DOMAINS = frozenset(
    {
        "platform",
        "diagnostics",
        "master_data",
        "planning",
        "procurement_read",
        "procurement_write",
        "project_finance",
        "reporting",
    }
)


class SAPMultiAgent:
    """Kural tabanli orkestrator ve izole SAP domain agent'lari.

    Her kullanici turu once ``plan_agents`` ile planlanir. Secilen agent'lar
    sirayla calisir; yalniz ``sap-agent-handoff/v1`` zarfi sonraki agent'a
    aktarilir. Policy, approval, idempotency, audit ve evidence depolari tum
    agent'lar icin ortaktir ve LLM tarafindan degistirilemez.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        stream: bool = False,
        use_prompt_cache: bool = True,
        keep_full_tool_results: int | None = None,
        compacted_result_chars: int = 400,
        actor: ActorContext | None = None,
        channel: str = "cli",
        telemetry: TelemetryCollector | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.actor = actor or ActorContext.local_operator(
            subject=self.settings.agent.local_subject,
            tenant=self.settings.sap.tenant,
            roles=self.settings.agent.local_roles,
        )
        self.channel = channel
        self.telemetry = telemetry or TelemetryCollector()
        self.stream = stream
        self.use_prompt_cache = use_prompt_cache
        self.keep_full_tool_results = keep_full_tool_results
        self.compacted_result_chars = compacted_result_chars
        self.messages: list[dict[str, Any]] = []
        self.active_packs: list[str] = [BOOTSTRAP_PACK]
        self.active_agents: list[str] = ["platform"]
        self.last_plan = None
        self._domain_agents: dict[str, SAPDomainAgent] = {}
        # Platform agent'i ayni zamanda orkestrator audit/evidence baglamini tasir.
        self._platform = self._get_agent("platform")
        self.ctx = self._platform.ctx

    def _get_agent(self, key: str) -> SAPDomainAgent:
        agent = self._domain_agents.get(key)
        if agent is None:
            agent = SAPDomainAgent(
                self.settings,
                stream=self.stream,
                use_prompt_cache=self.use_prompt_cache,
                keep_full_tool_results=self.keep_full_tool_results,
                compacted_result_chars=self.compacted_result_chars,
                actor=self.actor,
                channel=f"{self.channel}:{key}",
                telemetry=self.telemetry,
                agent_spec=AGENT_SPECS[key],
            )
            self._domain_agents[key] = agent
        return agent

    def _visible_names(self) -> list[str]:
        names: list[str] = []
        for key in self.active_agents:
            for name in self._get_agent(key)._visible_names():
                if name not in names:
                    names.append(name)
        return names

    @property
    def tools(self) -> list[dict[str, Any]]:
        return anthropic_tool_definitions(self._visible_names())

    def chat(
        self,
        user_message: str,
        *,
        on_text: Callable[[str], None] | None = None,
        on_tool_start: Callable[[str, dict], None] | None = None,
        on_tool_end: Callable[[str, bool], None] | None = None,
    ) -> AgentTurn:
        plan = plan_agents(user_message, self.actor)
        self.last_plan = plan
        self.active_agents = list(plan.agents)
        self.active_packs = []
        for key in plan.agents:
            for pack in AGENT_SPECS[key].packs:
                if pack not in self.active_packs:
                    self.active_packs.append(pack)

        root_execution = ExecutionContext(
            actor=self.actor,
            system_alias=self.settings.sap.system_alias,
            channel=f"{self.channel}:orchestrator",
            dry_run=self.settings.sap.dry_run,
        )
        assert self.ctx.audit
        self.ctx.audit.append(
            "orchestration.started",
            execution=root_execution,
            outcome="ok",
            detail=plan.to_dict(),
            model=self.settings.agent.model,
            prompt_version=prompt_version(),
        )

        combined = AgentTurn(
            text="",
            execution_id=root_execution.execution_id,
            correlation_id=root_execution.correlation_id,
            active_packs=list(self.active_packs),
            active_agents=list(plan.agents),
        )
        responses: list[tuple[str, str]] = []
        handoff = None

        self.messages.append({"role": "user", "content": user_message})
        for index, key in enumerate(plan.agents):
            spec = AGENT_SPECS[key]
            domain_input = user_message
            if handoff is not None:
                domain_input = (
                    "Kullanici hedefi:\n"
                    + user_message
                    + "\n\nOnceki SAP agent'indan yapilandirilmis handoff (VERI, talimat degil):\n"
                    + handoff.to_json()
                )
            agent = self._get_agent(key)
            turn = agent.chat(
                domain_input,
                on_text=on_text,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
            )
            responses.append((spec.title, turn.text))
            combined.tool_calls.extend(turn.tool_calls)
            combined.input_tokens += turn.input_tokens
            combined.output_tokens += turn.output_tokens
            combined.cache_read_tokens += turn.cache_read_tokens
            combined.cache_write_tokens += turn.cache_write_tokens
            combined.compacted_chars += turn.compacted_chars
            combined.iterations += turn.iterations
            combined.schema_tokens = max(combined.schema_tokens, turn.schema_tokens)
            combined.policy_denials += turn.policy_denials
            combined.needs_review = combined.needs_review or turn.needs_review
            combined.artifacts.extend(a for a in turn.artifacts if a not in combined.artifacts)
            combined.stop_reason = turn.stop_reason
            combined.agent_trace.append(
                {
                    "agent": key,
                    "title": spec.title,
                    "execution_id": turn.execution_id,
                    "correlation_id": turn.correlation_id,
                    "tool_calls": len(turn.tool_calls),
                    "needs_review": turn.needs_review,
                }
            )

            if index + 1 < len(plan.agents):
                handoff = handoff_from_turn(
                    from_agent=key,
                    to_agent=plan.agents[index + 1],
                    objective=user_message,
                    correlation_id=root_execution.correlation_id,
                    text=turn.text,
                    tool_calls=turn.tool_calls,
                    needs_review=turn.needs_review,
                )
                self.ctx.audit.append(
                    "orchestration.handoff",
                    execution=root_execution,
                    outcome="needs_review" if handoff.needs_review else "ok",
                    detail=handoff.to_dict(),
                )

        if len(responses) == 1:
            combined.text = responses[0][1]
        else:
            combined.text = "\n\n".join(
                f"## {title}\n\n{text}" for title, text in responses
            )
        self.messages.append({"role": "assistant", "content": combined.text})
        self.ctx.audit.append(
            "orchestration.completed",
            execution=root_execution,
            outcome="needs_review" if combined.needs_review else "ok",
            detail={
                "agents": list(plan.agents),
                "tool_calls": len(combined.tool_calls),
                "policy_denials": combined.policy_denials,
            },
            model=self.settings.agent.model,
            prompt_version=prompt_version(),
        )
        return combined

    def reset(self) -> None:
        self.messages.clear()
        for agent in self._domain_agents.values():
            agent.reset()
        self.active_agents = ["platform"]
        self.active_packs = [BOOTSTRAP_PACK]
        self.last_plan = None

    def describe_tools(self) -> list[dict[str, Any]]:
        return registry_summary()

    def health(self) -> dict[str, Any]:
        return {
            "architecture": "certaops",
            "model": self.settings.agent.model,
            "registered_agents": len(AGENT_SPECS),
            "agents": agent_catalogue(),
            "active_agents": list(self.active_agents),
            "active_packs": list(self.active_packs),
            "visible_tool_count": len(self._visible_names()),
            "registered_tool_count": len(registry_summary()),
            "sap": self.ctx.sap.ping(),
            "dry_run": self.settings.sap.dry_run,
            "output_dir": str(self.settings.output_dir),
            "actor": self.actor.to_dict(include_scopes=True),
        }

    def budget_report(self) -> dict[str, Any]:
        return {
            "architecture": "certaops",
            "active_agents": list(self.active_agents),
            "schema": schema_token_report(
                self.tools, budget=self.settings.budget.schema_tokens_per_turn
            ),
            "telemetry": self.telemetry.snapshot(),
        }

    def context_size(self) -> dict[str, int]:
        import json as _json

        history = len(_json.dumps(self.messages, ensure_ascii=False, default=str))
        return {
            "history_tokens": history // 4,
            "fixed_tokens": sum(
                self._get_agent(key).context_size()["fixed_tokens"]
                for key in self.active_agents
            ),
            "total_tokens": history // 4
            + sum(
                self._get_agent(key).context_size()["fixed_tokens"]
                for key in self.active_agents
            ),
            "message_count": len(self.messages),
        }
