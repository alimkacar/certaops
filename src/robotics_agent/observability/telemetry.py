"""Token, maliyet ve guvenlik telemetrisi.

Her turda uncached/cache/output tokeni, tool semasi ile tool sonucu tokeni ayri
ayri; domain, tool, risk seviyesi, execution ID ve compaction etkisiyle birlikte
olculur. Loglara prompt veya SAP payload'i yazilmaz; yalnizca sayim, sinif ve ID.

## Neden gorev sonucu da olculur

Yalniz token saymak yaniltir: bir turu yarim birakip az token harcamak
"iyilesme" gibi gorunur. Rehberin olcum ilkesi **basarili gorev basina toplam
maliyet**tir. Bu yuzden her tur bir `TaskOutcome` ile siniflandirilir ve
maliyet metrikleri yalniz `success` turlara bolunur.

Siniflandirma deterministiktir ve fail-closed'dur: emin olunmayan her tur
`needs_review` sayilir, `success` degil.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class TaskOutcome(str, Enum):
    """Bir turun is acisindan sonucu.

    `SUCCESS` yalniz "model bir sey soyledi" demek degildir: tur hata almadan
    bitmis, insan incelemesi gerektirmemis ve yetki reddiyle kesilmemis olmali.
    """

    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True)
class CostModel:
    """Milyon token basina fiyat. Sifir = fiyatlandirilmamis.

    Maliyet **tahminidir**: saglayici faturasi degil, kendi sayaclarimiz
    uzerinden hesaplanir. `priced` False iken maliyet raporlanmaz; token bazli
    metrikler yine calisir, boylece fiyat girilmemis bir kurulum sessizce
    "maliyet sifir" gibi gorunmez.
    """

    currency: str = "USD"
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0

    @property
    def priced(self) -> bool:
        return any(
            value > 0
            for value in (
                self.input_per_mtok,
                self.output_per_mtok,
                self.cache_read_per_mtok,
                self.cache_write_per_mtok,
            )
        )

    def estimate(
        self,
        *,
        uncached_input: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        output: int = 0,
    ) -> float:
        if not self.priced:
            return 0.0
        million = 1_000_000
        return round(
            uncached_input / million * self.input_per_mtok
            + cache_read / million * self.cache_read_per_mtok
            + cache_write / million * self.cache_write_per_mtok
            + output / million * self.output_per_mtok,
            6,
        )

    @classmethod
    def from_settings(cls, settings: Any) -> CostModel:
        cfg = getattr(settings, "cost", settings)
        return cls(
            currency=str(getattr(cfg, "currency", "USD")),
            input_per_mtok=float(getattr(cfg, "input_per_mtok", 0.0)),
            output_per_mtok=float(getattr(cfg, "output_per_mtok", 0.0)),
            cache_read_per_mtok=float(getattr(cfg, "cache_read_per_mtok", 0.0)),
            cache_write_per_mtok=float(getattr(cfg, "cache_write_per_mtok", 0.0)),
        )


NO_PRICING = CostModel()


@dataclass
class ToolInvocationMetric:
    tool: str
    domain: str
    risk_tier: str
    outcome: str  # ok | error | denied
    duration_ms: float
    result_tokens: int
    trimmed: bool = False
    dropped_items: int = 0
    detail: str = "standard"
    denial_code: str = ""
    # --- Cache ve adapter performans olcumleri ------------------------------
    cache_hit: bool = False
    sap_calls: int = 0
    dlp_findings: int = 0
    # --- Onay ve yazma sonucu (rehber metrikleri icin) ----------------------
    #: Policy bu cagri icin gecerli bir onay kaydi tuketti mi?
    approval_used: bool = False
    #: `WriteGuard` sonucu: created | duplicate_prevented | reconciled | ...
    write_status: str = ""

    @property
    def is_write_attempt(self) -> bool:
        return bool(self.write_status)

    @property
    def is_duplicate_prevented(self) -> bool:
        return self.write_status == "duplicate_prevented"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "tool": self.tool,
            "domain": self.domain,
            "risk_tier": self.risk_tier,
            "outcome": self.outcome,
            "duration_ms": round(self.duration_ms, 1),
            "result_tokens": self.result_tokens,
            "detail": self.detail,
            "cache_hit": self.cache_hit,
            "sap_calls": self.sap_calls,
        }
        if self.trimmed:
            payload["trimmed"] = True
            payload["dropped_items"] = self.dropped_items
        if self.denial_code:
            payload["denial_code"] = self.denial_code
        if self.dlp_findings:
            payload["dlp_findings"] = self.dlp_findings
        if self.approval_used:
            payload["approval_used"] = True
        if self.write_status:
            payload["write_status"] = self.write_status
        return payload


@dataclass
class TurnMetrics:
    """Bir kullanici turunun olculen maliyeti."""

    execution_id: str
    correlation_id: str
    tenant: str = ""
    channel: str = "cli"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_packs: tuple[str, ...] = ()
    schema_tokens: int = 0
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    #: Muhakeme ("thinking") token'lari. Yanit govdesinde gorunmezler ama
    #: saglayici bunlari CIKTI tarifesinden faturalar; maliyet hesabina dahildir.
    reasoning_tokens: int = 0
    tool_result_tokens: int = 0
    compacted_chars: int = 0
    iterations: int = 0
    tool_calls: list[ToolInvocationMetric] = field(default_factory=list)
    policy_denials: int = 0
    budget_trims: int = 0
    needs_review: bool = False
    # Gizlilik metrikleri maskelenen alan ve engellenen veri erisimi sayisini
    # tutar. Veri degeri hicbir zaman telemetriye yazilmaz.
    dlp_findings: int = 0
    data_policy_denials: int = 0
    cache_hits: int = 0
    sap_calls: int = 0
    # --- Gorev sonucu ve model secimi ---------------------------------------
    #: Tur bittiginde `classify_outcome()` ile belirlenir; fail-closed.
    outcome: TaskOutcome = TaskOutcome.NEEDS_REVIEW
    #: Bu turda uygulanan akil yurutme seviyesi (kademelendirme kaniti).
    reasoning_level: str = ""
    model: str = ""
    #: Tur bir istisna ile bitti mi? `classify_outcome()` bunu ERROR sayar.
    failed: bool = False

    def record_tool(self, metric: ToolInvocationMetric) -> None:
        self.tool_calls.append(metric)
        self.tool_result_tokens += metric.result_tokens
        self.dlp_findings += metric.dlp_findings
        self.sap_calls += metric.sap_calls
        if metric.cache_hit:
            self.cache_hits += 1
        if metric.outcome == "denied":
            self.policy_denials += 1
            if metric.denial_code == "DATA_POLICY_DENIED":
                self.data_policy_denials += 1
        if metric.trimmed:
            self.budget_trims += 1

    # --- Gorev sonucu -------------------------------------------------------
    def classify_outcome(self) -> TaskOutcome:
        """Turu deterministik olarak siniflandirir ve `outcome`a yazar.

        Sira fail-closed'dur:
          1. Istisna -> ERROR
          2. `needs_review` -> NEEDS_REVIEW (yarim kalmis yazma, dogrulanmamis
             postcondition, iterasyon limiti)
          3. Hicbir tool basarili olmadan policy reddi -> DENIED
          4. Aksi halde -> SUCCESS

        3. adim onemli: modelin yetkisiz bir tool denemesi, sonra izinli bir
        tool ile isi tamamlamasi BASARIDIR. Turun tamami reddedilmisse basari
        degildir. "Reddedildi" ile "basarisiz" ayni sey degildir; ret dogru
        davranis olabilir, ama basarili gorev sayilmaz.
        """
        if self.failed:
            self.outcome = TaskOutcome.ERROR
        elif self.needs_review:
            self.outcome = TaskOutcome.NEEDS_REVIEW
        elif self.policy_denials and not any(t.outcome == "ok" for t in self.tool_calls):
            self.outcome = TaskOutcome.DENIED
        else:
            self.outcome = TaskOutcome.SUCCESS
        return self.outcome

    @property
    def succeeded(self) -> bool:
        return self.outcome is TaskOutcome.SUCCESS

    @property
    def approval_used(self) -> bool:
        return any(t.approval_used for t in self.tool_calls)

    @property
    def write_attempts(self) -> int:
        return sum(1 for t in self.tool_calls if t.is_write_attempt)

    @property
    def duplicate_writes_prevented(self) -> int:
        return sum(1 for t in self.tool_calls if t.is_duplicate_prevented)

    def estimated_cost(self, cost_model: CostModel = NO_PRICING) -> float:
        # Muhakeme token'lari cikti tarifesinden faturalanir; yalniz
        # `output_tokens` saymak maliyeti sistematik olarak dusuk gosterir.
        return cost_model.estimate(
            uncached_input=self.uncached_input_tokens,
            cache_read=self.cache_read_tokens,
            cache_write=self.cache_write_tokens,
            output=self.output_tokens + self.reasoning_tokens,
        )

    @property
    def billed_tokens(self) -> int:
        """Fiyat girilmemis kurulumlarda maliyet yerine kullanilan olcu."""
        return self.billed_input_tokens + self.output_tokens + self.reasoning_tokens

    @property
    def billed_input_tokens(self) -> int:
        return self.uncached_input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cache_hit_rate(self) -> float:
        billed = self.uncached_input_tokens + self.cache_read_tokens
        return round(self.cache_read_tokens / billed * 100, 1) if billed else 0.0

    @property
    def duration_s(self) -> float:
        return round((datetime.now(timezone.utc) - self.started_at).total_seconds(), 2)

    def budget_status(self, budget: Any) -> dict[str, Any]:
        """Guardrail'lere gore durum. `budget` config.TokenBudget."""
        return {
            "schema": {
                "tokens": self.schema_tokens,
                "limit": budget.schema_tokens_per_turn,
                "ok": self.schema_tokens <= budget.schema_tokens_per_turn,
            },
            "turn_results": {
                "tokens": self.tool_result_tokens,
                "limit": budget.turn_result_tokens,
                "ok": self.tool_result_tokens <= budget.turn_result_tokens,
            },
            "largest_result": max((t.result_tokens for t in self.tool_calls), default=0),
            "single_result_limit": budget.single_result_tokens,
        }

    def to_dict(self, cost_model: CostModel = NO_PRICING) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "tenant": self.tenant,
            "channel": self.channel,
            "duration_s": self.duration_s,
            "iterations": self.iterations,
            "active_packs": list(self.active_packs),
            "outcome": self.outcome.value,
            "reasoning_level": self.reasoning_level,
            "model": self.model,
            "tokens": {
                "schema": self.schema_tokens,
                "uncached_input": self.uncached_input_tokens,
                "cache_read": self.cache_read_tokens,
                "cache_write": self.cache_write_tokens,
                "output": self.output_tokens,
                # Yanitta gorunmez ama cikti tarifesinden faturalanir.
                "reasoning": self.reasoning_tokens,
                "tool_results": self.tool_result_tokens,
                "cache_hit_rate_pct": self.cache_hit_rate,
                "billed": self.billed_tokens,
            },
            "compacted_chars": self.compacted_chars,
            "policy_denials": self.policy_denials,
            "budget_trims": self.budget_trims,
            "needs_review": self.needs_review,
            "privacy": {
                "dlp_findings": self.dlp_findings,
                "data_policy_denials": self.data_policy_denials,
            },
            "performance": {
                "sap_calls": self.sap_calls,
                "cache_hits": self.cache_hits,
            },
            "tool_calls": [t.to_dict() for t in self.tool_calls],
        }
        # Fiyat girilmemisse "maliyet 0" yazmak yaniltici olurdu; alan hic
        # eklenmez ve `priced: false` ile durum acikca bildirilir.
        if cost_model.priced:
            payload["cost"] = {
                "priced": True,
                "currency": cost_model.currency,
                "estimated": self.estimated_cost(cost_model),
            }
        else:
            payload["cost"] = {"priced": False}
        return payload


