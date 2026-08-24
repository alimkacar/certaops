#!/usr/bin/env python3
"""Canli CAL kanitlarindan puanli tool adaylari ve kalici gelistirme notu uretir."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cal_acceptance_lib import BLOCKED, WARN, add_common_args, make_report
from cal_analysis_lib import (
    evaluate_opportunities,
    load_json,
    render_opportunity_markdown,
    write_json,
)

CORE_STAGES = ("01", "02", "03", "04", "05", "06")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    report, started = make_report("CAL-09", "Tool firsati puanlama ve notlar")
    run_dir = Path(args.out).parent if args.out else Path("artifacts/cal")
    inventory_path = run_dir / "service_inventory.json"
    profile_path = run_dir / "data_profile.json"

    inputs_ok = inventory_path.exists() and profile_path.exists()
    report.check(
        "Analiz girdileri mevcut",
        inputs_ok,
        f"inventory={inventory_path.exists()}, profile={profile_path.exists()}",
    )
    if not inputs_ok:
        return report.finish(started, out=args.out or "artifacts/cal/09-opportunities.json")

    stage_reports = {
        stage: _load_stage(run_dir / f"{stage}.json")
        for stage in CORE_STAGES
    }
    core_tests_passed = all(
        stage_reports[stage]
        and not any(
            case.get("status") == "FAIL"
            for case in stage_reports[stage].get("cases", [])
        )
        for stage in CORE_STAGES
    )
    report.add(
        "Cekirdek CAL gelistirme kapisi",
        "PASS" if core_tests_passed else BLOCKED,
        (
            "01-06 asamalarinda FAIL yok"
            if core_tests_passed
            else "01-06 raporlarindan biri eksik veya FAIL; yeni tool gelistirilmeyecek"
        ),
    )

    result = evaluate_opportunities(
        load_json(inventory_path),
        load_json(profile_path),
        core_tests_passed=core_tests_passed,
    )
    for candidate in result["candidates"]:
        status = candidate["status"]
        report.add(
            candidate["name"],
            "PASS" if status == "READY" else (BLOCKED if status == "BLOCKED" else WARN),
            f"durum={status}, puan={candidate['score']}",
        )

    opportunity_path = run_dir / "tool_opportunities.json"
    notes_path = run_dir / "development_notes.md"
    write_json(opportunity_path, result)
    notes_path.write_text(render_opportunity_markdown(result), encoding="utf-8")
    report.add(
        "Gelistirme notlari kaydedildi",
        "PASS",
        f"{opportunity_path}; {notes_path}",
    )
    return report.finish(started, out=args.out or "artifacts/cal/09-opportunities.json")


def _load_stage(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
