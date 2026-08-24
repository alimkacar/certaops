#!/usr/bin/env python3
"""SAP CAL kabul asamalari icin ortak, sir saklamayan raporlama yardimcilari."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PASS, WARN, FAIL, BLOCKED, SKIP = "PASS", "WARN", "FAIL", "BLOCKED", "SKIP"
COLOURS = {
    PASS: "\033[32m",
    WARN: "\033[33m",
    FAIL: "\033[31m",
    BLOCKED: "\033[35m",
    SKIP: "\033[2m",
}
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"


@dataclass
class CaseResult:
    name: str
    status: str
    detail: str = ""
    duration_ms: int = 0
    sap_calls: int = 0
    requirement: str = ""


@dataclass
class StageReport:
    stage: str
    title: str
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_ms: int = 0
    target: dict[str, Any] = field(default_factory=dict)
    seeds: dict[str, str] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)

    def add(
        self,
        name: str,
        status: str,
        detail: Any = "",
        *,
        duration_ms: int = 0,
        sap_calls: int = 0,
        requirement: str = "",
    ) -> CaseResult:
        row = CaseResult(
            name=name,
            status=status,
            detail=_one_line(detail),
            duration_ms=duration_ms,
            sap_calls=sap_calls,
            requirement=requirement,
        )
        self.cases.append(row)
        colour = COLOURS.get(status, "")
        timing = f" {duration_ms:>6} ms" if duration_ms else ""
        calls = f" | {sap_calls} SAP" if sap_calls else ""
        print(f"  [{colour}{status:<7}{RESET}] {name}{timing}{calls}")
        if row.detail:
            print(f"            {DIM}{row.detail[:500]}{RESET}")
        return row

    def check(
        self,
        name: str,
        condition: bool,
        detail: Any = "",
        *,
        critical: bool = True,
        duration_ms: int = 0,
        sap_calls: int = 0,
    ) -> CaseResult:
        return self.add(
            name,
            PASS if condition else (FAIL if critical else WARN),
            detail,
            duration_ms=duration_ms,
            sap_calls=sap_calls,
        )

    def summary(self) -> dict[str, int]:
        return {
            status: sum(1 for case in self.cases if case.status == status)
            for status in (PASS, WARN, FAIL, BLOCKED, SKIP)
        }

    def finish(self, started: float, *, out: str = "") -> int:
        self.duration_ms = round((time.perf_counter() - started) * 1000)
        tally = self.summary()
        print(
            f"\n{BOLD}{self.stage} sonucu:{RESET} "
            + "  ".join(f"{key}={value}" for key, value in tally.items() if value)
            + f"  sure={self.duration_ms / 1000:.2f} sn"
        )
        if out:
            path = Path(out)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(self)
            payload["summary"] = tally
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{DIM}Rapor: {path}{RESET}")
        return 1 if tally[FAIL] else 0


def _one_line(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def add_common_args(parser: argparse.ArgumentParser, *, live: bool = True) -> None:
    parser.add_argument(
        "--env-file",
        default=".env.cal",
        help="CAL profili (varsayilan .env.cal); sirlar rapora yazilmaz.",
    )
    parser.add_argument("--out", help="Asama JSON raporu.")
    if live:
        parser.add_argument("--material", default=os.getenv("CAL_MATERIAL", ""))
        parser.add_argument("--vendor", default=os.getenv("CAL_VENDOR", ""))
        parser.add_argument("--po", default=os.getenv("CAL_PO", ""))
        parser.add_argument("--invoice", default=os.getenv("CAL_INVOICE", ""))
        parser.add_argument("--wbs", default=os.getenv("CAL_WBS", ""))


def load_profile(path: str, *, required: bool = True) -> Path:
    from dotenv import load_dotenv

    profile = Path(path)
    if not profile.is_absolute():
        profile = PROJECT_ROOT / profile
    if not profile.exists():
        if required:
            raise FileNotFoundError(
                f"CAL profili bulunamadi: {profile}. Once `.env.cal.example` dosyasini "
                ".env.cal olarak kopyalayip doldurun."
            )
        return profile
    # CLI/shell override'lari (ozellikle kontrollu yazma kapilari) profilden
    # daha onceliklidir. `.env` daha sonra yuklense bile bu degerleri ezmez.
    load_dotenv(profile, override=False)
    return profile


def make_report(stage: str, title: str) -> tuple[StageReport, float]:
    print(f"\n{BOLD}{stage} - {title}{RESET}\n  " + "-" * 76)
    return StageReport(stage=stage, title=title), time.perf_counter()


def target_summary(settings: Any, backend: Any | None = None) -> dict[str, Any]:
    parsed = urlparse(settings.sap.base_url)
    connection = (
        backend.connection.describe()
        if backend is not None and hasattr(backend, "connection")
        else {}
    )
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname or "",
        "client": settings.sap.client,
        "system_alias": settings.sap.system_alias,
        "auth": connection.get("auth", settings.sap.auth_mode),
        "dry_run": settings.sap.dry_run,
        "verify_ssl": settings.sap.verify_ssl,
    }


def timed(call: Callable[[], Any], backend: Any | None = None) -> tuple[Any, int, int, Exception | None]:
    before = getattr(backend, "sap_call_count", 0) if backend is not None else 0
    started = time.perf_counter()
    try:
        value = call()
        error = None
    except Exception as exc:  # asama scripti tum kontrolleri gostermeye devam eder
        value, error = None, exc
    elapsed = round((time.perf_counter() - started) * 1000)
    after = getattr(backend, "sap_call_count", before) if backend is not None else before
    return value, elapsed, max(0, after - before), error


def tool_context(settings: Any, backend: Any, *, roles: tuple[str, ...]):
    from robotics_agent.contracts import ActorContext
    from robotics_agent.tools import ToolContext, load_all_tools

    load_all_tools()
    actor = ActorContext.local_operator(
        subject="cal-acceptance",
        tenant=settings.sap.tenant,
        roles=roles,
        company_code=settings.sap.company_code,
        plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )
    return ToolContext(settings=settings, sap=backend, actor=actor)


def call_tool(ctx: Any, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    from robotics_agent.tools import execute_tool

    raw, is_error = execute_tool(name, arguments, ctx)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = {"raw": str(raw)[:1000]}
    return payload if isinstance(payload, dict) else {"value": payload}, is_error


def payload_error(payload: dict[str, Any], is_error: bool) -> str:
    if is_error:
        return _one_line(payload.get("error") or payload)[:500]
    if payload.get("error"):
        return _one_line(payload["error"])[:500]
    return ""


def find_value(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (str, int)) and str(candidate).strip():
                return str(candidate).strip()
        for child in value.values():
            found = find_value(child, *keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_value(child, *keys)
            if found:
                return found
    return ""


def discover_seeds(backend: Any, supplied: dict[str, str], *, max_po_checks: int = 5) -> dict[str, str]:
    """Gercek is referanslarini tahmin etmeden bulur.

    Oncelik CLI/ortam degerlerindedir. Eksikler, gercek PO satirlarindan ve
    bunlarin RSEG referanslarindan tamamlanir. En fazla `max_po_checks` PO icin
    fatura sorgulanir; CAL suresini acik uçlu taramayla harcamaz.
    """
    seeds = {key: str(value or "").strip() for key, value in supplied.items()}
    orders = []
    if not all(seeds.get(k) for k in ("material", "vendor", "po")):
        orders = backend.get_purchase_orders(only_open=False, limit=50)
        seen: set[str] = set()
        orders = [
            order for order in orders
            if order.po_id and not (order.po_id in seen or seen.add(order.po_id))
        ]
        first = orders[0] if orders else None
        if first:
            seeds["material"] = seeds.get("material") or first.material_id
            seeds["vendor"] = seeds.get("vendor") or first.vendor_id
            seeds["po"] = seeds.get("po") or first.po_id
            seeds["wbs"] = seeds.get("wbs") or (first.wbs_element or "")

    if not seeds.get("invoice"):
        candidate_ids = [seeds.get("po", "")]
        candidate_ids.extend(order.po_id for order in orders)
        for po_id in list(dict.fromkeys(p for p in candidate_ids if p))[:max_po_checks]:
            invoices = backend.get_supplier_invoices(po_id=po_id, limit=10)
            if invoices:
                seeds["po"] = po_id
                seeds["invoice"] = invoices[0].invoice_id
                seeds["vendor"] = seeds.get("vendor") or invoices[0].vendor_id
                break
    return {key: seeds.get(key, "") for key in ("material", "vendor", "po", "invoice", "wbs")}


def supplied_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        key: getattr(args, key, "")
        for key in ("material", "vendor", "po", "invoice", "wbs")
    }
