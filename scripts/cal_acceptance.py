#!/usr/bin/env python3
"""Butun CAL kabul asamalarini fail-fast calistirip JSON/Markdown/JUnit ozetler."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from cal_acceptance_lib import BLOCKED, FAIL, PROJECT_ROOT, SKIP

STAGES = {
    "00": "cal_00_preflight.py",
    "01": "cal_01_connection.py",
    "02": "cal_02_contracts.py",
    "03": "cal_03_read_scenarios.py",
    "04": "cal_04_p2p_scenarios.py",
    "05": "cal_05_security_scenarios.py",
    "06": "cal_06_write_pr.py",
    "07": "cal_07_service_inventory.py",
    "08": "cal_08_data_profile.py",
    "09": "cal_09_tool_opportunities.py",
}

# Envanter/profil erken kaydedilir; gelistirme puani ise tum cekirdek testlerden
# sonra hesaplanir. Boylece test kirmizi olsa bile kesif kaniti kaybolmaz.
LIVE_STAGES = ["01", "02", "07", "08", "03", "04", "05", "06", "09"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pre-cal", action="store_true", help="Yalniz ucretsiz CAL-00 kapisi.")
    mode.add_argument("--live", action="store_true", help="CAL aktifken 01-09 dry-run ve analiz.")
    mode.add_argument("--full", action="store_true", help="00 + canli 01-09 dry-run ve analiz.")
    parser.add_argument("--env-file", default=".env.cal")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--material", default="")
    parser.add_argument("--vendor", default="")
    parser.add_argument("--po", default="")
    parser.add_argument("--invoice", default="")
    parser.add_argument("--wbs", default="")
    parser.add_argument("--execute-write", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--hourly-usd", type=float, default=4.0)
    args = parser.parse_args(argv)

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) if args.out_dir else PROJECT_ROOT / "artifacts" / "cal" / run_stamp
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.live:
        selected = LIVE_STAGES
    elif args.full:
        selected = ["00", *LIVE_STAGES]
    else:
        selected = ["00"]

    started = time.perf_counter()
    reports: list[dict] = []
    exit_code = 0
    print(f"CAL kabul kosusu: {', '.join(selected)}", flush=True)
    print(f"Rapor klasoru : {out_dir}", flush=True)

    for stage in selected:
        report_path = out_dir / f"{stage}.json"
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / STAGES[stage]),
            "--env-file", args.env_file,
            "--out", str(report_path),
        ]
        if stage != "00":
            for key in ("material", "vendor", "po", "invoice", "wbs"):
                if value := getattr(args, key):
                    command.extend([f"--{key}", value])
        if stage == "06" and args.execute_write:
            command.extend(["--execute-write", "--confirm", args.confirm])

        print(f"\n{'=' * 88}\nCAL-{stage} basliyor\n{'=' * 88}", flush=True)
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if report_path.exists():
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
        else:
            reports.append({
                "stage": f"CAL-{stage}", "title": STAGES[stage],
                "duration_ms": 0,
                "cases": [{
                    "name": "Asama raporu", "status": FAIL,
                    "detail": f"Script exit={result.returncode}; JSON raporu uretilmedi.",
                }],
                "summary": {FAIL: 1},
            })
        if result.returncode:
            exit_code = 1
            # Baglanti/kontrat kirmiziyken sonraki sorgular CAL suresini bosa harcar.
            if not args.continue_on_failure:
                print("\nFail-fast: hata giderilmeden sonraki asamalar calistirilmadi.")
                break

    elapsed = time.perf_counter() - started
    compute_cost = elapsed / 3600 * max(0.0, args.hourly_usd)
    _write_markdown(out_dir / "summary.md", reports, elapsed, compute_cost)
    _write_junit(out_dir / "junit.xml", reports)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "started_at": run_stamp,
                "duration_seconds": round(elapsed, 2),
                "estimated_runtime_cost_usd": round(compute_cost, 4),
                "note": "Aktivasyon/suspend ve kalici disk ucreti bu script suresine dahil degildir.",
                "stages": reports,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nToplam script suresi : {elapsed:.2f} sn ({elapsed / 60:.2f} dk)")
    print(f"Kosudaki compute payi: ~${compute_cost:.4f} @ ${args.hourly_usd:.2f}/saat")
    print("Not: CAL aktivasyonu ve disk maliyeti bu olcume dahil degildir.")
    print(f"Ozet: {out_dir / 'summary.md'}")
    return exit_code


def _write_markdown(path: Path, reports: list[dict], elapsed: float, cost: float) -> None:
    lines = [
        "# SAP CAL kabul sonucu",
        "",
        f"- Script süresi: {elapsed:.2f} saniye",
        f"- Bu süreye düşen tahmini compute: ${cost:.4f}",
        "- CAL aktivasyonu, suspend ve disk ücreti bu ölçümün dışındadır.",
        "",
        "| Aşama | PASS | WARN | FAIL | BLOCKED | Süre |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        tally = report.get("summary") or _tally(report)
        lines.append(
            f"| {report.get('stage')} {report.get('title', '')} "
            f"| {tally.get('PASS', 0)} | {tally.get('WARN', 0)} "
            f"| {tally.get('FAIL', 0)} | {tally.get('BLOCKED', 0)} "
            f"| {report.get('duration_ms', 0) / 1000:.2f} sn |"
        )
    lines.extend(["", "## Başarısız veya bloke kontroller", ""])
    problems = [
        (report.get("stage", ""), case)
        for report in reports
        for case in report.get("cases", [])
        if case.get("status") in {FAIL, BLOCKED}
    ]
    if not problems:
        lines.append("Yok.")
    else:
        for stage, case in problems:
            lines.append(
                f"- **{stage} / {case.get('status')} / {case.get('name')}**: "
                f"{case.get('detail', '')}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_junit(path: Path, reports: list[dict]) -> None:
    cases = [case for report in reports for case in report.get("cases", [])]
    suite = ElementTree.Element(
        "testsuite",
        name="sap-cal-acceptance",
        tests=str(len(cases)),
        failures=str(sum(c.get("status") == FAIL for c in cases)),
        skipped=str(sum(c.get("status") in {BLOCKED, SKIP} for c in cases)),
    )
    for report in reports:
        for case in report.get("cases", []):
            node = ElementTree.SubElement(
                suite,
                "testcase",
                classname=report.get("stage", "CAL"),
                name=case.get("name", "case"),
                time=f"{case.get('duration_ms', 0) / 1000:.3f}",
            )
            status = case.get("status")
            if status == FAIL:
                ElementTree.SubElement(node, "failure", message=case.get("detail", ""))
            elif status in {BLOCKED, SKIP}:
                ElementTree.SubElement(node, "skipped", message=case.get("detail", ""))
            elif status == "WARN":
                ElementTree.SubElement(node, "system-out").text = case.get("detail", "")
    ElementTree.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _tally(report: dict) -> dict[str, int]:
    return {
        status: sum(case.get("status") == status for case in report.get("cases", []))
        for status in ("PASS", "WARN", "FAIL", "BLOCKED", "SKIP")
    }


if __name__ == "__main__":
    raise SystemExit(main())
