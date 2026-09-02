from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import abap_trial_acceptance as acceptance  # noqa: E402
import abap_trial_aws as aws_plan  # noqa: E402
import abap_trial_fixture as fixture  # noqa: E402


def test_default_aws_plan_fits_three_dollar_guard() -> None:
    estimate = aws_plan.estimate_cost()
    assert estimate.instance_type == "r6i.xlarge"
    assert estimate.hours == 6
    assert estimate.volume_gib == 200
    assert estimate.instance_usd == pytest.approx(1.512)
    assert estimate.ebs_usd == pytest.approx(0.1333, abs=0.0001)
    assert estimate.public_ipv4_usd == pytest.approx(0.03)
    assert estimate.subtotal_usd < estimate.guarded_budget_usd == 3.0


@pytest.mark.parametrize(
    ("instance_type", "hours", "volume_gib"),
    [
        ("t3.micro", 6, 200),
        ("r6i.xlarge", 1, 200),
        ("r6i.xlarge", 9, 200),
        ("r6i.xlarge", 6, 100),
        ("r6i.xlarge", 6, 300),
    ],
)
def test_aws_plan_rejects_unsafe_shapes(
    instance_type: str, hours: int, volume_gib: int
) -> None:
    with pytest.raises(ValueError):
        aws_plan.validate_request(
            instance_type=instance_type, hours=hours, volume_gib=volume_gib
        )


def test_deploy_is_refused_before_any_aws_call(monkeypatch, capsys) -> None:
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("AWS must not be called")

    monkeypatch.setattr(aws_plan, "_aws", forbidden)
    rc = aws_plan.main(["deploy"])
    assert rc == 2
    assert not called
    assert aws_plan.DEPLOY_CONFIRMATION in capsys.readouterr().err


def test_cloudformation_contains_cost_and_network_guards() -> None:
    template = (ROOT / "infra" / "abap-trial" / "cloudformation.yaml").read_text()
    assert "SecurityGroupIngress" not in template
    assert "HttpTokens: required" in template
    assert "Encrypted: true" in template
    assert "DeleteOnTermination: true" in template
    assert "InstanceInitiatedShutdownBehavior: terminate" in template
    assert "AutoTerminateHours" in template
    assert "SAP_DEVELOPER_LICENSE_ACCEPTED" in template
    assert "docker login" in template


def test_operator_policy_is_region_and_resource_bounded() -> None:
    policy = json.loads((ROOT / "config" / "aws_abap_trial_operator_policy.json").read_text())
    rendered = json.dumps(policy)
    assert "AdministratorAccess" not in rendered
    assert "certaops-abap-trial" in rendered
    assert "us-east-1" in rendered
    assert "iam:PassRole" in rendered
    assert "ssm:StartSession" in rendered
    assert "SignInLocalDevelopmentAccess" not in rendered

    launch = next(
        statement
        for statement in policy["Statement"]
        if statement["Sid"] == "LaunchOnlyApprovedTrialInstanceTypes"
    )
    assert launch["Condition"]["StringEquals"]["ec2:InstanceType"] == [
        "r6i.xlarge",
        "r6i.2xlarge",
    ]

    managed = next(
        statement
        for statement in policy["Statement"]
        if statement["Sid"] == "ManageOnlyTaggedTrialResources"
    )
    assert managed["Condition"]["StringEquals"]["ec2:ResourceTag/Project"] == "CertaOps"
    assert (
        managed["Condition"]["StringEquals"]["ec2:ResourceTag/Environment"]
        == "ephemeral-test"
    )


def _fixture_payload() -> dict:
    return json.loads((ROOT / "config" / "abap_trial_scenario.json").read_text())


def test_realistic_fixture_covers_all_ecc_contracts() -> None:
    report = fixture.build_report(_fixture_payload())
    assert report["entity_set_count"] >= 20
    assert report["row_count"] >= report["entity_set_count"]
    assert report["summary"] == {"PASS": 7, "FAIL": 0}


def test_fixture_validator_detects_missing_critical_property() -> None:
    payload = copy.deepcopy(_fixture_payload())
    for row in payload["entity_sets"]["MaterialSet"]:
        row.pop("Currency")
    report = fixture.build_report(payload)
    assert report["summary"]["FAIL"] == 1
    finding = next(
        row for row in report["findings"] if row["check"] == "contract.critical_properties"
    )
    assert "Currency" in finding["detail"]


def test_fixture_validator_detects_broken_business_scenario() -> None:
    payload = copy.deepcopy(_fixture_payload())
    for row in payload["entity_sets"]["SupplyDemandSet"]:
        row["Quantity"] = abs(row["Quantity"])
    report = fixture.build_report(payload)
    assert any(
        row["check"] == "scenario.real_shortage" and row["status"] == "FAIL"
        for row in report["findings"]
    )


def test_private_profile_keeps_shell_override(tmp_path, monkeypatch) -> None:
    profile = tmp_path / ".env.abap-trial"
    profile.write_text("SAP_PASSWORD=profile-password\nSAP_CLIENT=001\n")
    monkeypatch.setenv("SAP_PASSWORD", "shell-password")
    env = acceptance._profile_env(profile)
    assert env["SAP_PASSWORD"] == "shell-password"
    assert env["SAP_CLIENT"] == "001"


def test_write_stage_requires_exact_confirmation(tmp_path) -> None:
    report = acceptance.Report(mode="write")
    acceptance._write_acceptance(
        report,
        tmp_path,
        tmp_path / "missing-profile",
        confirmation="almost",
    )
    assert report.summary()[acceptance.FAIL] == 1
    assert acceptance.WRITE_CONFIRMATION in report.cases[0].detail
