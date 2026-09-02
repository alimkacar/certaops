# ABAP Cloud Developer Trial 2025 test planı

This path validates CertaOps against a **real SAP ABAP/HANA and OData runtime** without
waiting for the 40-vCPU SAP CAL appliance. It does not turn the Developer Trial into an
S/4HANA business system and must not be presented as full S/4HANA semantic acceptance.

## What this environment proves

| Evidence layer | Result |
|---|---|
| Real SAP ABAP application server and HANA runtime | Yes |
| Real HTTP, Basic auth, SAP client, OData V2 metadata and CSRF behavior | Yes |
| CertaOps policy, audit, idempotency and 24-tool orchestration | Yes |
| Compatibility-service reads and one controlled PR write | Yes, after the eight Z services are installed |
| Standard S/4 APIs such as `API_PRODUCT_SRV` | No |
| Real MM/FI customizing, BAPIs and standard business tables | No |
| Production S/4HANA validation | No; CAL/customer quality tenant remains the final gate |

The compatibility contract intentionally reuses the existing ECC adapter. It expects six
OData V2 services and 12 capability aliases. The complete entity/field contract is generated
in [`ECC_ABAP_REQUIREMENTS.md`](ECC_ABAP_REQUIREMENTS.md). In Developer Trial these services
must be backed by custom test tables rather than unavailable ECC/S/4 business tables.

The deterministic scenario in
[`config/abap_trial_scenario.json`](../config/abap_trial_scenario.json) contains a coherent
purchase-to-pay flow: material shortage, competing suppliers, purchase requisition, partially
delivered purchase order, quantity-blocked invoice, workflow state and WBS cost impact. The
fixture validator fails if an entity, critical property or cross-document relationship is lost.

## Cost and security design

- Region: `us-east-1`
- Instance: `r6i.xlarge` — 4 vCPU, 32 GiB RAM
- Root disk: 200 GiB encrypted `gp3`, deleted with the instance
- Network: public address for outbound downloads, **zero inbound rules**
- Access: AWS Systems Manager Session Manager only
- SAP ports: bound to EC2 localhost and reached through an SSM tunnel
- Metadata: IMDSv2 required
- Lifetime: 6 hours by default, hard range 2–8 hours
- Shutdown: SAP container receives a graceful stop before EC2 power-off
- EC2 behavior: power-off terminates the instance and removes its root disk
- Default estimate: about `$1.68` for six hours before tax or pricing changes
- Deployment budget guard: `$3.00`

The template never stores Docker Hub credentials, the SAP password, or a license response.
Docker login and SAP Developer License acceptance happen interactively on the disposable host.

## AWS operator identity — do not automate as root

Do not select the AWS account `root` session in `aws login`. Root is used only to bootstrap a
separate human identity. AWS recommends temporary credentials, MFA and least privilege for normal
administration.

For this one-person test account, create an IAM user named `certaops-trial-operator` with:

1. AWS Management Console access, but **no access key**;
2. MFA enabled;
3. AWS managed policy `SignInLocalDevelopmentAccess`, required by `aws login`;
4. the customer-managed policy created from
   [`config/aws_abap_trial_operator_policy.json`](../config/aws_abap_trial_operator_policy.json).

The customer-managed policy is region-bound to `us-east-1`, names the only IAM role it can manage, restricts
CloudFormation to `certaops-abap-trial`, and limits SSM sessions to the tagged disposable host.
After the final stack deletion, remove this temporary IAM user or at least detach both policies.
IAM Identity Center with a temporary permission set is preferred if you already use it.

Sign out of the root console, sign in as `certaops-trial-operator`, then acquire temporary CLI
credentials without creating a long-lived key:

```bash
aws login --profile certaops-trial --region us-east-1
export AWS_PROFILE=certaops-trial
```

`aws logout --profile certaops-trial` removes the cached login when testing ends.

## Phase 0 — free local preflight

Run this before creating any AWS resource:

```bash
python scripts/abap_trial_acceptance.py --preflight
```

It validates the 24-tool inventory, eight-service/15-alias contract, realistic fixture, offline
cost plan, security controls and selected tests. `BLOCKED` results for Docker Hub login, the live
ABAP service installation and the SSM plugin are expected until those external steps are done.

Validate the AWS account, default network and current 8-vCPU quota using read-only APIs:

```bash
python scripts/abap_trial_aws.py --hours 6 --budget-usd 3 plan
```

This command does not create or modify AWS resources.

## Phase 1 — create the disposable host

The deployment command is deliberately guarded. It creates an EC2 instance, IAM role, instance
profile and outbound-only security group through CloudFormation:

