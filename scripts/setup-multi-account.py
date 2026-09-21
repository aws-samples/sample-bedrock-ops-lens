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
                          pre-provisioned, along with the administration
                          role in the central account. See
                          docs/multi-account-setup.md for both trust chains.

All four scopes use the same role template (`infra/monitored-account-role.yaml`).

Re-running updates existing roles and adds missing stack instances. Removing
an account from the list does not delete its role. The script reads the central
account ID via STS, rather than taking it as input.

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
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


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


def account_ids(accounts: str | None, accounts_file: str | None) -> list[str]:
    """Resolve one explicit source; never silently discard invalid targets."""
    if (accounts is not None) == (accounts_file is not None):
        raise ValueError("provide exactly one of --accounts or --accounts-file")
    if accounts_file is not None:
        values = [
            line.strip()
            for line in Path(accounts_file).read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        values = [value.strip() for value in accounts.split(",")]
    if not values or any(not re.fullmatch(r"[0-9]{12}", value) for value in values):
        raise ValueError(
            "every account ID must contain exactly 12 digits; "
            "use comma-separated IDs or a file with one ID per line"
        )
    return sorted(set(values))


def parameters(central_account_id: str, role_name: str,
               external_id: str | None,
               ingester_role_arn: str | None = None) -> list[dict]:
    out = [
        {"ParameterKey": "CentralAccountId", "ParameterValue": central_account_id},
        {"ParameterKey": "RoleName", "ParameterValue": role_name},
        {"ParameterKey": "ExternalId", "ParameterValue": external_id or ""},
        {"ParameterKey": "CentralIngesterRoleArn",
         "ParameterValue": ingester_role_arn or ""},
    ]
    return out


def stackset_summaries(cfn, method: str, **kwargs) -> list[dict]:
    """Read every page; a failure or missing account may be on the last page."""
    rows = []
    while True:
        page = getattr(cfn, method)(**kwargs)
        rows.extend(page.get("Summaries", []))
        token = page.get("NextToken")
        if not token:
            return rows
        kwargs["NextToken"] = token


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
        if status in ("SUCCEEDED", "FAILED", "STOPPED"):
            # SUCCEEDED can include failed accounts within the failure tolerance.
            # Do not configure ingestion until every reported target succeeded.
            results = stackset_summaries(
                cfn, "list_stack_set_operation_results", **kwargs
            )
            failed = [r for r in results if r.get("Status") != "SUCCEEDED"]
            if failed:
                print()
                print("  Per-account failures:")
                for r in failed[:10]:
                    reason = (r.get("StatusReason") or "").strip()
                    print(f"    - {r['Account']}/{r.get('Region', '?')}: "
                          f"{r.get('Status')} {reason[:200]}")
                if "already exists" in " ".join(r.get("StatusReason", "") for r in failed):
                    print()
                    print("  Hint: inspect the existing role's CloudFormation owner "
                          "before changing it. See docs/multi-account-setup.md.")
            if status == "SUCCEEDED" and not failed:
                return
            raise RuntimeError(
                f"StackSet operation {operation_id} ended in status {status}; "
                f"{len(failed)} account result(s) did not succeed"
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
        "Parameters": parameters(central_account_id, args.role_name, args.external_id,
                                 args.ingester_role_arn),
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
        Parameters=parameters(central_account_id, args.role_name, args.external_id,
                              args.ingester_role_arn),
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
            raise RuntimeError(
                "A StackSet operation is already running. Wait for it to finish, "
                "then re-run setup; ingestion has not been reconfigured."
            ) from e
        else:
            raise

    # 3. summary ----------------------------------------------------------------
    summary = stackset_summaries(
        cfn, "list_stack_instances", StackSetName=stack_set_name, CallAs=call_as
    )
    print(f"  ✓ StackSet has {len(summary)} stack instances")


# ---------------------------------------------------------------------------
# Scope: accounts  (self-managed StackSet)
# ---------------------------------------------------------------------------
def scope_accounts(args, central_account_id: str) -> None:
    """Deploy via self-managed StackSet to an explicit account list.

    Requirements (one-time, per AWS docs):
      - Central account: AWSCloudFormationStackSetAdministrationRole exists
      - Each member account: AWSCloudFormationStackSetExecutionRole exists,
        trusting the central admin role.

    The script checks the central role exists. CloudFormation checks the target
    execution roles during rollout; the caller need not assume them directly.
    Bootstrap instructions and templates are in docs/multi-account-setup.md.
    """
    accounts = account_ids(args.accounts, args.accounts_file)

    print(f"[scope=accounts]  central={central_account_id}  "
          f"members={len(accounts)}  region={args.region}")

    iam = boto3.client("iam")
    admin_role_name = "AWSCloudFormationStackSetAdministrationRole"
    try:
        admin_role = iam.get_role(RoleName=admin_role_name)["Role"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            sys.exit(
                f"Missing IAM role '{admin_role_name}' in central account "
                f"{central_account_id}.\n\n"
                "This is a one-time AWS-StackSets pre-requisite for self-managed "
                "deployments. See docs/multi-account-setup.md for the included "
                "bootstrap templates and the operator's iam:PassRole permission.\n"
                "AWS reference:\n"
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
        Parameters=parameters(central_account_id, args.role_name, args.external_id,
                              args.ingester_role_arn),
        PermissionModel="SELF_MANAGED",
        AdministrationRoleARN=admin_role["Arn"],
        ExecutionRoleName="AWSCloudFormationStackSetExecutionRole",
    )

    try:
        existing = cfn.describe_stack_set(StackSetName=stack_set_name)["StackSet"]
        if existing.get("PermissionModel") != "SELF_MANAGED":
            raise ValueError(
                f"{stack_set_name} is not SELF_MANAGED. Use a separate StackSet "
                "name for a different permission model; do not replace it implicitly."
            )
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

    summaries = stackset_summaries(
        cfn, "list_stack_instances", StackSetName=stack_set_name
    )
    present = {r["Account"] for r in summaries if r["Region"] == args.region}
    missing = sorted(set(accounts) - present)
    if missing:
        print(f"  attaching stack instances to {len(missing)} new accounts "
              f"in {args.region}…")
        resp = cfn.create_stack_instances(
            StackSetName=stack_set_name,
            Accounts=missing,
            Regions=[args.region],
            OperationPreferences={
                "FailureToleranceCount": args.failure_tolerance,
                "MaxConcurrentCount": min(10, args.failure_tolerance + 1),
            },
        )
        wait_for_stackset_operation(
            cfn, stack_set_name, None, resp["OperationId"], max_minutes=60
        )
    summaries = stackset_summaries(
        cfn, "list_stack_instances", StackSetName=stack_set_name
    )
    current = {
        r["Account"] for r in summaries
        if r["Region"] == args.region and r.get("Status") == "CURRENT"
        and r.get("StackInstanceStatus", {}).get("DetailedStatus", "SUCCEEDED")
        == "SUCCEEDED"
    }
    incomplete = sorted(set(accounts) - current)
    if incomplete:
        raise RuntimeError(
            "Reader-role rollout is incomplete for account(s): "
            + ", ".join(incomplete)
            + ". Inspect the StackSet instance status before retrying."
        )
    print(f"  ✓ All {len(accounts)} requested accounts have current stack instances")


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
        help=f"IAM role name to deploy. Default: {DEFAULT_ROLE_NAME}. Must match "
             "ReaderRoleName in the central Lambda stack; this command does not "
             "change the central IAM policy or runtime configuration.",
    )
    p.add_argument(
        "--external-id", default=os.environ.get("BEDROCK_OPS_LENS_EXTERNAL_ID", ""),
        help="Optional external ID for the trust policy.",
    )
    p.add_argument(
        "--ingester-role-arn", default=None,
        help="Trust only this central ingester IAM role. setup-pipeline.sh obtains "
             "it from the deployed Lambda. Omitting it retains account-level trust.",
    )
    p.add_argument(
        "--print-account-ids", action="store_true",
        help="Validate an explicit account source, print canonical CSV, and exit "
             "without calling AWS.",
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
        "--failure-tolerance", type=int, default=0,
        help="StackSets FailureToleranceCount. Default 0. Any failed account "
             "still makes this setup command fail.",
    )
    args = p.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", args.role_name):
        p.error("--role-name must be a valid IAM role name (1-64 characters, no path or wildcards)")
    if args.failure_tolerance < 0:
        p.error("--failure-tolerance must be nonnegative")
    if args.scope == "accounts":
        try:
            resolved = account_ids(args.accounts, args.accounts_file)
        except (ValueError, OSError) as e:
            p.error(str(e))
        if args.print_account_ids:
            print(",".join(resolved))
            return 0
        # Freeze the input once so the same validated targets are used throughout.
        args.accounts, args.accounts_file = ",".join(resolved), None
    elif args.print_account_ids:
        p.error("--print-account-ids requires --scope accounts")

    me = caller_identity()
    central_account_id = me["Account"]
    if args.ingester_role_arn:
        if not re.fullmatch(
            rf"arn:[a-z0-9-]+:iam::{central_account_id}:role/[\w+=,.@/-]+",
            args.ingester_role_arn,
        ):
            p.error("--ingester-role-arn must name an IAM role in the central account")
    else:
        print("NOTE: reader roles will trust the central account; principals there "
              "also need sts:AssumeRole permission. Use --ingester-role-arn to "
              "restrict trust to the ingester.")
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
    print(f"  Ensure the central stack's ReaderRoleName is {args.role_name!r}.")
    print("  For Lambda deployments, setup-pipeline.sh uses that parameter for both")
    print("  rollout and ingestion; deploy.sh changes it via BEDROCK_OPS_LENS_ROLE_NAME.")
    print(f"  Update the central ingester to use mode='discover-org' or 'explicit'")
    print(f"  with the relevant account list. Then trigger:")
    print(
        f"    aws lambda invoke --function-name BedrockOpsLens-<suffix>-ingester "
        f"--invocation-type RequestResponse /tmp/out.json"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