def _percentile(values: list[float], pct: float) -> float:
    """Kucuk orneklemler icin en yakin-siralama yuzdelik degeri."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return round(ordered[index], 2)


class TelemetryCollector:
    """Son N turun metrigini bellekte tutar ve toplu istatistik uretir."""

    def __init__(self, *, keep: int = 200, cost_model: CostModel = NO_PRICING) -> None:
        self._keep = max(10, keep)
        self._turns: list[TurnMetrics] = []
        self._lock = threading.Lock()
        self.cost_model = cost_model

    def start_turn(
        self,
        *,
        execution_id: str,
        correlation_id: str,
        tenant: str = "",
        channel: str = "cli",
        model: str = "",
        reasoning_level: str = "",
    ) -> TurnMetrics:
        metrics = TurnMetrics(
            execution_id=execution_id,
            correlation_id=correlation_id,
            tenant=tenant,
            channel=channel,
            model=model,
            reasoning_level=reasoning_level,
        )
        return metrics

    def finish_turn(self, metrics: TurnMetrics) -> None:
        metrics.classify_outcome()
        with self._lock:
            self._turns.append(metrics)
            if len(self._turns) > self._keep:
                self._turns = self._turns[-self._keep :]
        # Prompt icerigi degil, yalnizca sayim ve siniflandirma loglanir.
        log.info(
            "turn | exec=%s | outcome=%s | iter=%d | schema=%d | results=%d | out=%d "
            "| cache=%.0f%% | deny=%d | reasoning=%s",
            metrics.execution_id,
            metrics.outcome.value,
            metrics.iterations,
            metrics.schema_tokens,
            metrics.tool_result_tokens,
            metrics.output_tokens,
            metrics.cache_hit_rate,
            metrics.policy_denials,
            metrics.reasoning_level or "-",
        )

    # --- Rehber metrikleri --------------------------------------------------
    def effectiveness(self, *, limit: int = 20) -> dict[str, Any]:
        """Basarili gorev basina maliyet ve guvenlik oranlari.

        Rehberin temel olcum ilkesi: "Daha dusuk token veya cagri sayisini
        yalnizca gorev basarisi ve guvenlik metrikleri korunuyorsa iyilestirme
        olarak kabul edin." Bu yuzden maliyet **basarili gorev sayisina**
        bolunur; yarim kalan turlarin ucuzlugu iyilesme sayilmaz.

        Fiyat girilmemisse `cost_per_successful_task` yerine
        `tokens_per_successful_task` kullanilir; ikisi de ayni kararı verdirir.
        """
        with self._lock:
            turns = list(self._turns[-limit:])
        if not turns:
            return {"turns": 0}

        total = len(turns)
        successful = [t for t in turns if t.succeeded]
        success_count = len(successful)
        tool_calls = sum(len(t.tool_calls) for t in turns)
        denied_calls = sum(t.policy_denials for t in turns)
        write_attempts = sum(t.write_attempts for t in turns)
        duplicates = sum(t.duplicate_writes_prevented for t in turns)
        approval_turns = sum(1 for t in turns if t.approval_used)
        latencies = [t.duration_s for t in turns]

        def per_success(value: float) -> float | None:
            return round(value / success_count, 4) if success_count else None

        report: dict[str, Any] = {
            "turns": total,
            "successful_tasks": success_count,
            "task_success_rate_pct": round(success_count / total * 100, 1),
            "outcomes": {
                outcome.value: sum(1 for t in turns if t.outcome is outcome)
                for outcome in TaskOutcome
            },
            # Yetkisiz islem orani: reddedilen cagri / toplam cagri. Rehber bu
            # oranin sifira yakin olmasini bekler; yuksek oran ya yanlis yetki
            # tanimi ya da modelin sinir zorlamasi demektir.
            "unauthorized_attempt_rate_pct": (
                round(denied_calls / tool_calls * 100, 1) if tool_calls else 0.0
            ),
            "human_approval_rate_pct": round(approval_turns / total * 100, 1),
            "duplicate_write_rate_pct": (
                round(duplicates / write_attempts * 100, 1) if write_attempts else 0.0
            ),
            "tool_calls_per_successful_task": per_success(tool_calls),
            "sap_calls_per_successful_task": per_success(sum(t.sap_calls for t in turns)),
            "tokens_per_successful_task": per_success(sum(t.billed_tokens for t in turns)),
            "p50_latency_s": _percentile(latencies, 50),
            "p95_latency_s": _percentile(latencies, 95),
            "cache_hit_rate_pct": round(sum(t.cache_hit_rate for t in turns) / total, 1),
            "sensitive_data_findings": sum(t.dlp_findings for t in turns),
            "data_policy_denials": sum(t.data_policy_denials for t in turns),
        }
        if self.cost_model.priced:
            spend = sum(t.estimated_cost(self.cost_model) for t in turns)
            report["cost"] = {
                "priced": True,
                "currency": self.cost_model.currency,
                "estimated_total": round(spend, 4),
                "cost_per_successful_task": per_success(spend),
            }
        else:
            report["cost"] = {
                "priced": False,
                "note": (
                    "MODEL_COST_* degerleri girilmedi; maliyet yerine "
                    "tokens_per_successful_task kullanin."
                ),
            }
        return report

    def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            turns = list(self._turns[-limit:])
        if not turns:
            return {"turns": 0}
        tool_counter: dict[str, int] = {}
        for turn in turns:
            for call in turn.tool_calls:
                tool_counter[call.tool] = tool_counter.get(call.tool, 0) + 1
        reasoning_counter: dict[str, int] = {}
        for turn in turns:
            key = turn.reasoning_level or "unset"
            reasoning_counter[key] = reasoning_counter.get(key, 0) + 1
        return {
            "turns": len(turns),
            "avg_schema_tokens": round(sum(t.schema_tokens for t in turns) / len(turns), 1),
            "avg_result_tokens": round(sum(t.tool_result_tokens for t in turns) / len(turns), 1),
            "avg_output_tokens": round(sum(t.output_tokens for t in turns) / len(turns), 1),
            "avg_cache_hit_rate_pct": round(sum(t.cache_hit_rate for t in turns) / len(turns), 1),
            "total_policy_denials": sum(t.policy_denials for t in turns),
            "total_budget_trims": sum(t.budget_trims for t in turns),
            # Gizlilik telemetrisi yalniz maskelenen alan ve engellenen veri
            # erisimi sayilarini tutar; veri degeri hicbir zaman saklanmaz.
            "total_dlp_findings": sum(t.dlp_findings for t in turns),
            "total_data_policy_denials": sum(t.data_policy_denials for t in turns),
            "total_sap_calls": sum(t.sap_calls for t in turns),
            "total_cache_hits": sum(t.cache_hits for t in turns),
            "top_tools": sorted(tool_counter.items(), key=lambda kv: -kv[1])[:10],
            # Kademelendirmenin gercekten uygulandiginin kaniti.
            "reasoning_levels": reasoning_counter,
            "effectiveness": self.effectiveness(limit=limit),
        }