```bash
python scripts/abap_trial_aws.py \
  --hours 6 \
  --budget-usd 3 \
  deploy \
  --confirm DEPLOY-ABAP-TRIAL
```

Do not run it until the free preflight and online plan are green. The six-hour timer starts at
first boot, not when the SAP container becomes ready.

Read status without changing anything:

```bash
python scripts/abap_trial_aws.py status
```

Open the private administration shell:

```bash
python scripts/abap_trial_aws.py session
```

On the EC2 host, authenticate to Docker Hub without placing credentials in a command argument:

```bash
sudo docker login
```

Read the current SAP Developer License. Only after accepting it, start the image:

```bash
sudo env SAP_DEVELOPER_LICENSE_ACCEPTED=YES \
  /opt/certaops/start-abap-trial.sh
```

If the official runtime reports only a host-limit check failure after the normal start was
attempted, retry on a fresh disposable instance with `SAP_SKIP_LIMITS_CHECK=YES`. This is not the
default path because it weakens an official precondition.

Follow startup:

```bash
sudo docker logs -f a4h
```

## Phase 2 — install the compatibility service

The Developer Trial has no standard MM/FI business content. The following eight custom services
must be created in package `ZCERTAOPS_TRIAL` and registered in client `001`:

1. `ZAGENT_MM_MATERIAL_SRV`
2. `ZAGENT_MM_STOCK_SRV`
3. `ZAGENT_MM_SOURCING_SRV`
4. `ZAGENT_MM_PR_SRV`
5. `ZAGENT_MM_PO_SRV`
6. `ZAGENT_FI_INVOICE_SRV`
7. `ZAGENT_WF_STATUS_SRV`
8. `ZAGENT_PS_COST_SRV`

The test implementation must follow these rules:

- use custom tables and the checked-in fixture; do not pretend that ECC tables or BAPIs exist;
- expose the exact entity-set and critical-property names in the generated requirements document;
- support `$filter`, `$select`, `$top` and `$skip` for read paths;
- return standard SAP Gateway error bodies;
- implement CSRF-protected `POST` only for `PurchaseRequisitionSet`;
- enforce unique `IdempotencyKey` and persist the mapping in `IdempotencySet`;
- return an existing object for an identical retry instead of creating a second PR;
- reject conflicting payloads that reuse an idempotency key;
- keep all other entity sets read-only;
- load the fixture once and preserve document relationships.

This layer provides real ABAP/OData transport evidence and controlled persistence. It remains a
compatibility test facade, not evidence that standard S/4HANA MM/FI behavior is correct.

## Phase 3 — private OData tunnel and read acceptance

Keep this command open in a separate local terminal:

```bash
python scripts/abap_trial_aws.py tunnel
```

Create the private profile and enter only the Developer Trial password:

```bash
cp .env.abap-trial.example .env.abap-trial
```

Run live connection, all 12 contracts, all 21 tools in read/dry-run mode and the negative VIEWER
authorization cases:

```bash
python scripts/abap_trial_acceptance.py \
  --read \
  --env-file .env.abap-trial
```

The run writes JSON and Markdown evidence under `artifacts/abap-trial/<timestamp>/`. A missing
service, field, deterministic seed, tool result or role denial makes the stage fail.

## Phase 4 — one real write

Only after the read run is green, create exactly one test PR and reconcile it:

```bash
python scripts/abap_trial_acceptance.py \
  --write \
  --env-file .env.abap-trial \
  --confirm WRITE-ABAP-TRIAL-PR
```

The write still requires the three independent application gates: the exact confirmation above,
`SAP_DRY_RUN=false` inside the isolated subprocess and `SAP_INTEGRATION_ALLOW_WRITE=1`, plus
`sap_tool_sweep.py --allow-write`. The checked-in profile remains dry-run.

## Phase 5 — cleanup

Delete early rather than waiting for the timer:

```bash
python scripts/abap_trial_aws.py \
  destroy \
  --confirm DESTROY-ABAP-TRIAL
```

Confirm `status` reports `not-found`. CloudFormation deletion and the instance's own shutdown both
use `DeleteOnTermination=true`; do not create a snapshot unless its continuing storage cost is
intentional.

## Acceptance language for the project

After a green run, the defensible claim is:

> All CertaOps tools were exercised against a real SAP ABAP/HANA OData runtime using a controlled,
> deterministic compatibility service; released S/4 API contracts were separately checked against
> SAP API Business Hub. Full standard S/4HANA business-semantic and customizing validation remains
> pending an authorized S/4 quality tenant.
