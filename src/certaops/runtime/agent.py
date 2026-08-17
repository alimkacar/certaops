"""Tek SAP agent runtime'i: bir kullanici turu = bir model dongusu.

Mimari
------
    kanal -> kimlik/ActorContext -> deterministik PackRouter -> SAPAgentRuntime
          -> ModelProvider -> guvenli Tool Registry
          -> Policy / Risk / Approval / DLP -> SAP backend

Neden tek runtime
-----------------
Onceki tasarimda her SAP domaini ayri bir agent nesnesiydi ve cok domainli
bir istek ayri ayri model turlari calistirip sonuclari model disinda
birlestiriyordu. Bu, ayni isi yapmak icin N kat model maliyeti demekti; ustelik
agent'lar arasi ``HandoffEnvelope`` yolu, korunmasi gereken ayri bir guvenlik
yuzeyi uretiyordu.

Domain ayrimi kaybolmadi - **calisma zamani nesnesi olmaktan cikip veriye
donustu** (bkz. ``profiles``). Yetkili pack'lerin birlesimi hesaplanir, tek
prompt kurulur, tek dongu calisir.

Guvenlik degismezleri
---------------------
1. Model tool CALISTIRAMAZ. Yalnizca oneri uretir; her oneri
   ``execute_tool`` uzerinden policy, RBAC/ABAC, risk skoru, onay,
   idempotency, DLP, audit ve butce katmanlarindan gecer.
2. Modele yalnizca PackRouter'in secip ActorContext'in yetkilendirdigi tool
   declaration'lari gonderilir.
3. Modelin urettigi gorunmeyen/yetkisiz/bilinmeyen tool adi **fail-closed**
   reddedilir; handler cagrilmaz.
4. Ayni ``call_id`` ikinci kez gelirse tool TEKRAR CALISTIRILMAZ; onceki
   guvenli sonuc kullanilir.
5. Mutating tool hicbir kosulda otomatik tekrarlanmaz.
6. Saglayici timeout/retry'i yalnizca saglayici cagrisini kapsar; o noktada
   SAP tarafinda calistirilmis bir tool tekrarlanmaz.

Paralellik hakkinda bilincli karar
----------------------------------
Bir turda birden fazla bagimsiz R0 okumasi paralellestirilebilir ve bu
gecikmeyi dusururdu. **Su an KAPALI**, cunku paylasilan `ToolContext`
(cache, evidence store, sap_call_count sayaci, telemetri) thread-safe
oldugunu kanitlayan bir test yok. Olcusuz bir hizlanma icin veri yarisi
riskini almak, bu projenin guvenlik duruşuyla celisir.

Acmak icin gereken sira: (1) `ToolContext` alanlarinin es zamanli erisimini
kapsayan testler, (2) yalniz `risk_tier == R0` ve `idempotent` okumalar,
(3) sinirli havuz (bounded concurrency), (4) mutating tool HER ZAMAN
sirali.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from robotics_agent.config import Settings, get_settings
from robotics_agent.contracts import ActorContext, ExecutionContext, estimate_tokens
from robotics_agent.core import (
    RoutingDecision,
    direct_answer_for,
    domains_for_packs,
    match_shortcut,
    route,
    summarize_intent,
)
from robotics_agent.observability import TelemetryCollector
from robotics_agent.privacy import is_secret_field, sanitize_for_client, sanitize_text
from robotics_agent.privacy.output import REDACTED as SECRET_REDACTED
from robotics_agent.prompts import build_runtime_prompt, prompt_version
from robotics_agent.sap import get_backend
from robotics_agent.tools import (
    ToolContext,
    execute_tool,
    load_all_tools,
    registry_summary,
    visible_tool_names,
)
from robotics_agent.tools.registry import REGISTRY

from ..providers import (
    FunctionCall,
    FunctionResult,
    ModelMessage,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    TokenUsage,
    build_provider,
)
from .profiles import DOMAIN_PROFILES, iteration_budget_for, profiles_for_packs

log = logging.getLogger(__name__)

__all__ = ["AgentTurn", "SAPAgentRuntime", "ToolCall"]

_COMPACT_MARKER = (
    "\n... [eski tool sonucu kisaltildi. Tam sonuc gerekiyorsa tool'u yeniden cagirin.]"
)

#: Modelin gorunmeyen bir tool adi uretmesi durumunda donen govde.
#: Metin bilerek "hangi tool'lar var" bilgisi vermez: yetkisiz bir actor'a
#: yetki envanteri sizdirmak istemeyiz.
_UNKNOWN_TOOL = {
    "error": "Bu tool bu oturumda kullanilabilir degil.",
    "denial_code": "TOOL_NOT_AVAILABLE",
    "remediation": "Yalnizca sana verilen tool listesindeki araclari cagir.",
}


@dataclass
class ToolCall:
    """Bir turda calistirilan tool (kanal ciktisi ve handoff icin)."""

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
    reasoning_tokens: int = 0
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
    #: Bu turda acilan SAP domainleri (eski `active_agents` alaninin karsiligi).
    active_domains: list[str] = field(default_factory=list)
    domain_trace: list[dict[str, Any]] = field(default_factory=list)
    direct_answer: bool = False
    direct_answer_reason: str = ""
    #: Kac provider cagrisi yapildi. 0 = SAP verisi model saglayicisina gitmedi.
    model_calls: int = 0
    provider: str = ""
    model: str = ""
    thinking_level: str = ""

    # --- Geriye donuk uyumluluk --------------------------------------------
    @property
    def active_agents(self) -> list[str]:
        """Deprecated: ayri agent'lar yok; acik domainleri dondurur."""
        return list(self.active_domains)

    @property
    def agent_trace(self) -> list[dict[str, Any]]:
        """Deprecated: `domain_trace` kullanin."""
        return list(self.domain_trace)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        billed = self.input_tokens + self.cache_read_tokens
        return round(self.cache_read_tokens / billed * 100, 1) if billed else 0.0


