#!/usr/bin/env python3
"""
Bedrock Ops Lens — multi-account reader-role rollout.

One Python module that replaces the old multi-mode bash. Deploys the
`BedrockOpsLensReader` IAM role into every account whose Bedrock data the
central ingester needs to pull. Picks the right CloudFormation API based
on `--scope`:

    --scope single        Just the central account (no cross-account work).
    --scope ou            Service-managed StackSet, deployed to one or more
                          OUs. Auto-deploy ON, so accounts joining the OU
                          later are auto-onboarded. Requires AWS Organizations.
    --scope org-root      Service-managed StackSet across the entire org root.
                          Same auto-deploy mechanic as --scope ou.
    --scope accounts      Self-managed StackSet against an explicit account
                          list. No Organizations required, but each member
                          account must have the `AWSCloudFormationStackSetExecutionRole`
                          pre-provisioned (a one-time AWS-doc setup; not
                          something we automate per-account).

All four scopes use the same role template (`infra/monitored-account-role.yaml`).

Idempotent: re-runnable without side effects. The script reads the central
account ID via STS, doesn't take it as input.

Run from the central account:
    python scripts/setup-multi-account.py --scope ou --ou-id ou-xxxx-yyyyyyyy
    python scripts/setup-multi-account.py --scope org-root
    python scripts/setup-multi-account.py --scope accounts \\
        --accounts 111111111111,222222222222,333333333333
    python scripts/setup-multi-account.py --scope single
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError


ROOT = Path(__file__).resolve().parent.parent
ROLE_TEMPLATE_PATH = ROOT / "infra" / "monitored-account-role.yaml"
DEFAULT_ROLE_NAME = "BedrockOpsLensReader"
DEFAULT_STACK_SET_NAME = "BedrockOpsLensReaderRole"
DEFAULT_REGION = "us-east-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def caller_identity() -> dict:
    """Return STS GetCallerIdentity for the running creds."""
    return boto3.client("sts").get_caller_identity()


def organization_root_id() -> str | None:
    """Return the org root ID (`r-xxxx`) or None if Organizations isn't enabled."""
    try:
        roots = boto3.client("organizations").list_roots()["Roots"]
        return roots[0]["Id"] if roots else None
    except ClientError as e:
        if e.response["Error"]["Code"] in (
            "AWSOrganizationsNotInUseException",
            "AccessDeniedException",
        ):
            return None
        raise


def template_body() -> str:
    if not ROLE_TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"Missing role template: {ROLE_TEMPLATE_PATH}")
    return ROLE_TEMPLATE_PATH.read_text(encoding="utf-8")


def parameters(central_account_id: str, role_name: str,
                external_id: str | None) -> list[dict]:
    out = [
        {"ParameterKey": "CentralAccountId", "ParameterValue": central_account_id},
        {"ParameterKey": "RoleName", "ParameterValue": role_name},
    ]
    if external_id:
        out.append({"ParameterKey": "ExternalId", "ParameterValue": external_id})
    return out


def wait_for_stackset_operation(cfn, stack_set_name: str,
                                  call_as: str | None,
                                  operation_id: str,
                                  *, sleep_s: int = 10,
                                  max_minutes: int = 30) -> None:
    """Poll DescribeStackSetOperation until SUCCEEDED, FAILED, or STOPPED.

    On failure, fetch per-account result reasons and surface them. CFN's
    StackSet errors are notoriously vague at the operation level; the real
    cause lives in the per-instance results.
    """
    deadline = time.monotonic() + max_minutes * 60
    last_status = None
    while time.monotonic() < deadline:
        kwargs = {"StackSetName": stack_set_name, "OperationId": operation_id}
        if call_as:
            kwargs["CallAs"] = call_as
        op = cfn.describe_stack_set_operation(**kwargs)["StackSetOperation"]
        status = op["Status"]
        if status != last_status:
            print(f"    operation {operation_id[:8]}...  status={status}")
            last_status = status
        if status == "SUCCEEDED":
            return
        if status in ("FAILED", "STOPPED"):
            # Surface per-account details so the operator knows what to fix.
            try:
                results = cfn.list_stack_set_operation_results(
                    StackSetName=stack_set_name,
                    OperationId=operation_id,
                    **({"CallAs": call_as} if call_as else {}),
                )["Summaries"]
            except ClientError:
                results = []
            failed = [r for r in results if r.get("Status") == "FAILED"]
            if failed:
                print()
                print("  Per-account failures:")
                for r in failed[:10]:
                    reason = (r.get("StatusReason") or "").strip()
                    print(f"    - {r['Account']}: {reason[:200]}")
                if "already exists" in " ".join(r.get("StatusReason", "") for r in failed):
                    print()
                    print("  Hint: at least one account already has the role from a "
                          "prior StackSet. Delete the conflicting StackSet first "
                          "or use --role-name to choose a non-conflicting name.")
            raise RuntimeError(
                f"StackSet operation {operation_id} ended in status {status}"
            )
        # Polling delay between StackSet operation status checks. Not a
        # leftover debug sleep; this is the documented pattern for waiting
        # on async CFN StackSet ops (no native waiter exists).
        time.sleep(sleep_s)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(
        f"StackSet operation {operation_id} did not finish within {max_minutes} min"
    )


