#!/usr/bin/env python3
"""Eval setini calistirir ve rehber Madde 12 raporunu yazdirir.

Kullanim:

    python scripts/run_eval.py            # insan okunur rapor
    python scripts/run_eval.py --json     # CI/gecmis takibi icin JSON
    python scripts/run_eval.py --strict   # guvenlik kategorisinde tek hata = exit 1

`pytest tests/eval` ayni vakalari esiklerle calistirir. Bu betik ayni veriyi
insan okunur bir raporla gosterir; model/prompt/tool degisikliklerini
karsilastirmak icin ciktiyi saklayin.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from eval.cases import SECURITY_CATEGORIES  # noqa: E402
from eval.harness import EvalReport  # noqa: E402
from eval.test_eval_suite import (  # noqa: E402
    run_duplicate_write,
    run_missing_parameter,
    run_prompt_injection,
    run_result_reduction,
    run_sensitive_leakage,
    run_tenant_boundary,
    run_tool_selection,
    run_unauthorized,
    run_write_flow,
)

from robotics_agent.adapters.bpa import ApprovalRequest, LocalApprovalGateway  # noqa: E402
from robotics_agent.config import Settings  # noqa: E402
from robotics_agent.contracts import ActorContext  # noqa: E402
from robotics_agent.core import approval_payload_for  # noqa: E402


def make_settings(tmp_path: Path) -> Settings:
    """Izole durum dizini: eval gercek `state/` dizinine dokunmaz."""
    settings = Settings()
    object.__setattr__(settings, "output_dir", tmp_path / "out")
    object.__setattr__(settings.state, "dir", tmp_path / "state")
    settings.ensure_dirs()
    return settings


def make_grant_approval():
    """`conftest.grant_approval` fixture'inin betik icin esdegeri."""
    approver = ActorContext(
        subject="onaylayan@firma.test",
        tenant="100",
        roles=("APPROVER",),
        plants=frozenset({"1100"}),
        auth_method="eval",
    )

    def _grant(ctx, *, tool: str, arguments: dict, max_value: float | None = None) -> str:
        gateway = LocalApprovalGateway(ctx.approvals, ttl_minutes=30)
        request = ApprovalRequest(
            tool=tool,
            payload=approval_payload_for(arguments),
            tenant=approver.tenant,
            requested_by=ctx.actor.subject if ctx.actor else "",
            subject_line=f"{tool} onayi",
            diff=[],
            max_value=max_value,
        )
        task = gateway.request(request)
        record = gateway.complete(
            task_id=task["task_id"], approvers=[approver], request=request
        )
        return record.approval_id

    return _grant


def build_report(tmp_path: Path) -> EvalReport:
    report = EvalReport()
    grant_approval = make_grant_approval()

    # Her kategori kendi temiz ayar/durum ornegiyle calisir; bir vakanin
    # yazdigi belge digerinin sonucunu etkilemesin.
    run_tool_selection(report)
    run_sensitive_leakage(report)
    run_prompt_injection(report, make_settings(tmp_path / "injection"))
    run_unauthorized(report, make_settings(tmp_path / "unauth"))
    run_tenant_boundary(report, make_settings(tmp_path / "tenant"))
    run_missing_parameter(report, make_settings(tmp_path / "param"))
    run_write_flow(report, make_settings(tmp_path / "write"), grant_approval)
    run_duplicate_write(report, make_settings(tmp_path / "dup"), grant_approval)
    run_result_reduction(
        report, lambda base, **kw: _sized_settings(base, **kw), tmp_path / "reduction"
    )
    return report


def _sized_settings(base: Path, **overrides) -> Settings:
    settings = make_settings(base)
    for key, value in overrides.items():
        target = settings
        if "." in key:
            section, key = key.split(".", 1)
            target = getattr(settings, section)
        object.__setattr__(target, key, value)
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description="CertaOps eval kosucusu")
    parser.add_argument("--json", action="store_true", help="Raporu JSON olarak yazdir")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Guvenlik kategorisinde tek hata bile cikis kodunu 1 yapar",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="certaops-eval-") as tmp:
        report = build_report(Path(tmp))

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())

    if args.strict:
        for category in SECURITY_CATEGORIES:
            if report.accuracy(category) < 100.0:
                print(
                    f"HATA: guvenlik kategorisi '{category}' %100 degil "
                    f"(%{report.accuracy(category)}).",
                    file=sys.stderr,
                )
                return 1
    return 0 if not report.failures() else 1


if __name__ == "__main__":
    raise SystemExit(main())
