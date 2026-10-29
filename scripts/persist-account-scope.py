#!/usr/bin/env python3
"""Persist pipeline scope using the deployed CloudFormation template.

setup-pipeline.sh supplies a private environment JSON file and the revision/role
it just read from Lambda. Only the four scope parameters are changed. In
particular, a NoEcho value from DescribeStacks must never be copied back as the
external ID: the desired value comes from the private file, even on a rerun.

CloudFormation has no Lambda RevisionId precondition. Check the revision just
before submission, reject intervening stack changes, and verify both the stack
and live function after completion. Operators must serialize setup/deploy
operations; these checks cannot atomically exclude an out-of-band Lambda edit
while CloudFormation is applying its update.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


PARAMETER_ENV = {
    "MonitoredAccountsMode": "MONITORED_ACCOUNTS_MODE",
    "MonitoredAccountIds": "MONITORED_ACCOUNTS_IDS",
    "MonitoredOuIds": "MONITORED_OU_IDS",
    "ReaderExternalId": "BEDROCK_OPS_LENS_EXTERNAL_ID",
}
READY_STATES = {
    "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE",
}
DEFAULT_READER_ROLE = "BedrockOpsLensReader"


class ScopeError(RuntimeError):
    """An operator-safe error; never include environment values or AWS errors."""


def desired_parameters(environment: dict) -> dict[str, str]:
    """Validate the complete desired scope before making any AWS requests."""
    variables = environment.get("Variables", {})
    values = {key: variables.get(env, "") for key, env in PARAMETER_ENV.items()}
    if any(not isinstance(value, str) for value in values.values()):
        raise ScopeError("Scope settings must be strings.")
    mode = values["MonitoredAccountsMode"]
    accounts, ous = values["MonitoredAccountIds"], values["MonitoredOuIds"]
    if mode not in {"single", "explicit", "discover-org", "discover-ou"}:
        raise ScopeError("Invalid monitored accounts mode.")
    if mode == "explicit":
        if not accounts or any(not re.fullmatch(r"[0-9]{12}", a) for a in accounts.split(",")):
            raise ScopeError("Explicit mode requires valid account IDs.")
    elif accounts:
        raise ScopeError("Account IDs must be empty outside explicit mode.")
    if mode == "discover-ou":
        if not ous or any(not re.fullmatch(r"ou-[0-9a-z]{4,32}-[0-9a-z]{8,32}", ou)
                          for ou in ous.split(",")):
            raise ScopeError("OU discovery requires valid OU IDs.")
    elif ous:
        raise ScopeError("OU IDs must be empty outside OU discovery mode.")
    reader_role = variables.get("BEDROCK_OPS_LENS_ROLE_NAME", "")
    if not isinstance(reader_role, str) or not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", reader_role):
        raise ScopeError("A valid reader role name is required.")
    return values


def read_stack(cfn, stack_name: str) -> dict:
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    if stack.get("StackStatus") not in READY_STATES:
        raise ScopeError("The central stack is busy or not updateable; wait or repair it before setup.")
    return stack


def stack_values(stack: dict) -> dict[str, str]:
    return {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])}


def stack_signature(stack: dict) -> tuple:
    return (
        stack["StackId"], stack.get("LastUpdatedTime"), stack.get("CreationTime"),
        stack.get("StackStatus"), stack.get("RoleARN"),
        sorted(stack.get("Capabilities", [])),
        sorted((p["ParameterKey"], p.get("ParameterValue"), p.get("ResolvedValue"))
               for p in stack.get("Parameters", [])),
    )


def read_function(lam, function_name: str, expected_role: str) -> dict:
    config = lam.get_function_configuration(FunctionName=function_name)
    if config.get("Role") != expected_role:
        raise ScopeError("The ingester role changed during setup; re-run before changing its scope.")
    if config.get("Environment", {}).get("Error"):
        raise ScopeError("Cannot read the ingester environment; resolve its access error before setup.")
    if config.get("LastUpdateStatus", "Successful") != "Successful":
        raise ScopeError("The ingester configuration is busy or failed; repair it before setup.")
    return config


def verify_live_scope(lam, function_name: str, expected_role: str,
                      desired: dict[str, str], reader_role: str) -> None:
    config = read_function(lam, function_name, expected_role)
    variables = config.get("Environment", {}).get("Variables", {})
    if (any(variables.get(env, "") != desired[key] for key, env in PARAMETER_ENV.items())
            or variables.get("BEDROCK_OPS_LENS_ROLE_NAME") != reader_role):
        raise ScopeError(
            "Live ingester scope does not match the requested stack configuration. "
            "Check template wiring and configuration drift, then reapply the deployment template "
            "and re-run setup; ingestion was not started."
        )


def persist_scope(cfn, lam, *, stack_name: str, function_name: str,
                  environment: dict, expected_revision: str, expected_role: str) -> bool:
    """Return True for an update, False for a verified CloudFormation no-op."""
    desired = desired_parameters(environment)
    reader_role = environment["Variables"]["BEDROCK_OPS_LENS_ROLE_NAME"]
    if not expected_revision or not expected_role:
        raise ScopeError("The expected ingester revision and role are required.")
    stack = read_stack(cfn, stack_name)
    current = stack_values(stack)
    if not set(PARAMETER_ENV).issubset(current):
        raise ScopeError("The deployed template lacks durable scope parameters; apply the new deploy template first.")
    if current.get("ReaderRoleName", DEFAULT_READER_ROLE) != reader_role:
        raise ScopeError("The central ReaderRoleName changed during setup; re-run setup.")
    if (desired["MonitoredAccountsMode"] in {"discover-org", "discover-ou"}
            and current.get("EnableOrganizationsDiscovery") == "false"):
        raise ScopeError("Organizations discovery is disabled on the central stack.")

    request = {
        "StackName": stack["StackId"],
        "UsePreviousTemplate": True,
        "Parameters": [
            {"ParameterKey": key, "ParameterValue": desired[key]}
            if key in desired else {"ParameterKey": key, "UsePreviousValue": True}
            for key in current
        ],
        "ClientRequestToken": "lens-scope-" + uuid.uuid4().hex,
    }
    if stack.get("RoleARN"):
        request["RoleARN"] = stack["RoleARN"]
    if stack.get("Capabilities"):
        request["Capabilities"] = stack["Capabilities"]

    # Use the immutable stack ID and reject changes since our parameter snapshot.
    if stack_signature(read_stack(cfn, stack["StackId"])) != stack_signature(stack):
        raise ScopeError("The central stack changed during setup; re-run before changing its scope.")
    config = read_function(lam, function_name, expected_role)
    if config.get("RevisionId") != expected_revision:
        raise ScopeError("The ingester revision changed during setup; re-run before changing its scope.")

    updated = True
    try:
        cfn.update_stack(**request)
    except ClientError as error:
        # NoEcho prevents reliable client-side comparison of ReaderExternalId.
        # Only the service can establish that all parameter values are unchanged.
        details = error.response.get("Error", {})
        if (details.get("Code") == "ValidationError"
                and details.get("Message", "").strip() == "No updates are to be performed."):
            updated = False
        else:
            raise ScopeError(
                "CloudFormation rejected the scope update; inspect stack events and permissions. "
                "AWS error details are suppressed to protect ReaderExternalId."
            ) from None

    if updated:
        print("    waiting for the CloudFormation scope update...", flush=True)
        try:
            cfn.get_waiter("stack_update_complete").wait(StackName=stack["StackId"])
        except Exception:
            raise ScopeError(
                "The CloudFormation scope update did not complete successfully; inspect stack events "
                "before retrying. Ingestion was not started."
            ) from None

    final_stack = read_stack(cfn, stack["StackId"])
    final_values = stack_values(final_stack)
    if updated and final_stack.get("StackStatus") != "UPDATE_COMPLETE":
        raise ScopeError("CloudFormation did not finish in UPDATE_COMPLETE; inspect stack events.")
    if (not set(PARAMETER_ENV).issubset(final_values)
            or final_stack.get("RoleARN") != stack.get("RoleARN")
            or final_values.get("ReaderRoleName", DEFAULT_READER_ROLE) != reader_role
            or any(final_values.get(key) != value for key, value in desired.items()
                   if key != "ReaderExternalId")):
        raise ScopeError("Stack scope changed or was not persisted; inspect the stack before retrying.")
    verify_live_scope(lam, function_name, expected_role, desired, reader_role)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--function-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment-file", required=True,
                        help="Private JSON file with Variables; never pass the external ID on the command line.")
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-role-arn", required=True)
    args = parser.parse_args(argv)
    try:
        environment = json.loads(Path(args.environment_file).read_text(encoding="utf-8"))
        desired_parameters(environment)
        updated = persist_scope(
            boto3.client("cloudformation", region_name=args.region),
            boto3.client("lambda", region_name=args.region),
            stack_name=args.stack_name, function_name=args.function_name,
            environment=environment, expected_revision=args.expected_revision,
            expected_role=args.expected_role_arn,
        )
    except ScopeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception:
        # SDK errors and malformed JSON may contain the external ID. Do not
        # interpolate their messages, response bodies, or tracebacks.
        print("ERROR: cannot persist or verify scope; check the private input file, AWS access, "
              "stack events, and Lambda configuration. Details suppressed to protect ReaderExternalId.",
              file=sys.stderr)
        return 1
    print("    scope persisted in CloudFormation; live ingester scope verified."
          if updated else "    CloudFormation scope unchanged; live ingester scope verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