# ---------------------------------------------------------------------------
# Scope: single
# ---------------------------------------------------------------------------
def scope_single(args, central_account_id: str) -> None:
    """Deploy the reader role into the central account only.

    Useful for tier-A POC deploys — no Organizations, no StackSet. The
    central ingester will assume the role into itself when iterating
    discovered accounts.
    """
    print(f"[scope=single]  central={central_account_id}  region={args.region}")
    cfn = boto3.client("cloudformation", region_name=args.region)
    stack_name = args.stack_name or "BedrockOpsLensReaderRole"
    print(f"  deploying stack {stack_name} to central account…")

    try:
        cfn.describe_stacks(StackName=stack_name)
        verb = "update_stack"
    except ClientError as e:
        if "does not exist" in str(e):
            verb = "create_stack"
        else:
            raise

    kwargs = {
        "StackName": stack_name,
        "TemplateBody": template_body(),
        "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Parameters": parameters(central_account_id, args.role_name, args.external_id),
    }
    try:
        getattr(cfn, verb)(**kwargs)
    except ClientError as e:
        if "No updates are to be performed" in str(e):
            print("  (no changes)")
            return
        raise

    waiter = cfn.get_waiter(
        "stack_update_complete" if verb == "update_stack" else "stack_create_complete"
    )
    print("  waiting for stack to settle…")
    waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    print(f"  ✓ role '{args.role_name}' is in central account {central_account_id}")


