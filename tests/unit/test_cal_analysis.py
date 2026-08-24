from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cal_acceptance import LIVE_STAGES  # noqa: E402
from cal_analysis_lib import (  # noqa: E402
    OPPORTUNITIES,
    evaluate_opportunities,
    infer_join_fields,
    join_profile,
    profile_records,
)


def test_profile_records_never_persists_raw_values() -> None:
    raw = [
        {
            "material_id": "SECRET-MAT-4711",
            "vendor_id": "SECRET-VENDOR-9",
            "moving_avg_price": 123.45,
        },
        {
            "material_id": "SECRET-MAT-4712",
            "vendor_id": "SECRET-VENDOR-9",
            "moving_avg_price": None,
        },
    ]

    profile = profile_records(raw)
    encoded = json.dumps(profile)

    assert profile["raw_values_persisted"] is False
    assert profile["fields"]["material_id"]["distinct_count"] == 2
    assert profile["fields"]["moving_avg_price"]["non_null_count"] == 1
    assert "SECRET-MAT" not in encoded
    assert "SECRET-VENDOR" not in encoded
    assert "123.45" not in encoded


def test_join_profile_measures_overlap_without_exposing_keys() -> None:
    result = join_profile(
        [{"po_id": "4500000010"}, {"po_id": "4500000020"}],
        [{"purchase_order": "4500000020"}, {"purchase_order": "4500000030"}],
        "po",
    )

    assert result["intersection_count"] == 1
    assert result["left_coverage_pct"] == 50.0
    assert "4500000020" not in json.dumps(result)


def test_metadata_fields_are_mapped_to_canonical_join_keys() -> None:
    result = infer_join_fields(
        ["Product", "Plant", "Supplier", "PurchaseOrder", "PurchaseOrderItem"]
    )

    assert result == {
        "material": ["Product"],
        "vendor": ["Supplier"],
        "po": ["PurchaseOrder"],
        "item": ["PurchaseOrderItem"],
        "plant": ["Plant"],
    }


def test_opportunity_gate_opens_only_with_core_and_live_evidence() -> None:
    service_aliases = {
        alias
        for opportunity in OPPORTUNITIES
        for group in opportunity["required_services"]
        for alias in group
    }
    dataset_names = {
        name
        for opportunity in OPPORTUNITIES
        for name in opportunity["required_datasets"]
    }
    join_names = {
        name
        for opportunity in OPPORTUNITIES
        for name in opportunity["required_joins"]
    }
    inventory = {
        "services": [
            {"alias": alias, "contract_ok": alias != "workflow"}
            for alias in service_aliases
        ]
    }
    profile = {
        "datasets": {
            name: {"sample_count": 2, "latency_ms": 500, "error": ""}
            for name in dataset_names
            if name != "workflow_steps"
        },
        "joins": {name: {"intersection_count": 1} for name in join_names},
    }

    passed = evaluate_opportunities(inventory, profile, core_tests_passed=True)
    failed = evaluate_opportunities(inventory, profile, core_tests_passed=False)

    assert passed["development_gate"] == "OPEN"
    assert "sap_gr_ir_reconciliation" in passed["ready_candidates"]
    assert next(
        row for row in passed["candidates"]
        if row["name"] == "sap_approval_bottleneck_monitor"
    )["status"] == "BLOCKED"
    assert failed["development_gate"] == "CLOSED"
    assert {row["status"] for row in failed["candidates"]} == {"DEFERRED"}


def test_live_stage_order_preserves_discovery_before_core_and_scores_last() -> None:
    assert LIVE_STAGES.index("07") < LIVE_STAGES.index("03")
    assert LIVE_STAGES.index("08") < LIVE_STAGES.index("03")
    assert LIVE_STAGES[-1] == "09"