def _args_fingerprint(name: str, arguments: dict[str, Any]) -> str:
    blob = json.dumps({"t": name, "a": arguments}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


#: Saglayici cagrisinin saglikli bittigini gosteren durumlar.
#: `requires_action` modelin tool cagirmak ISTEDIGINI soyler; bu normal
#: akistir. Bunu "tamamlanmadi" saymak butun tool cagrilarini sessizce
#: dusururdu.
HEALTHY_PROVIDER_STATUSES = frozenset({"", "completed", "requires_action"})

#: Acikca bozuk oldugu bilinen durumlar.
BROKEN_PROVIDER_STATUSES = frozenset({"failed", "cancelled", "incomplete"})


def _supports_streaming(provider: Any) -> bool:
    """Saglayici `generate(..., on_text=...)` kabul ediyor mu?

    Streaming opsiyoneldir. Desteklenmiyorsa istek `stream=False` gider ve
    metin tek seferde, temizlendikten SONRA iletilir.
    """
    try:
        return "on_text" in inspect.signature(provider.generate).parameters
    except (TypeError, ValueError):  # pragma: no cover - imzasi okunamayan saglayici
        return False


def _scrub(value: Any, *, actor: ActorContext, settings: Any, dlp: Any) -> Any:
    """Bir yapinin metin yapraklarini model sink'ine gore temizler.

    Iki ayri kural isler:
      * **Desen**: metnin kendisi bir sir gibi goruniyorsa (Bearer, IBAN, ...)
        `sanitize_text` yakalar.
      * **Alan adi**: `authorization: hunter2` gibi degerler desene uymaz ama
        anahtari yuzunden sirdir; bunlar kosulsuz gizlenir.
    """
    if isinstance(value, str):
        return sanitize_text(value, actor=actor, sink="model", settings=settings, dlp=dlp)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and is_secret_field(key):
                out[key] = SECRET_REDACTED
            else:
                out[key] = _scrub(item, actor=actor, settings=settings, dlp=dlp)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, actor=actor, settings=settings, dlp=dlp) for v in value)
    return value