# ---------------------------------------------------------------------------
# Scope: ou + org-root  (service-managed StackSet)
# ---------------------------------------------------------------------------
def scope_org(args, central_account_id: str) -> None:
    """Deploy via service-managed StackSet to OU(s) or org root.

    AWS handles execution-role provisioning in member accounts. AutoDeployment
    on means new accounts joining the targeted scope auto-onboard.

    Must be run from the management account or a delegated administrator.
    """
    root_id = organization_root_id()
    if root_id is None:
        sys.exit(
            "AWS Organizations is not enabled or this account cannot list roots. "
            "Use --scope accounts for non-org deploys, or --scope single for "
            "central-account-only."
        )

    if args.scope == "org-root":
        targets = {"OrganizationalUnitIds": [root_id]}
        target_label = f"org root {root_id}"
    else:
        ou_ids = [s.strip() for s in args.ou_id.split(",") if s.strip()]
        if not ou_ids:
            sys.exit("--scope ou requires --ou-id ou-xxxx-yyyyyyyy[,ou-...]")
        targets = {"OrganizationalUnitIds": ou_ids}
        target_label = f"OUs {','.join(ou_ids)}"

    cfn = boto3.client("cloudformation", region_name=args.region)
    stack_set_name = args.stack_set_name or DEFAULT_STACK_SET_NAME
    call_as = "DELEGATED_ADMIN" if args.delegated_admin else "SELF"
    print(f"[scope={args.scope}]  central={central_account_id}  "
          f"target={target_label}  region={args.region}  callAs={call_as}")

    # 1. create_stack_set or update_stack_set ----------------------------------
    template = template_body()
    common = dict(
        StackSetName=stack_set_name,
        TemplateBody=template,
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Parameters=parameters(central_account_id, args.role_name, args.external_id),
        PermissionModel="SERVICE_MANAGED",
        AutoDeployment={"Enabled": True, "RetainStacksOnAccountRemoval": False},
        CallAs=call_as,
    )

    try:
        cfn.describe_stack_set(StackSetName=stack_set_name, CallAs=call_as)
        exists = True
    except ClientError as e:
        if "StackSetNotFoundException" in str(e):
            exists = False
        else:
            raise

    if exists:
        print(f"  updating StackSet {stack_set_name}…")
        try:
            resp = cfn.update_stack_set(**common)
            wait_for_stackset_operation(
                cfn, stack_set_name, call_as, resp["OperationId"]
            )
        except ClientError as e:
            if "No updates are to be performed" in str(e):
                print("  (StackSet template/params unchanged)")
            else:
                raise
    else:
        print(f"  creating StackSet {stack_set_name}…")
        cfn.create_stack_set(**common)
        # create_stack_set returns immediately; no operation to wait on.

    # 2. create_stack_instances --------------------------------------------------
    print(f"  attaching stack instances to {target_label} in {args.region}…")
    try:
        resp = cfn.create_stack_instances(
            StackSetName=stack_set_name,
            DeploymentTargets=targets,
            Regions=[args.region],
            OperationPreferences={
                "FailureToleranceCount": args.failure_tolerance,
                "MaxConcurrentPercentage": 50,
            },
            CallAs=call_as,
        )
        wait_for_stackset_operation(
            cfn, stack_set_name, call_as, resp["OperationId"], max_minutes=60
        )
    except ClientError as e:
        msg = str(e)
        if "StackInstanceNotFoundException" in msg or "already exists" in msg:
            print("  (some instances already present; that's fine)")
        elif "OperationInProgressException" in msg:
            print("  (an operation is already running; let it finish, then re-run)")
            return
        else:
            raise

    # 3. summary ----------------------------------------------------------------
    summary = cfn.list_stack_instances(StackSetName=stack_set_name, CallAs=call_as)
    print(f"  ✓ StackSet has {len(summary.get('Summaries', []))} stack instances")


