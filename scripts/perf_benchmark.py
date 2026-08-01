#!/usr/bin/env python3
"""Tool bazli p95 gecikme, SAP cagri sayisi ve token butcesi benchmark'i.

Her salt-okunur tool defalarca calistirilir; p50/p95, SAP cagri sayisi ve
sonuc token sayisi olculur ve tool'un **kendi bildirdigi** performans
butcesiyle karsilastirilir. Butce asilirsa sifir disi kod doner ve CI
performans kapisi kirmizi olur.

Onemli sinir: bu benchmark mock backend uzerinde calisir. Olculen sey SAP'in
gercek gecikmesi degil, **bizim katmanimizin** (policy, DLP, cache, butce,
serilestirme) ek maliyetidir. Gercek SLO kalibrasyonu quality tenant'ta
`tests/integration` ile yapilir; ikisi birbirinin yerine gecmez.

Kullanim:
    python scripts/perf_benchmark.py
    python scripts/perf_benchmark.py --iterations 200 --json perf.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robotics_agent.cache import reset_tool_cache  # noqa: E402
from robotics_agent.config import get_settings  # noqa: E402
from robotics_agent.contracts import ActorContext, estimate_tokens  # noqa: E402
from robotics_agent.sap import build_backend  # noqa: E402
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools  # noqa: E402
from robotics_agent.tools.registry import REGISTRY  # noqa: E402

# Olculen senaryolar: her biri gercek bir kullanici sorusuna karsilik gelir.
SCENARIOS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("sap_material_360", {"material_id": "SFT-SCN-270"}),
    ("sap_stock_overview", {"material_ids": ["SFT-SCN-270", "ROB-6AX-20-1800"]}),
    ("sap_atp_check", {"requests": [{"material_id": "SFT-SCN-270", "quantity": 4}]}),
    ("sap_mrp_shortage_explain", {"material_id": "SFT-SCN-270"}),
    ("sap_compare_vendors", {"material_id": "SFT-SCN-270", "quantity": 4}),
    ("sap_track_purchase_orders", {"only_open": True}),
    ("sap_document_flow", {"document_id": "5105600231"}),
    ("sap_purchase_order_360", {"po_id": "4500019014"}),
    ("sap_workflow_status", {"object_type": "purchase_requisition", "object_id": "0010004801"}),
    ("sap_supplier_invoice_status", {"only_blocked": True}),
    ("sap_invoice_block_explain", {"invoice_id": "5105600231"}),
)


def _percentile(values: list[float], pct: float) -> float:
    """Kesikli p-yuzdelik. Kucuk orneklem icin statistics.quantiles guvenilmez."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def _context() -> ToolContext:
    settings = get_settings()
    settings.ensure_dirs()
    actor = ActorContext.local_operator(
        subject="perf-benchmark",
        tenant=settings.sap.tenant,
        roles=("BUYER_LEAD", "AUDITOR"),
        company_code=settings.sap.company_code,
        plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )
    return ToolContext(settings=settings, sap=build_backend(settings), actor=actor)


def measure(tool_name: str, arguments: dict[str, Any], *, iterations: int) -> dict[str, Any]:
    spec = REGISTRY[tool_name]
    durations: list[float] = []
    tokens = 0

    for _ in range(iterations):
        # Cache her turda temizlenir: olculen sey **soguk** yol, yani en kotu
        # durum. Cache hit'i olcmek SLO'yu yapay olarak iyilestirirdi.
        reset_tool_cache()
        ctx = _context()
        started = time.perf_counter()
        payload, is_error = execute_tool(tool_name, dict(arguments), ctx)
        durations.append((time.perf_counter() - started) * 1000)
        if is_error:
            return {"tool": tool_name, "error": payload[:200], "ok": False}
        tokens = estimate_tokens(payload)

    budget = spec.performance_budget
    p50, p95 = _percentile(durations, 50), _percentile(durations, 95)
    within_latency = p95 <= budget.p95_ms
    within_tokens = tokens <= budget.max_result_tokens
    return {
        "tool": tool_name,
        "iterations": iterations,
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "max_ms": round(max(durations), 2),
        "result_tokens": tokens,
        "budget_p95_ms": budget.p95_ms,
        "budget_tokens": budget.max_result_tokens,
        "within_latency_budget": within_latency,
        "within_token_budget": within_tokens,
        "ok": within_latency and within_tokens,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Tool p95 ve token butcesi benchmark'i")
    parser.add_argument("--iterations", type=int, default=40, help="Tool basina tekrar sayisi")
    parser.add_argument("--json", type=str, default="", help="Sonucu bu dosyaya JSON olarak yaz")
    args = parser.parse_args()

    load_all_tools()
    rows = [measure(name, arguments, iterations=args.iterations) for name, arguments in SCENARIOS]
    reset_tool_cache()

    print(f"{'tool':<32} {'p50':>8} {'p95':>8} {'butce':>8} {'token':>7} {'durum':>7}")
    print("-" * 78)
    for row in rows:
        if not row.get("ok") and "error" in row:
            print(f"{row['tool']:<32} {'HATA':>41}")
            continue
        print(
            f"{row['tool']:<32} {row['p50_ms']:>8.1f} {row['p95_ms']:>8.1f} "
            f"{row['budget_p95_ms']:>8} {row['result_tokens']:>7} "
            f"{'OK' if row['ok'] else 'ASIM':>7}"
        )

    failures = [r for r in rows if not r.get("ok")]
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"iterations": args.iterations, "results": rows, "failures": len(failures)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if failures:
        print("\nPerformans butcesi asildi:", file=sys.stderr)
        for row in failures:
            print(f"  - {row['tool']}: {row}", file=sys.stderr)
        return 1
    print(f"\nTum tool'lar butce icinde ({len(rows)} senaryo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
