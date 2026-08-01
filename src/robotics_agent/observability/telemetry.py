"""Token ve guvenlik telemetrisi.

Her turda uncached/cache/output tokeni, tool semasi ile tool sonucu tokeni ayri
ayri; domain, tool, risk seviyesi, execution ID ve compaction etkisiyle birlikte
olculur. Loglara prompt veya SAP payload'i yazilmaz; yalnizca sayim, sinif ve ID.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "tenant": self.tenant,
            "channel": self.channel,
            "duration_s": self.duration_s,
            "iterations": self.iterations,
            "active_packs": list(self.active_packs),
            "tokens": {
                "schema": self.schema_tokens,
                "uncached_input": self.uncached_input_tokens,
                "cache_read": self.cache_read_tokens,
                "cache_write": self.cache_write_tokens,
                "output": self.output_tokens,
                "tool_results": self.tool_result_tokens,
                "cache_hit_rate_pct": self.cache_hit_rate,
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


class TelemetryCollector:
    """Son N turun metrigini bellekte tutar ve toplu istatistik uretir."""

    def __init__(self, *, keep: int = 200) -> None:
        self._keep = max(10, keep)
        self._turns: list[TurnMetrics] = []
        self._lock = threading.Lock()

    def start_turn(
        self, *, execution_id: str, correlation_id: str, tenant: str = "", channel: str = "cli"
    ) -> TurnMetrics:
        metrics = TurnMetrics(
            execution_id=execution_id,
            correlation_id=correlation_id,
            tenant=tenant,
            channel=channel,
        )
        return metrics

    def finish_turn(self, metrics: TurnMetrics) -> None:
        with self._lock:
            self._turns.append(metrics)
            if len(self._turns) > self._keep:
                self._turns = self._turns[-self._keep :]
        # Prompt icerigi degil, yalnizca sayim ve siniflandirma loglanir.
        log.info(
            "turn | exec=%s | iter=%d | schema=%d | results=%d | out=%d | cache=%.0f%% | deny=%d",
            metrics.execution_id,
            metrics.iterations,
            metrics.schema_tokens,
            metrics.tool_result_tokens,
            metrics.output_tokens,
            metrics.cache_hit_rate,
            metrics.policy_denials,
        )

    def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            turns = list(self._turns[-limit:])
        if not turns:
            return {"turns": 0}
        tool_counter: dict[str, int] = {}
        for turn in turns:
            for call in turn.tool_calls:
                tool_counter[call.tool] = tool_counter.get(call.tool, 0) + 1
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
        }