class SAPAgentRuntime:
    """Tum SAP domainlerini tek model dongusuyle calistiran runtime.

    Kullanim::

        runtime = SAPAgentRuntime()
        turn = runtime.chat("HD-GEAR-CSF25-100 icin ATP ve proje etkisi")
        print(turn.text)
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: ModelProvider | None = None,
        actor: ActorContext | None = None,
        channel: str = "cli",
        telemetry: TelemetryCollector | None = None,
        system_prompt: str | None = None,
        keep_full_tool_results: int | None = None,
        compacted_result_chars: int = 400,
        stream: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        problems = self.settings.validate()
        for problem in problems:
            log.warning("Konfigurasyon uyarisi: %s", problem)

        load_all_tools()
        self.provider: ModelProvider = provider or build_provider(self.settings)
        self.actor = actor or ActorContext.local_operator(
            subject=self.settings.agent.local_subject,
            tenant=self.settings.sap.tenant,
            roles=self.settings.agent.local_roles,
        )
        self.channel = channel
        self.telemetry = telemetry or TelemetryCollector()
        self.ctx = ToolContext(
            settings=self.settings, sap=get_backend(self.settings), actor=self.actor
        )
        self._custom_prompt = system_prompt
        self.prompt_version = prompt_version() if system_prompt is None else "custom"
        self.ctx.model = f"{self.provider.name}:{self.provider.model}"
        self.ctx.prompt_version = self.prompt_version
        self.keep_full_tool_results = (
            self.settings.budget.keep_full_results
            if keep_full_tool_results is None
            else keep_full_tool_results
        )
        self.compacted_result_chars = compacted_result_chars
        # Streaming yalniz saglayici `on_text` kabul ediyorsa istenir. Aksi
        # halde istek stream=False gider: yarim metin parcalari uretmektense
        # tek seferde temizlenmis metni vermek tercih edilir.
        self.stream = bool(stream) and _supports_streaming(self.provider)

        self.messages: list[ModelMessage] = []
        self.active_packs: list[str] = ["bootstrap"]
        self.last_routing: RoutingDecision | None = None

    # --- Gorunurluk ---------------------------------------------------------
    def visible_tools(self) -> list[str]:
        """Bu turda modele gosterilecek tool adlari.

        Iki filtre birlikte uygulanir: PackRouter'in sectigi domainler VE
        actor'un sahip oldugu kapsamlar. Yetkisi olmayan bir mutating tool
        modele **hic gosterilmez** - hem token hem saldiri yuzeyi azalir.
        """
        return visible_tool_names(domains_for_packs(self.active_packs), self.actor)

    def _declarations(self, names: list[str]):
        return [REGISTRY[n].to_function_declaration() for n in names if n in REGISTRY]

    def _thinking_level(self, *, iteration: int, domain_count: int) -> str:
        """Istegin karmasikligina gore muhakeme butcesi.

        Uc girdi birleşir, hepsi yalniz YUKSELTIR:
          1. acik pack override'lari (`AGENT_REASONING_LEVELS`),
          2. akisin karmasikligi (cok domain / cok adim),
          3. `GEMINI_ALLOW_HIGH_THINKING` tavani.

        Yon tek yonludur: yanlis yazilmis bir override bir yazma yolunu az
        dusunulmus birakamaz.
        """
        # 1. Pack bazli taban: acilan paketlerden en yuksegi.
        base = self.settings.agent.reasoning_level(self.active_packs)
        # 2. Akis karmasikligi tabani daha da yukseltebilir.
        if (domain_count > 1 or iteration > 1) and base in {"minimal", "low"}:
            base = "medium"
        # 3. `high` acik izin ister; yoksa `medium`a inilir.
        if base == "high" and not self.settings.model.allow_high_thinking:
            return "medium"
        return base

    # --- Yardimcilar --------------------------------------------------------
    def _compact_history(self) -> int:
        """Eski tool sonuclarini kisaltir; son N tanesi tam kalir."""
        if self.keep_full_tool_results <= 0:
            return 0
        results: list[tuple[int, int, FunctionResult]] = [
            (mi, ri, result)
            for mi, message in enumerate(self.messages)
            if message.role == "tool"
            for ri, result in enumerate(message.function_results)
        ]
        stale = results[: max(0, len(results) - self.keep_full_tool_results)]
        trimmed = 0
        for message_index, result_index, result in stale:
            content = result.content
            if content.endswith(_COMPACT_MARKER) or len(content) <= self.compacted_result_chars:
                continue
            new_content = content[: self.compacted_result_chars] + _COMPACT_MARKER
            trimmed += len(content) - len(new_content)
            message = self.messages[message_index]
            updated = list(message.function_results)
            updated[result_index] = FunctionResult(
                call_id=result.call_id,
                name=result.name,
                content=new_content,
                is_error=result.is_error,
            )
            self.messages[message_index] = ModelMessage(
                role=message.role,
                text=message.text,
                function_calls=message.function_calls,
                function_results=tuple(updated),
                provider_state=message.provider_state,
            )
        return trimmed

    def _execute_call(
        self,
        call: FunctionCall,
        *,
        allowed: frozenset[str],
        executed: dict[str, FunctionResult],
        fingerprints: dict[str, FunctionResult],
        turn: AgentTurn,
        on_tool_start: Callable[[str, dict], None] | None,
        on_tool_end: Callable[[str, bool], None] | None,
    ) -> FunctionResult:
        """Tek bir model onerisini guvenli sekilde yurutur.

        Sirasiyla: (a) call_id tekilleme, (b) allowlist kontrolu,
        (c) mutating tool tekrar korumasi, (d) ``execute_tool``.
        """
        arguments = dict(call.arguments or {})

        # (a) Ayni call_id ikinci kez geldi: TEKRAR CALISTIRMA.
        # Saglayici retry'i ya da modelin tekrari, yazilmis bir SAP belgesini
        # ikinci kez yazamaz.
        previous = executed.get(call.id)
        if previous is not None:
            log.info("tool tekrari engellendi | call_id=%s | %s", call.id, call.name)
            return previous

        # (b) Fail-closed: gorunmeyen/yetkisiz/bilinmeyen tool calistirilmaz.
        if call.name not in allowed:
            log.warning(
                "model gorunmeyen tool onerdi | %s | reddedildi (fail-closed)", call.name
            )
            assert self.ctx.audit and self.ctx.execution
            self.ctx.audit.append(
                "tool.not_available",
                execution=self.ctx.execution,
                tool=call.name,
                outcome="denied",
                detail={"reason": "model gorunmeyen tool onerdi"},
                model=self.ctx.model,
                prompt_version=self.prompt_version,
            )
            payload = json.dumps({**_UNKNOWN_TOOL, "tool": call.name}, ensure_ascii=False)
            turn.policy_denials += 1
            result = FunctionResult(
                call_id=call.id, name=call.name, content=payload, is_error=True
            )
            executed[call.id] = result
            turn.tool_calls.append(
                ToolCall(name=call.name, arguments=arguments, result=payload, is_error=True)
            )
            return result

        # (c) Mutating tool: ayni cagri iki farkli call_id ile gelse bile
        # bir turda bir kez calisir. Idempotency katmani ikinci savunmadir.
        spec = REGISTRY.get(call.name)
        fingerprint = ""
        if spec is not None and spec.risk_tier.is_mutating:
            fingerprint = _args_fingerprint(call.name, arguments)
            repeated = fingerprints.get(fingerprint)
            if repeated is not None:
                log.warning(
                    "mutating tool ayni turda tekrar onerildi | %s | engellendi", call.name
                )
                executed[call.id] = repeated
                return repeated

        if on_tool_start:
            on_tool_start(call.name, arguments)
        payload, is_error = execute_tool(call.name, arguments, self.ctx)
        if on_tool_end:
            on_tool_end(call.name, is_error)

        result = FunctionResult(
            call_id=call.id, name=call.name, content=payload, is_error=is_error
        )
        executed[call.id] = result
        if fingerprint:
            fingerprints[fingerprint] = result
        turn.tool_calls.append(
            ToolCall(name=call.name, arguments=arguments, result=payload, is_error=is_error)
        )
        if '"needs_review":true' in payload:
            turn.needs_review = True
        return result

    # --- Kisayol (model cagrilmadan) ---------------------------------------
    def _try_shortcut(self, user_message: str, execution: ExecutionContext) -> AgentTurn | None:
        """Soru deterministik olarak tek bir okuma tool'una esleniyor mu?

        Eslesirse tool yine ``execute_tool`` uzerinden (policy + DLP + audit)
        calisir, sonuc yerel olarak metne cevrilir ve **saglayiciya hicbir
        istek gitmez**: SAP verisi surecin disina cikmaz.
        """
        if not self.settings.agent.direct_answers_enabled:
            return None
        match = match_shortcut(user_message)
        if match is None:
            return None
        permitted = frozenset(visible_tool_names(_ALL_DOMAINS, self.actor))
        if match.tool not in permitted:
            return None

        payload, is_error = execute_tool(match.tool, match.arguments, self.ctx)
        if is_error:
            return None
        try:
            body = json.loads(payload)
        except ValueError:
            return None
        answer = direct_answer_for(match.tool, body, reason="shortcut")
        if answer is None:
            return None

        assert self.ctx.audit
        self.ctx.audit.append(
            "turn.direct_answer",
            execution=execution,
            tool=match.tool,
            outcome="ok",
            detail={"shortcut": match.shortcut.name, **answer.to_dict()},
            model="(atlandi)",
            prompt_version=self.prompt_version,
        )
        log.info("dogrudan yanit | kisayol=%s | saglayici cagrilmadi", match.shortcut.name)
        self.messages.append(ModelMessage(role="user", text=user_message))
        self.messages.append(ModelMessage(role="assistant", text=answer.text))
        return AgentTurn(
            text=answer.text,
            tool_calls=[
                ToolCall(name=match.tool, arguments=match.arguments, result=payload, is_error=False)
            ],
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            active_packs=list(self.active_packs),
            direct_answer=True,
            direct_answer_reason=answer.reason,
            model_calls=0,
            provider=self.provider.name,
            model=self.provider.model,
        )

    # --- Genel API ----------------------------------------------------------
    def chat(
        self,
        user_message: str,
        *,
        on_text: Callable[[str], None] | None = None,
        on_tool_start: Callable[[str, dict], None] | None = None,
        on_tool_end: Callable[[str, bool], None] | None = None,
    ) -> AgentTurn:
        """Kullanici mesajini isler ve nihai yaniti dondurur."""
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

        shortcut = self._try_shortcut(user_message, execution)
        if shortcut is not None:
            self.telemetry.finish_turn(metrics)
            self.ctx.metrics = None
            return shortcut

        # Deterministik router: LLM KULLANMAZ.
        decision = route(user_message, self.actor)
        self.active_packs = list(decision.packs)
        self.last_routing = decision
        profiles = profiles_for_packs(self.active_packs)
        log.info(
            "router | packs=%s | domains=%s | intent=%r",
            ",".join(self.active_packs),
            ",".join(p.key for p in profiles),
            summarize_intent(user_message),
        )

        system = self._custom_prompt or build_runtime_prompt(
            self.settings, profiles=profiles, actor=self.actor
        )
        names = self.visible_tools()
        allowed = frozenset(names)
        declarations = self._declarations(names)

        turn = AgentTurn(
            text="",
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            active_packs=list(self.active_packs),
            active_domains=[p.key for p in profiles],
            provider=self.provider.name,
            model=self.provider.model,
            schema_tokens=sum(estimate_tokens(d.to_dict()) for d in declarations),
        )
        metrics.schema_tokens = turn.schema_tokens
        metrics.active_packs = tuple(self.active_packs)
        turn.domain_trace = [
            {"domain": p.key, "title": p.title, "packs": list(p.domain_packs)} for p in profiles
        ]

        self.messages.append(ModelMessage(role="user", text=user_message))
        artifacts_before = len(self.ctx.artifacts)
        max_iterations = min(
            iteration_budget_for(self.active_packs), self.settings.agent.max_tool_iterations
        )
        executed: dict[str, FunctionResult] = {}
        fingerprints: dict[str, FunctionResult] = {}
        usage = TokenUsage()
        budget_exhausted = False

        for iteration in range(1, max_iterations + 1):
            turn.iterations = iteration
            metrics.iterations = iteration
            allow_tools = metrics.tool_result_tokens < self.settings.budget.turn_result_tokens
            if not allow_tools and not budget_exhausted:
                budget_exhausted = True
                log.warning(
                    "tur sonuc butcesi doldu (%d/%d token); tool cagrilari kapatildi",
                    metrics.tool_result_tokens,
                    self.settings.budget.turn_result_tokens,
                )
            level = self._thinking_level(
                iteration=iteration, domain_count=len(profiles)
            )
            turn.thinking_level = level
            request = ModelRequest(
                system=system,
                messages=self._history_for_provider(),
                functions=declarations if allow_tools else (),
                max_output_tokens=self.settings.agent.max_tokens,
                thinking_level=level,  # type: ignore[arg-type]
                stream=self.stream and on_text is not None,
                timeout_s=self.settings.model.timeout_s,
                store=self.settings.model.store_interactions,
            )
            try:
                response = self._call_provider(request, on_text)
            except ModelProviderError as exc:
                # Saglayici hatasi SAP tarafinda bir sey degistirmez; bu
                # noktada calistirilmis tool'lar zaten tamamlanmistir ve
                # TEKRARLANMAZ.
                log.error("saglayici hatasi | %s | %s", exc.kind, exc.provider)
                assert self.ctx.audit
                self.ctx.audit.append(
                    "model.provider_error",
                    execution=execution,
                    outcome="error",
                    detail={"kind": exc.kind, "retryable": exc.retryable},
                    model=self.ctx.model,
                    prompt_version=self.prompt_version,
                )
                turn.needs_review = True
                metrics.needs_review = True
                turn.stop_reason = "provider_error"
                turn.text = (
                    (turn.text + "\n\n" if turn.text else "")
                    + f"[Model saglayicisina ulasilamadi ({exc.kind}). "
                    "Calistirilmis SAP islemleri tekrarlanmadi; devam eden bir islem "
                    "varsa sap_get_execution_audit ile durumunu dogrulayin.]"
                )
                break

            turn.model_calls += 1
            usage = usage.merge(response.usage)
            turn.stop_reason = response.stop_reason
            if response.text:
                # Model cikti metni istemciye ham verilmez (OWASP LLM05): hem
                # `turn.text` hem streaming geri cagrisi ayni temiz metni alir.
                turn.text = (
                    sanitize_for_client(
                        response.text,
                        actor=self.actor,
                        settings=self.settings,
                        dlp=self.ctx.dlp,
                    )
                    if self.actor is not None
                    else response.text
                )
                if on_text and not request.stream:
                    on_text(turn.text)
            self.messages.append(response.to_assistant_message())

            if not response.function_calls:
                break

            # Saglayici cagrisi saglikli bitmediyse function call argumanlari
            # yarim kalmis olabilir. Yarim bir argumanla SAP yazmasi
            # calistirmak geri alinamaz; bu yuzden hicbiri calistirilmaz.
            # Iki ayri "yarim kalmis" hali vardir ve ikisi de function call
            # argumanlarini eksik birakabilir:
            #   status != completed  -> saglayici cagrisi saglikli bitmedi
            #   stop_reason == max_tokens -> uretim token siniri yuzunden kesildi
            # Ikincisinde status "completed" gelir; yalniz status'e bakmak
            # yetmez. Her iki halde de hicbir cagri calistirilmaz.
            truncated = response.stop_reason == "max_tokens" and bool(response.function_calls)
            unhealthy = response.status not in HEALTHY_PROVIDER_STATUSES
            if unhealthy and response.status not in BROKEN_PROVIDER_STATUSES:
                # Taninmayan durum: guvenli tarafta kaliriz ama sessiz kalmayiz.
                # Sessiz bloklama, bir eval'in "hicbir sey calismadigi icin"
                # gecmesine yol acar - yani yanlis sebepten yesil olur.
                log.error(
                    "saglayici taninmayan bir status dondurdu: %r - "
                    "function call'lar guvenlik geregi calistirilmadi",
                    response.status,
                )
            if unhealthy or truncated:
                assert self.ctx.audit
                self.ctx.audit.append(
                    "model.incomplete_response",
                    execution=execution,
                    outcome="error",
                    detail={
                        "status": response.status,
                        "stop_reason": response.stop_reason,
                        "dropped_calls": [c.name for c in response.function_calls],
                    },
                    model=self.ctx.model,
                )
                log.error(
                    "saglayici yaniti tamamlanmadi (%s): %d function call calistirilmadi",
                    response.status, len(response.function_calls),
                )
                turn.needs_review = True
                turn.stop_reason = "max_tokens" if truncated else response.status
                turn.text = (
                    "[Saglayici yaniti tamamlanmis sayilmadi; hicbir islem "
                    "calistirilmadi. Lutfen tekrar deneyin.]"
                )
                break

            results: list[FunctionResult] = []
            for call in response.function_calls:
                results.append(
                    self._execute_call(
                        call,
                        allowed=allowed,
                        executed=executed,
                        fingerprints=fingerprints,
                        turn=turn,
                        on_tool_start=on_tool_start,
                        on_tool_end=on_tool_end,
                    )
                )

            # Son tur atlama: sonuc kendi kendine yeterliyse payload'i
            # saglayiciya GERI GONDERMEYIZ.
            if self._finish_locally(turn, response.function_calls, results, iteration):
                self.messages.append(ModelMessage(role="tool", function_results=tuple(results)))
                self.messages.append(ModelMessage(role="assistant", text=turn.text))
                break

            self.messages.append(ModelMessage(role="tool", function_results=tuple(results)))
            turn.compacted_chars += self._compact_history()
            metrics.compacted_chars = turn.compacted_chars
        else:
            turn.text += (
                f"\n\n[Uyari: {max_iterations} tool adimi limitine ulasildi. Islem yarim "
                "kalmis olabilir; sap_get_execution_audit ile son durumu kontrol edin.]"
            )
            turn.needs_review = True
            metrics.needs_review = True

        if budget_exhausted:
            turn.text += (
                f"\n\n[Uyari: tool sonuclari icin ayrilan "
                f"{self.settings.budget.turn_result_tokens:,} token butcesi doldu.]"
            )
            turn.needs_review = True
            metrics.needs_review = True

        turn.input_tokens = usage.input_tokens
        turn.output_tokens = usage.output_tokens
        turn.cache_read_tokens = usage.cached_input_tokens
        turn.reasoning_tokens = usage.reasoning_tokens
        metrics.uncached_input_tokens += usage.input_tokens
        metrics.cache_read_tokens += usage.cached_input_tokens
        metrics.output_tokens += usage.output_tokens
        metrics.reasoning_tokens += usage.reasoning_tokens
        turn.artifacts = self.ctx.artifacts[artifacts_before:]
        turn.policy_denials = max(turn.policy_denials, metrics.policy_denials)

        assert self.ctx.audit
        self.ctx.audit.append(
            "turn.completed",
            execution=execution,
            outcome="needs_review" if turn.needs_review else "ok",
            detail={
                "provider": self.provider.name,
                "model": self.provider.model,
                "thinking_level": turn.thinking_level,
                "model_calls": turn.model_calls,
                "tool_calls": len(turn.tool_calls),
                "domains": turn.active_domains,
                "usage": usage.to_dict(),
            },
            model=self.ctx.model,
            prompt_version=self.prompt_version,
        )
        self.telemetry.finish_turn(metrics)
        self.ctx.metrics = None
        return turn

    def _history_for_provider(self) -> list[Any]:
        """Saglayiciya gidecek konusma gecmisi.

        Runtime'in kendi urettigi `ModelMessage` kayitlari zaten DLP'den
        gecmistir. Ama gecmis bir oturum kaydindan yuklenmis ya da disaridan
        atanmis ham kayitlar gecmemis olabilir; saglayici siniri bu kayitlara
        GUVENMEZ ve onlari yeniden temizler.
        """
        out: list[Any] = []
        for message in self.messages:
            if isinstance(message, ModelMessage) or self.actor is None:
                out.append(message)
                continue
            out.append(
                _scrub(message, actor=self.actor, settings=self.settings, dlp=self.ctx.dlp)
            )
        return out

    def _call_provider(self, request: ModelRequest, on_text):
        """Saglayiciyi cagirir; destekliyorsa streaming kullanir.

        Streaming opsiyoneldir: saglayici `on_text` anahtar argumanini kabul
        etmiyorsa sessizce normal moda dusulur. Function call'lar her iki
        durumda da **akis bittikten sonra** islenir; yarim argumanla SAP
        yazmasi calistirilamaz.
        """
        if request.stream and on_text is not None:
            try:
                return self.provider.generate(request, on_text=on_text)
            except TypeError:
                log.debug("saglayici streaming desteklemiyor; normal moda dusuluyor")
        return self.provider.generate(request)

    def _finish_locally(
        self,
        turn: AgentTurn,
        calls: tuple[FunctionCall, ...],
        results: list[FunctionResult],
        iteration: int,
    ) -> bool:
        """Tool sonucunu saglayiciya geri gondermeden turu bitirebilir miyiz?"""
        if not self.settings.agent.direct_answers_enabled:
            return False
        if iteration != 1 or len(calls) != 1 or len(results) != 1 or len(turn.tool_calls) != 1:
            return False
        result = results[0]
        if result.is_error:
            return False
        try:
            body = json.loads(result.content)
        except ValueError:
            return False
        answer = direct_answer_for(result.name, body, reason="self_contained")
        if answer is None:
            return False
        turn.text = answer.text
        turn.direct_answer = True
        turn.direct_answer_reason = answer.reason
        turn.stop_reason = "direct_answer"
        if self.ctx.audit and self.ctx.execution:
            self.ctx.audit.append(
                "turn.direct_answer",
                execution=self.ctx.execution,
                tool=result.name,
                outcome="ok",
                detail=answer.to_dict(),
                model=self.ctx.model,
                prompt_version=self.prompt_version,
            )
        log.info("dogrudan yanit | tool=%s | son saglayici turu atlandi", result.name)
        return True

    # --- Teshis -------------------------------------------------------------
    def reset(self) -> None:
        self.messages.clear()
        self.active_packs = ["bootstrap"]
        self.last_routing = None

    def describe_tools(self) -> list[dict[str, Any]]:
        return registry_summary()

    def health(self) -> dict[str, Any]:
        return {
            "architecture": "certaops-single-runtime",
            "model": self.settings.model.describe(),
            "domains": [p.to_dict() for p in DOMAIN_PROFILES.values()],
            "active_packs": list(self.active_packs),
            "visible_tool_count": len(self.visible_tools()),
            "registered_tool_count": len(registry_summary()),
            "sap": self.ctx.sap.ping(),
            "dry_run": self.settings.sap.dry_run,
            "output_dir": str(self.settings.output_dir),
            "actor": self.actor.to_dict(include_scopes=True),
        }

    def budget_report(self) -> dict[str, Any]:
        from robotics_agent.core import schema_token_report

        declarations = [d.to_dict() for d in self._declarations(self.visible_tools())]
        return {
            "architecture": "certaops-single-runtime",
            "active_packs": list(self.active_packs),
            "schema": schema_token_report(
                declarations, budget=self.settings.budget.schema_tokens_per_turn
            ),
            "telemetry": self.telemetry.snapshot(),
            "limits": {
                "single_result_tokens": self.settings.budget.single_result_tokens,
                "turn_result_tokens": self.settings.budget.turn_result_tokens,
                "iteration_limit": iteration_budget_for(self.active_packs),
            },
        }

    def context_size(self) -> dict[str, int]:
        history = sum(
            len(m.text) + sum(len(r.content) for r in m.function_results)
            for m in self.messages
        )
        fixed = len(self._custom_prompt or "") + sum(
            estimate_tokens(d.to_dict()) * 4 for d in self._declarations(self.visible_tools())
        )
        return {
            "history_tokens": history // 4,
            "fixed_tokens": fixed // 4,
            "total_tokens": (history + fixed) // 4,
            "message_count": len(self.messages),
        }

    def close(self) -> None:
        self.provider.close()


#: Kisayol yolunda actor yetkisini kontrol etmek icin tum domainler.
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
