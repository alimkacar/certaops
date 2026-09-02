#!/usr/bin/env python3
"""Plan, deploy and remove the bounded AWS host for ABAP Developer Trial.

The default command is read-only. A paid resource can only be created with the
``deploy`` subcommand and the exact ``--confirm DEPLOY-ABAP-TRIAL`` phrase.
The CloudFormation template adds a second guard: the instance shuts itself down
after the configured window, which terminates the EC2 instance and deletes its
root volume.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "infra" / "abap-trial" / "cloudformation.yaml"

DEFAULT_REGION = "us-east-1"
DEFAULT_STACK = "certaops-abap-trial"
DEFAULT_INSTANCE_TYPE = "r6i.xlarge"
DEFAULT_VOLUME_GIB = 200
DEFAULT_HOURS = 6
DEFAULT_BUDGET_USD = 3.0

# 2026-08-25 public us-east-1 reference prices. They are estimates, not an
# invoice. Callers can override the compute rate after checking AWS pricing.
INSTANCE_HOURLY_USD = {
    "r6i.xlarge": 0.252,
    "r6i.2xlarge": 0.504,
}
INSTANCE_VCPU = {"r6i.xlarge": 4, "r6i.2xlarge": 8}
EBS_GP3_GIB_MONTH_USD = 0.08
PUBLIC_IPV4_HOURLY_USD = 0.005
MONTH_HOURS = 720

DEPLOY_CONFIRMATION = "DEPLOY-ABAP-TRIAL"
DESTROY_CONFIRMATION = "DESTROY-ABAP-TRIAL"
STANDARD_QUOTA_CODE = "L-1216C47A"


class AwsCliError(RuntimeError):
    """AWS CLI could not complete a requested operation."""


@dataclass(frozen=True)
class CostEstimate:
    instance_usd: float
    ebs_usd: float
    public_ipv4_usd: float
    subtotal_usd: float
    guarded_budget_usd: float
    hours: int
    volume_gib: int
    instance_type: str


def estimate_cost(
    *,
    instance_type: str = DEFAULT_INSTANCE_TYPE,
    hours: int = DEFAULT_HOURS,
    volume_gib: int = DEFAULT_VOLUME_GIB,
    budget_usd: float = DEFAULT_BUDGET_USD,
    instance_hourly_usd: float | None = None,
) -> CostEstimate:
    """Return the bounded compute + root disk + public IPv4 estimate."""
    validate_request(instance_type=instance_type, hours=hours, volume_gib=volume_gib)
    hourly = (
        INSTANCE_HOURLY_USD[instance_type]
        if instance_hourly_usd is None
        else instance_hourly_usd
    )
    if hourly <= 0:
        raise ValueError("instance_hourly_usd must be positive")
    if budget_usd <= 0:
        raise ValueError("budget_usd must be positive")
    instance = hourly * hours
    ebs = EBS_GP3_GIB_MONTH_USD * volume_gib * hours / MONTH_HOURS
    ipv4 = PUBLIC_IPV4_HOURLY_USD * hours
    subtotal = instance + ebs + ipv4
    return CostEstimate(
        instance_usd=round(instance, 4),
        ebs_usd=round(ebs, 4),
        public_ipv4_usd=round(ipv4, 4),
        subtotal_usd=round(subtotal, 4),
        guarded_budget_usd=round(budget_usd, 2),
        hours=hours,
        volume_gib=volume_gib,
        instance_type=instance_type,
    )


def validate_request(*, instance_type: str, hours: int, volume_gib: int) -> None:
    if instance_type not in INSTANCE_HOURLY_USD:
        raise ValueError(f"unsupported instance type: {instance_type}")
    if not 2 <= hours <= 8:
        raise ValueError("hours must be between 2 and 8")
    if not 180 <= volume_gib <= 250:
        raise ValueError("volume_gib must be between 180 and 250")


def _aws(args: list[str], *, region: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if shutil.which("aws") is None:
        raise AwsCliError("AWS CLI is not installed or is not on PATH.")
    command = ["aws", *args, "--region", region, "--output", "json"]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise AwsCliError(message[-1] if message else f"AWS CLI exit={result.returncode}")
    return result


def _aws_json(args: list[str], *, region: str) -> Any:
    result = _aws(args, region=region)
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AwsCliError("AWS CLI returned invalid JSON.") from exc


def _stack(region: str, stack_name: str) -> dict[str, Any] | None:
    result = _aws(
        ["cloudformation", "describe-stacks", "--stack-name", stack_name],
        region=region,
        check=False,
    )
    if result.returncode:
        blob = f"{result.stdout}\n{result.stderr}"
        if "does not exist" in blob:
            return None
        raise AwsCliError(blob.strip().splitlines()[-1])
    payload = json.loads(result.stdout or "{}")
    stacks = payload.get("Stacks") or []
    return stacks[0] if stacks else None


def _resolve_network(region: str) -> tuple[str, str]:
    vpcs = _aws_json(
        ["ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true"],
        region=region,
    ).get("Vpcs", [])
    if not vpcs:
        raise AwsCliError(f"No default VPC was found in {region}.")
    vpc_id = vpcs[0]["VpcId"]
    subnets = _aws_json(
        [
            "ec2",
            "describe-subnets",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
            "Name=state,Values=available",
        ],
        region=region,
    ).get("Subnets", [])
    if not subnets:
        raise AwsCliError(f"No available subnet was found in default VPC {vpc_id}.")
    # The template explicitly associates a public IP. Prefer the subnet with
    # the most free addresses; no inbound security-group rule is created.
    subnet = max(subnets, key=lambda row: int(row.get("AvailableIpAddressCount", 0)))
    return vpc_id, subnet["SubnetId"]


def _quota(region: str) -> float:
    payload = _aws_json(
        [
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "ec2",
            "--quota-code",
            STANDARD_QUOTA_CODE,
        ],
        region=region,
    )
    return float((payload.get("Quota") or {}).get("Value") or 0)


def _identity_is_ready(region: str) -> bool:
    payload = _aws_json(["sts", "get-caller-identity"], region=region)
    return bool(payload.get("Account") and payload.get("Arn"))


def _validate_template_online(region: str) -> bool:
    payload = _aws_json(
        [
            "cloudformation",
            "validate-template",
            "--template-body",
            f"file://{TEMPLATE}",
        ],
        region=region,
    )
    parameters = {row.get("ParameterKey") for row in payload.get("Parameters") or []}
    return {"VpcId", "SubnetId", "AutoTerminateHours", "RootVolumeGiB"}.issubset(parameters)


def _outputs(stack: dict[str, Any]) -> dict[str, str]:
    return {
        str(row.get("OutputKey")): str(row.get("OutputValue"))
        for row in stack.get("Outputs") or []
        if row.get("OutputKey")
    }


def _instance_status(region: str, instance_id: str) -> dict[str, Any]:
    payload = _aws_json(
        ["ec2", "describe-instances", "--instance-ids", instance_id], region=region
    )
    reservations = payload.get("Reservations") or []
    instances = reservations[0].get("Instances", []) if reservations else []
    if not instances:
        return {"instance_id": instance_id, "state": "not-found"}
    row = instances[0]
    return {
        "instance_id": instance_id,
        "state": (row.get("State") or {}).get("Name", "unknown"),
        "instance_type": row.get("InstanceType", ""),
        "launch_time": str(row.get("LaunchTime", "")),
        "public_ip_present": bool(row.get("PublicIpAddress")),
        "metadata_tokens": (row.get("MetadataOptions") or {}).get("HttpTokens", ""),
    }


def _print_plan(args: argparse.Namespace, *, online: bool) -> int:
    estimate = estimate_cost(
        instance_type=args.instance_type,
        hours=args.hours,
        volume_gib=args.volume_gib,
        budget_usd=args.budget_usd,
        instance_hourly_usd=args.instance_hourly_usd,
    )
    if estimate.subtotal_usd > args.budget_usd:
        print(
            f"REFUSED: estimated ${estimate.subtotal_usd:.2f} exceeds "
            f"the ${args.budget_usd:.2f} guard.",
            file=sys.stderr,
        )
        return 2

    online_result: dict[str, Any] = {"checked": False}
    if online:
        _identity_is_ready(args.region)
        vpc_id, subnet_id = _resolve_network(args.region)
        quota = _quota(args.region)
        template_valid = _validate_template_online(args.region)
        required = INSTANCE_VCPU[args.instance_type]
        if quota < required:
            raise AwsCliError(
                f"EC2 Standard quota is {quota:g} vCPU; {args.instance_type} needs {required}."
            )
        online_result = {
            "checked": True,
            "default_vpc_found": True,
            "subnet_found": True,
            "standard_vcpu_quota": quota,
            "required_vcpu": required,
            "quota_ok": True,
            # IDs are intentionally not printed into logs/artifacts.
            "network_resolved": bool(vpc_id and subnet_id),
            "cloudformation_template_valid": template_valid,
        }

    payload = {
        "mode": "online-read-only" if online else "offline-static",
        "region": args.region,
        "stack": args.stack_name,
        "image": "sapse/abap-cloud-developer-trial:2025",
        "image_arch": "linux/amd64",
        "image_compressed_gb": 22.2,
        "estimate": asdict(estimate),
        "aws_checks": online_result,
        "guards": {
            "no_inbound_ports": True,
            "ssm_only": True,
            "imdsv2_required": True,
            "encrypted_root_disk": True,
            "delete_disk_on_termination": True,
            "auto_terminate_hours": args.hours,
            "sap_image_pull_is_manual": True,
            "sap_license_acceptance_is_manual": True,
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


def _deploy(args: argparse.Namespace) -> int:
    if args.confirm != DEPLOY_CONFIRMATION:
        print(
            f"REFUSED: deploy requires --confirm {DEPLOY_CONFIRMATION}", file=sys.stderr
        )
        return 2
    plan_rc = _print_plan(args, online=True)
    if plan_rc:
        return plan_rc
    if _stack(args.region, args.stack_name) is not None:
        print(
            f"REFUSED: stack {args.stack_name!r} already exists; inspect or destroy it first.",
            file=sys.stderr,
        )
        return 2
    vpc_id, subnet_id = _resolve_network(args.region)
    command = [
        "cloudformation",
        "deploy",
        "--template-file",
        str(TEMPLATE),
        "--stack-name",
        args.stack_name,
        "--capabilities",
        "CAPABILITY_NAMED_IAM",
        "--no-fail-on-empty-changeset",
        "--parameter-overrides",
        f"VpcId={vpc_id}",
        f"SubnetId={subnet_id}",
        f"InstanceType={args.instance_type}",
        f"RootVolumeGiB={args.volume_gib}",
        f"AutoTerminateHours={args.hours}",
    ]
    result = _aws(command, region=args.region)
    if result.stdout.strip():
        print(result.stdout.strip())
    stack = _stack(args.region, args.stack_name)
    if stack is None:
        raise AwsCliError("CloudFormation deploy returned but the stack is missing.")
    outputs = _outputs(stack)
    print(json.dumps({"stack_status": stack.get("StackStatus"), "outputs": outputs}, indent=2))
    return 0


def _status(args: argparse.Namespace) -> int:
    stack = _stack(args.region, args.stack_name)
    if stack is None:
        print(json.dumps({"stack": args.stack_name, "status": "not-found"}, indent=2))
        return 1
    outputs = _outputs(stack)
    instance_id = outputs.get("InstanceId", "")
    payload = {
        "stack": args.stack_name,
        "stack_status": stack.get("StackStatus"),
        "instance": _instance_status(args.region, instance_id) if instance_id else {},
    }
    print(json.dumps(payload, indent=2))
    return 0


def _destroy(args: argparse.Namespace) -> int:
    if args.confirm != DESTROY_CONFIRMATION:
        print(
            f"REFUSED: destroy requires --confirm {DESTROY_CONFIRMATION}", file=sys.stderr
        )
        return 2
    if _stack(args.region, args.stack_name) is None:
        print(f"Stack {args.stack_name!r} does not exist; nothing to destroy.")
        return 0
    _aws(
        ["cloudformation", "delete-stack", "--stack-name", args.stack_name],
        region=args.region,
    )
    # The waiter keeps cleanup deterministic. It does not extend the EC2 cost
    # window; deletion starts before waiting.
    _aws(
        ["cloudformation", "wait", "stack-delete-complete", "--stack-name", args.stack_name],
        region=args.region,
    )
    print(f"Deleted stack {args.stack_name}; root disk was configured DeleteOnTermination=true.")
    return 0


def _instance_id_for_stack(region: str, stack_name: str) -> str:
    stack = _stack(region, stack_name)
    if stack is None:
        raise AwsCliError(f"Stack {stack_name!r} does not exist.")
    instance_id = _outputs(stack).get("InstanceId", "")
    if not re.fullmatch(r"i-[0-9a-f]+", instance_id):
        raise AwsCliError("Stack has no valid InstanceId output.")
    return instance_id


def _session(args: argparse.Namespace) -> int:
    instance_id = _instance_id_for_stack(args.region, args.stack_name)
    command = [
        "ssm",
        "start-session",
        "--target",
        instance_id,
        "--region",
        args.region,
    ]
    return subprocess.run(["aws", *command], check=False).returncode


def _tunnel(args: argparse.Namespace) -> int:
    instance_id = _instance_id_for_stack(args.region, args.stack_name)
    command = [
        "ssm",
        "start-session",
        "--target",
        instance_id,
        "--document-name",
        "AWS-StartPortForwardingSession",
        "--parameters",
        f"portNumber={args.remote_port},localPortNumber={args.local_port}",
        "--region",
        args.region,
    ]
    return subprocess.run(["aws", *command], check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--stack-name", default=DEFAULT_STACK)
    parser.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    parser.add_argument("--volume-gib", type=int, default=DEFAULT_VOLUME_GIB)
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    parser.add_argument("--instance-hourly-usd", type=float)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Read-only quota/network/cost check.")
    plan.add_argument("--offline", action="store_true", help="Do not call AWS APIs.")

    deploy = sub.add_parser("deploy", help="Create the paid ephemeral stack.")
    deploy.add_argument("--confirm", default="")

    sub.add_parser("status", help="Read-only stack and instance state.")

    destroy = sub.add_parser("destroy", help="Delete stack and its root disk.")
    destroy.add_argument("--confirm", default="")

    sub.add_parser("session", help="Open an interactive SSM shell.")
    tunnel = sub.add_parser("tunnel", help="Forward local HTTPS to ABAP through SSM.")
    tunnel.add_argument("--remote-port", type=int, default=50001)
    tunnel.add_argument("--local-port", type=int, default=50001)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            return _print_plan(args, online=not args.offline)
        if args.command == "deploy":
            return _deploy(args)
        if args.command == "status":
            return _status(args)
        if args.command == "destroy":
            return _destroy(args)
        if args.command == "session":
            return _session(args)
        if args.command == "tunnel":
            return _tunnel(args)
    except (AwsCliError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
