#!/usr/bin/env python3
"""Run preflight, contract and 24-tool acceptance for ABAP Trial 2025."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PASS, WARN, FAIL, BLOCKED = "PASS", "WARN", "FAIL", "BLOCKED"
WRITE_CONFIRMATION = "WRITE-ABAP-TRIAL-PR"


@dataclass
class Case:
    name: str
    status: str
    detail: str = ""
    duration_ms: int = 0


@dataclass
class Report:
    mode: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: int = 0
    cases: list[Case] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: Any = "", duration_ms: int = 0) -> None:
        text = " ".join(str(detail).split()) if detail not in (None, "") else ""
        self.cases.append(Case(name, status, text[:1000], duration_ms))
        print(f"[{status:<7}] {name}" + (f" - {text[:300]}" if text else ""))

    def summary(self) -> dict[str, int]:
        return {
            status: sum(case.status == status for case in self.cases)
            for status in (PASS, WARN, FAIL, BLOCKED)
        }


def _run(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 300):
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return result, round((time.perf_counter() - started) * 1000)


def _last_output(result: subprocess.CompletedProcess[str]) -> str:
    lines = (result.stdout + "\n" + result.stderr).strip().splitlines()
    return lines[-1] if lines else f"exit={result.returncode}"


def _profile_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(
            f"Profile not found: {path}. Copy .env.abap-trial.example to .env.abap-trial."
        )
    values = dotenv_values(path)
    env = os.environ.copy()
    for key, value in values.items():
        # A non-empty shell value is an intentional override. Empty values
        # commonly leak from the API Hub profile and must not erase this
        # profile's required client/host settings.
        if value is not None and not env.get(key):
            env[key] = value
    return env


def _load_settings(env: dict[str, str]):
    original = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        from robotics_agent.config import Settings

        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(original)


def _preflight(report: Report, out_dir: Path, *, skip_tests: bool) -> None:
    from robotics_agent.adapters.ecc.capabilities import ECC_CAPABILITY_MANIFEST
    from robotics_agent.tools import load_all_tools
    from robotics_agent.tools.registry import REGISTRY

    required_files = [
        PROJECT_ROOT / "infra" / "abap-trial" / "cloudformation.yaml",
        PROJECT_ROOT / ".env.abap-trial.example",
        PROJECT_ROOT / "config" / "abap_trial_scenario.json",
        PROJECT_ROOT / "config" / "aws_abap_trial_operator_policy.json",
        PROJECT_ROOT / "docs" / "ECC_ABAP_REQUIREMENTS.md",
    ]
    missing = [str(path.relative_to(PROJECT_ROOT)) for path in required_files if not path.exists()]
    report.add("Infrastructure and contract files", PASS if not missing else FAIL, missing or "ready")

    load_all_tools()
    report.add("Registered tool inventory", PASS if len(REGISTRY) == 24 else FAIL, f"count={len(REGISTRY)}")
    services = {cap.service_path for cap in ECC_CAPABILITY_MANIFEST.values()}
    report.add(
        "ABAP compatibility service contract",
        PASS if len(ECC_CAPABILITY_MANIFEST) == 15 and len(services) == 8 else FAIL,
        f"aliases={len(ECC_CAPABILITY_MANIFEST)}, services={len(services)}",
    )

    template = required_files[0].read_text(encoding="utf-8") if not missing else ""
    guards = {
        "no ingress rules": "SecurityGroupIngress" not in template,
        "IMDSv2": "HttpTokens: required" in template,
        "encrypted disk": "Encrypted: true" in template,
        "disk deletion": "DeleteOnTermination: true" in template,
        "auto termination": "InstanceInitiatedShutdownBehavior: terminate" in template,
        "manual SAP license": "SAP_DEVELOPER_LICENSE_ACCEPTED" in template,
    }
    report.add(
        "AWS cost and security guards",
        PASS if all(guards.values()) else FAIL,
        guards,
    )

    plan_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "abap_trial_aws.py"),
        "--hours",
        "6",
        "--budget-usd",
        "3",
        "plan",
        "--offline",
    ]
    result, elapsed = _run(plan_command)
    report.add("Offline AWS cost plan", PASS if result.returncode == 0 else FAIL, _last_output(result), elapsed)

    fixture_json = out_dir / "fixture-report.json"
    fixture_md = out_dir / "fixture-report.md"
    fixture_command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "abap_trial_fixture.py"),
        "--out",
        str(fixture_json),
        "--markdown",
        str(fixture_md),
    ]
    result, elapsed = _run(fixture_command)
    report.add("Realistic P2P fixture", PASS if result.returncode == 0 else FAIL, _last_output(result), elapsed)
    if fixture_json.exists():
        report.artifacts["fixture_json"] = str(fixture_json)
        report.artifacts["fixture_markdown"] = str(fixture_md)

    report.add(
        "AWS CLI available",
        PASS if shutil.which("aws") else BLOCKED,
        shutil.which("aws") or "Install/configure AWS CLI before online plan.",
    )
    report.add(
        "Session Manager plugin available",
        PASS if shutil.which("session-manager-plugin") else BLOCKED,
        shutil.which("session-manager-plugin")
        or "Install the AWS Session Manager plugin before opening the private tunnel.",
    )
    report.add(
        "Docker Hub authentication",
        BLOCKED,
        "Performed interactively on the ephemeral EC2 host; credentials are never stored in this repository.",
    )
    report.add(
        "Eight Z OData services deployed in ABAP Trial",
        BLOCKED,
        "Requires the running ABAP system; live contract probing will verify every entity set and field.",
    )

    if skip_tests:
        report.add("Local quality tests", WARN, "skipped by --skip-tests")
        return
    commands = [
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_abap_trial.py",
            "tests/unit/test_ecc_backend.py",
            "tests/unit/test_cal_analysis.py",
            "-q",
        ],
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "scripts/abap_trial_aws.py",
            "scripts/abap_trial_fixture.py",
            "scripts/abap_trial_acceptance.py",
            "tests/unit/test_abap_trial.py",
        ],
    ]
    labels = ["Selected pytest gate", "Ruff quality gate"]
    for label, command in zip(labels, commands, strict=True):
        result, elapsed = _run(command)
        report.add(label, PASS if result.returncode == 0 else FAIL, _last_output(result), elapsed)


def _validate_live_profile(report: Report, settings: Any) -> bool:
    parsed = urlparse(settings.sap.base_url)
    checks = {
        "backend is ECC compatibility adapter": settings.sap.backend == "ecc",
        "SSM tunnel host only": parsed.hostname in {"127.0.0.1", "localhost"},
        "HTTPS port is 50001": parsed.scheme == "https" and parsed.port == 50001,
        "ABAP development client": settings.sap.client == "001",
        "OData V2": settings.sap.odata_version in {"v2", "auto"},
        "basic auth only for ephemeral development": settings.sap.auth_mode == "basic",
        "password configured": bool(settings.sap.password),
        "read stage is dry-run": settings.sap.dry_run,
        "target host allowlisted": parsed.hostname in settings.security.allowed_sap_hosts,
    }
    report.add("Safe live profile", PASS if all(checks.values()) else FAIL, checks)
    problems = settings.sap.validate()
    report.add("SAP profile structural validation", PASS if not problems else FAIL, problems or "valid")
    return all(checks.values()) and not problems


def _read_acceptance(report: Report, out_dir: Path, profile: Path) -> None:
    try:
        env = _profile_env(profile)
    except FileNotFoundError as exc:
        report.add("Private ABAP Trial profile", FAIL, exc)
        return
    settings = _load_settings(env)
    if not _validate_live_profile(report, settings):
        return

    original = os.environ.copy()
    backend = None
    try:
        os.environ.clear()
        os.environ.update(env)
        from robotics_agent.sap import build_backend

        backend = build_backend(settings)
        started = time.perf_counter()
        health = backend.ping()
        report.add(
            "Real ABAP/OData connection",
            PASS if health.get("status") == "ok" else FAIL,
            {key: value for key, value in health.items() if key not in {"password", "secret"}},
            round((time.perf_counter() - started) * 1000),
        )
        probes = backend.probe_capabilities()
        contract_path = out_dir / "contracts.json"
        contract_path.write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
        report.artifacts["contracts"] = str(contract_path)
        failures = [
            row.get("alias", "?") for row in probes
            if not row.get("available", True) or not row.get("contract_ok")
        ]
        report.add(
            "All ABAP compatibility contracts",
            PASS if len(probes) == 15 and not failures else FAIL,
            f"checked={len(probes)}, failed={failures}",
        )
    except Exception as exc:
        report.add("ABAP contract probe", FAIL, f"{type(exc).__name__}: {exc}")
        return
    finally:
        if backend is not None:
            backend.close()
        os.environ.clear()
        os.environ.update(original)

    sweep_path = out_dir / "tool-sweep-read.json"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "sap_tool_sweep.py"),
        "--material",
        env.get("SAP_INTEGRATION_MATERIAL", ""),
        "--vendor",
        env.get("SAP_INTEGRATION_VENDOR", ""),
        "--po",
        env.get("SAP_INTEGRATION_PO", ""),
        "--invoice",
        env.get("SAP_INTEGRATION_INVOICE", ""),
        "--wbs",
        env.get("SAP_INTEGRATION_WBS", ""),
        "--no-discover",
        "--negative",
        "--roles",
        "PURCHASER,AUDITOR",
        "--strict",
        "--out",
        str(sweep_path),
    ]
    result, elapsed = _run(command, env=env, timeout=900)
    report.artifacts["read_sweep"] = str(sweep_path)
    if not sweep_path.exists():
        report.add("24-tool read sweep", FAIL, _last_output(result), elapsed)
        return
    sweep = json.loads(sweep_path.read_text(encoding="utf-8"))
    rows = sweep.get("results") or []
    fatal = [row["tool"] for row in rows if row.get("status") in {"SAP HATA", "COKTU"}]
    unexpected_skips = [
        row["tool"] for row in rows
        if row.get("status") == "ATLANDI" and row.get("tool") != "sap_pr_submit"
    ]
    negatives = sweep.get("negative") or []
    report.add(
        "24-tool read/dry-run sweep",
        PASS if result.returncode == 0 and len(rows) == 24 and not fatal and not unexpected_skips else FAIL,
        f"count={len(rows)}, fatal={fatal}, unexpected_skips={unexpected_skips}",
        elapsed,
    )
    report.add(
        "VIEWER write denial",
        PASS if negatives and all(row.get("pass") for row in negatives) else FAIL,
        f"negative_cases={len(negatives)}",
    )


def _write_acceptance(
    report: Report, out_dir: Path, profile: Path, *, confirmation: str
) -> None:
    if confirmation != WRITE_CONFIRMATION:
        report.add(
            "Explicit real-write confirmation",
            FAIL,
            f"Use --confirm {WRITE_CONFIRMATION}; one test purchase requisition will be created.",
        )
        return
    try:
        env = _profile_env(profile)
    except FileNotFoundError as exc:
        report.add("Private ABAP Trial profile", FAIL, exc)
        return
    env["SAP_DRY_RUN"] = "false"
    env["SAP_INTEGRATION_ALLOW_WRITE"] = "1"
    sweep_path = out_dir / "tool-sweep-write.json"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "sap_tool_sweep.py"),
        "--material",
        env.get("SAP_INTEGRATION_MATERIAL", ""),
        "--no-discover",
        "--allow-write",
        "--negative",
        "--roles",
        "PURCHASER,AUDITOR",
        "--only",
        "sap_pr_submit",
        "--only",
        "sap_reconcile_execution",
        "--strict",
        "--out",
        str(sweep_path),
    ]
    result, elapsed = _run(command, env=env, timeout=600)
    report.artifacts["write_sweep"] = str(sweep_path)
    if not sweep_path.exists():
        report.add("Real PR write and reconciliation", FAIL, _last_output(result), elapsed)
        return
    sweep = json.loads(sweep_path.read_text(encoding="utf-8"))
    rows = {row.get("tool"): row for row in sweep.get("results") or []}
    submit_ok = rows.get("sap_pr_submit", {}).get("status") == "OK"
    reconcile_ok = rows.get("sap_reconcile_execution", {}).get("status") in {"OK", "BOS"}
    negatives = sweep.get("negative") or []
    report.add(
        "Real PR write and reconciliation",
        PASS if result.returncode == 0 and submit_ok and reconcile_ok else FAIL,
        {
            "submit": rows.get("sap_pr_submit", {}).get("status", "missing"),
            "reconcile": rows.get("sap_reconcile_execution", {}).get("status", "missing"),
        },
        elapsed,
    )
    report.add(
        "Write run retained role separation",
        PASS if negatives and all(row.get("pass") for row in negatives) else FAIL,
        f"negative_cases={len(negatives)}",
    )


def _write_summary(out_dir: Path, report: Report) -> None:
    summary = report.summary()
    payload = asdict(report)
    payload["summary"] = summary
    json_path = out_dir / "summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# ABAP Trial acceptance: {report.mode}",
        "",
        f"- PASS: {summary[PASS]}",
        f"- WARN: {summary[WARN]}",
        f"- FAIL: {summary[FAIL]}",
        f"- BLOCKED: {summary[BLOCKED]}",
        f"- Duration: {report.duration_ms / 1000:.2f} seconds",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for case in report.cases:
        lines.append(f"| {case.name} | {case.status} | {case.detail} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Summary: {out_dir / 'summary.md'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--read", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--full-read", action="store_true", help="Preflight followed by live read.")
    parser.add_argument("--env-file", default=".env.abap-trial")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)

    selected_mode = "write" if args.write else "read" if args.read else "full-read" if args.full_read else "preflight"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) if args.out_dir else PROJECT_ROOT / "artifacts" / "abap-trial" / stamp
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(args.env_file)
    if not profile.is_absolute():
        profile = PROJECT_ROOT / profile

    report = Report(mode=selected_mode)
    started = time.perf_counter()
    if selected_mode in {"preflight", "full-read"}:
        _preflight(report, out_dir, skip_tests=args.skip_tests)
    if selected_mode in {"read", "full-read"}:
        _read_acceptance(report, out_dir, profile)
    if selected_mode == "write":
        _write_acceptance(report, out_dir, profile, confirmation=args.confirm)
    report.duration_ms = round((time.perf_counter() - started) * 1000)
    _write_summary(out_dir, report)
    return 1 if report.summary()[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