# ---------------------------------------------------------------------------
# Scope: accounts  (self-managed StackSet)
# ---------------------------------------------------------------------------
def scope_accounts(args, central_account_id: str) -> None:
    """Deploy via self-managed StackSet to an explicit account list.

    Requirements (one-time, per AWS docs):
      - Central account: AWSCloudFormationStackSetAdministrationRole exists
      - Each member account: AWSCloudFormationStackSetExecutionRole exists,
        trusting the central admin role.

    The script doesn't auto-create those roles (they're org-foundation IAM
    you'd want your central platform team to manage). It checks for them
    and prints the AWS-doc snippet if missing.
    """
    accounts = sorted({a.strip() for a in (args.accounts or "").split(",") if a.strip()})
    if args.accounts_file:
        accounts = sorted({
            line.strip() for line in Path(args.accounts_file).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        })
    accounts = [a for a in accounts if a.isdigit() and len(a) == 12]
    if not accounts:
        sys.exit("--scope accounts requires --accounts CSV or --accounts-file <path>")

    print(f"[scope=accounts]  central={central_account_id}  "
          f"members={len(accounts)}  region={args.region}")

    iam = boto3.client("iam")
    admin_role_name = "AWSCloudFormationStackSetAdministrationRole"
    try:
        iam.get_role(RoleName=admin_role_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            sys.exit(
                f"Missing IAM role '{admin_role_name}' in central account "
                f"{central_account_id}.\n\n"
                "This is a one-time AWS-StackSets pre-requisite for self-managed "
                "deployments. Create it via the CFN template at:\n"
                "  https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/"
                "stacksets-prereqs-self-managed.html\n\n"
                "Each member account also needs "
                "AWSCloudFormationStackSetExecutionRole trusting this admin role. "
                "After both are in place, re-run this command."
            )
        raise

    cfn = boto3.client("cloudformation", region_name=args.region)
    stack_set_name = args.stack_set_name or DEFAULT_STACK_SET_NAME

    common = dict(
        StackSetName=stack_set_name,
        TemplateBody=template_body(),
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Parameters=parameters(central_account_id, args.role_name, args.external_id),
        PermissionModel="SELF_MANAGED",
        AdministrationRoleARN=(
            f"arn:aws:iam::{central_account_id}:role/{admin_role_name}"
        ),
        ExecutionRoleName="AWSCloudFormationStackSetExecutionRole",
    )

    try:
        cfn.describe_stack_set(StackSetName=stack_set_name)
        exists = True
    except ClientError as e:
        if "StackSetNotFoundException" in str(e):
            exists = False
        else:
            raise

    if exists:
        print(f"  updating StackSet {stack_set_name}…")
        try:
            resp = cfn.update_stack_set(**common)
            wait_for_stackset_operation(cfn, stack_set_name, None, resp["OperationId"])
        except ClientError as e:
            if "No updates are to be performed" in str(e):
                print("  (StackSet template/params unchanged)")
            else:
                raise
    else:
        print(f"  creating StackSet {stack_set_name}…")
        cfn.create_stack_set(**common)

    print(f"  attaching stack instances to {len(accounts)} accounts in {args.region}…")
    try:
        resp = cfn.create_stack_instances(
            StackSetName=stack_set_name,
            Accounts=accounts,
            Regions=[args.region],
            OperationPreferences={
                "FailureToleranceCount": args.failure_tolerance,
                "MaxConcurrentCount": 10,
            },
        )
        wait_for_stackset_operation(
            cfn, stack_set_name, None, resp["OperationId"], max_minutes=60
        )
    except ClientError as e:
        msg = str(e)
        if "already exists" in msg:
            print("  (instances already present; updating any drifted ones)")
        elif "OperationInProgressException" in msg:
            print("  (an operation is already running; let it finish, then re-run)")
            return
        else:
            raise

    summary = cfn.list_stack_instances(StackSetName=stack_set_name)
    print(f"  ✓ StackSet has {len(summary.get('Summaries', []))} stack instances")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Bedrock Ops Lens — multi-account reader-role rollout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--scope", required=True,
        choices=["single", "ou", "org-root", "accounts"],
        help="Which accounts to deploy the reader role into.",
    )
    p.add_argument(
        "--ou-id", default=None,
        help="OU ID (or comma-separated list of OU IDs). Required for --scope ou.",
    )
    p.add_argument(
        "--accounts", default=None,
        help="CSV of 12-digit account IDs. Used by --scope accounts.",
    )
    p.add_argument(
        "--accounts-file", default=None,
        help="Path to file with one account ID per line. Used by --scope accounts.",
    )
    p.add_argument(
        "--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION),
        help="Region for the StackSet stack instances. Default: AWS_REGION env or us-east-1.",
    )
    p.add_argument(
        "--role-name", default=DEFAULT_ROLE_NAME,
        help=f"IAM role name to deploy. Default: {DEFAULT_ROLE_NAME}.",
    )
    p.add_argument(
        "--external-id", default=os.environ.get("BEDROCK_OPS_LENS_EXTERNAL_ID", ""),
        help="Optional external ID for the trust policy.",
    )
    p.add_argument(
        "--stack-name", default=None,
        help="(--scope single) Stack name override.",
    )
    p.add_argument(
        "--stack-set-name", default=None,
        help=f"StackSet name override. Default: {DEFAULT_STACK_SET_NAME}.",
    )
    p.add_argument(
        "--delegated-admin", action="store_true",
        help="Pass CallAs=DELEGATED_ADMIN. Use when running from a non-management "
             "account that has been registered as a StackSets delegated administrator.",
    )
    p.add_argument(
        "--failure-tolerance", type=int, default=2,
        help="StackSets FailureToleranceCount. Default 2.",
    )
    args = p.parse_args()

    me = caller_identity()
    central_account_id = me["Account"]
    print(f"Caller: {me['Arn']}")

    if args.scope == "single":
        scope_single(args, central_account_id)
    elif args.scope in ("ou", "org-root"):
        scope_org(args, central_account_id)
    elif args.scope == "accounts":
        scope_accounts(args, central_account_id)
    else:
        sys.exit(f"Unknown scope: {args.scope}")

    print()
    print("Next steps:")
    print(f"  Update the central ingester to use mode='discover-org' or 'explicit'")
    print(f"  with the relevant account list. Then trigger:")
    print(
        f"    aws lambda invoke --function-name BedrockOpsLens-<suffix>-ingester "
        f"--invocation-type RequestResponse /tmp/out.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
