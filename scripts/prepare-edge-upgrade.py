#!/usr/bin/env python3
"""Install retention on deployed resources before deploying a new template.

Usage: python3 scripts/prepare-edge-upgrade.py --stack-name NAME --region us-east-1
       [--role-arn CLOUDFORMATION_SERVICE_ROLE_ARN]
Main: add --kind main --retain-role BackendLambdaRole [--retain-role ...]
      --template-bucket EXISTING_CFN_STAGING_BUCKET

Only the deployed template's DeletionPolicy and UpdateReplacePolicy are changed.
All parameters use their previous values, including NoEcho parameters. An existing
stack service role takes precedence over --role-arn; the latter is used only if
the stack has no service role. The caller must stop deployment on a nonzero exit.

Edge mode protects the edge function, version, and generated role. Main mode
protects only explicitly selected generated roles about to be removed when
switching to supplied roles; it does not change active, unselected roles.
The caller determines those transitions. Main mode without selections, an absent
stack, or already protected resources need no update.

Large templates are staged in the supplied existing bucket, using a unique key.
No bucket is created or configured. The object remains available for stack
history/rollback; the staging bucket's existing lifecycle policy governs cleanup.
Preparation does not publish a version, change execution roles, detach
CloudFront, or clean up retained resources. Retained main roles can be deleted
only after aliases and older versions no longer need them.
Run immediately before the corresponding stack deployment. Serialize
deployments and direct resource edits: CloudFormation rejects stale change sets,
but its APIs provide no atomic lock against out-of-band IAM/Lambda edits.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import sys
import uuid

import boto3
from botocore.exceptions import ClientError
import yaml


EDGE_TYPES = {
    "EdgeShaRole": "AWS::IAM::Role",
    "EdgeShaFunction": "AWS::Lambda::Function",
    "EdgeShaVersion": "AWS::Lambda::Version",
}
MAIN_ROLE_PARAMETERS = {
    "BackendLambdaRole": "BackendRoleArn",
    "IngesterLambdaRole": "IngesterRoleArn",
    "SchemaInitLambdaRole": "SchemaInitRoleArn",
    "PrewarmLambdaRole": "PrewarmRoleArn",
    "PreSignUpFnRole": "PreSignUpRoleArn",
    "PostConfirmFnRole": "PostConfirmRoleArn",
}
RETENTION = {"DeletionPolicy", "UpdateReplacePolicy"}
READY_STATES = {
    "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE",
}


class PreparationError(RuntimeError):
    """An actionable error that is safe to print without template/parameter values."""


class TemplateLoader(yaml.SafeLoader):
    """Read CloudFormation YAML as the equivalent long-form JSON."""


def _intrinsic(loader, tag, node):
    if tag not in {
        "Ref", "Condition", "Base64", "Cidr", "FindInMap", "GetAtt", "GetAZs",
        "ImportValue", "Join", "Select", "Split", "Sub", "Transform", "And",
        "Equals", "If", "Not", "Or", "Length", "ToJsonString",
    }:
        raise PreparationError("The deployed template contains an unsupported YAML tag.")
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    if tag == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {tag if tag in {"Ref", "Condition"} else "Fn::" + tag: value}


TemplateLoader.add_multi_constructor("!", _intrinsic)
# CloudFormation treats dates such as AWSTemplateFormatVersion as strings.
TemplateLoader.add_constructor("tag:yaml.org,2002:timestamp",
                               lambda loader, node: loader.construct_scalar(node))


def read_template(cfn, stack_id):
    body = cfn.get_template(StackName=stack_id, TemplateStage="Original")["TemplateBody"]
    template = yaml.load(body, Loader=TemplateLoader) if isinstance(body, str) else deepcopy(body)
    if not isinstance(template, dict) or not isinstance(template.get("Resources"), dict):
        raise PreparationError("Cannot read the deployed template's resources.")
    return template


def read_stack(cfn, stack_id, *, allow_absent=False):
    try:
        stacks = cfn.describe_stacks(StackName=stack_id)["Stacks"]
    except ClientError as error:
        detail = error.response.get("Error", {})
        if (allow_absent and detail.get("Code") == "ValidationError"
                and detail.get("Message") == f"Stack with id {stack_id} does not exist"):
            return None
        raise
    if len(stacks) != 1 or stacks[0].get("StackStatus") not in READY_STATES:
        raise PreparationError("The stack is busy or not updateable; wait or repair it first.")
    return stacks[0]


def resources(cfn, stack_id):
    """Discover actual physical IDs; never infer names from the stack name."""
    result = {}
    request = {"StackName": stack_id}
    while True:
        page = cfn.list_stack_resources(**request)
        for resource in page["StackResourceSummaries"]:
            if resource["ResourceStatus"] != "DELETE_COMPLETE":
                result[resource["LogicalResourceId"]] = {
                    key: resource.get(key) for key in
                    ("PhysicalResourceId", "ResourceType", "ResourceStatus")
                }
        if not page.get("NextToken"):
            return result
        request["NextToken"] = page["NextToken"]


def stack_signature(stack):
    return (
        stack["StackId"], stack.get("CreationTime"), stack.get("LastUpdatedTime"),
        stack["StackStatus"], stack.get("RoleARN"), stack.get("Capabilities", []),
        sorted(stack.get("Parameters", []), key=lambda p: p["ParameterKey"]),
        sorted(stack.get("Tags", []), key=lambda tag: tag["Key"]),
        sorted(stack.get("NotificationARNs", [])),
    )


def resource_identities(inventory):
    return {key: (value["ResourceType"], value["PhysicalResourceId"])
            for key, value in inventory.items()}


def require_unchanged(cfn, stack, template, inventory):
    stack_id = stack["StackId"]
    if (stack_signature(read_stack(cfn, stack_id)) != stack_signature(stack)
            or read_template(cfn, stack_id) != template
            or resources(cfn, stack_id) != inventory
            or stack_signature(read_stack(cfn, stack_id)) != stack_signature(stack)):
        raise PreparationError("The stack changed during preparation; re-run deployment.")


def retention_targets(kind, retain_roles):
    if kind == "edge":
        if retain_roles:
            raise PreparationError("--retain-role requires --kind main.")
        return EDGE_TYPES
    if kind != "main" or any(role not in MAIN_ROLE_PARAMETERS for role in retain_roles):
        raise PreparationError("Unknown stack kind or generated main role.")
    return {role: "AWS::IAM::Role" for role in retain_roles}


def retention_template(template, inventory, targets):
    prepared = deepcopy(template)
    changed = set()
    for logical_id, expected_type in targets.items():
        live = inventory.get(logical_id)
        if live is None:
            continue  # Includes a conditional generated role that was never created.
        definition = prepared["Resources"].get(logical_id, {})
        if (live["ResourceType"] != expected_type or definition.get("Type") != expected_type
                or not live["PhysicalResourceId"]
                or live["ResourceStatus"] not in READY_STATES):
            raise PreparationError(f"Cannot safely identify the deployed {logical_id} resource.")
        for attribute in RETENTION:
            if definition.get(attribute) != "Retain":
                definition[attribute] = "Retain"
                changed.add(logical_id)
    if changed and template.get("Transform"):
        raise PreparationError("Retention preparation refuses to re-run a template transform.")
    return prepared, changed


def check_drift(cfn, stack_id, inventory, targets):
    # Lambda::Version is immutable and does not support CFN drift detection.
    # Its physical ID and all its properties are checked through the template,
    # inventory and change set. Role/function drift detection covers properties
    # supported by CFN; it does not check Lambda source code.
    for logical_id, resource_type in targets.items():
        if logical_id not in inventory or resource_type == "AWS::Lambda::Version":
            continue
        drift = cfn.detect_stack_resource_drift(
            StackName=stack_id, LogicalResourceId=logical_id)["StackResourceDrift"]
        if (drift.get("StackResourceDriftStatus") != "IN_SYNC"
                or drift.get("PhysicalResourceId") != inventory[logical_id]["PhysicalResourceId"]):
            raise PreparationError(f"{logical_id} drifted or could not be checked; reconcile it first.")


def check_change_set(cfn, stack_id, change_set_id, changed, inventory, targets):
    request = {"StackName": stack_id, "ChangeSetName": change_set_id}
    while True:
        page = cfn.describe_change_set(**request)
        if page.get("Status") != "CREATE_COMPLETE" or page.get("ExecutionStatus") != "AVAILABLE":
            raise PreparationError("The retention change set is not available; re-run deployment.")
        for change in page.get("Changes", []):
            resource = change.get("ResourceChange", {})
            logical_id = resource.get("LogicalResourceId")
            if (change.get("Type") != "Resource" or logical_id not in changed
                    or resource.get("Action") != "Modify"
                    or resource.get("Replacement") not in (None, "False")
                    or resource.get("ResourceType") != targets[logical_id]
                    or resource.get("PhysicalResourceId") != inventory[logical_id]["PhysicalResourceId"]
                    or not resource.get("Scope")
                    or not set(resource["Scope"]).issubset(RETENTION)):
                raise PreparationError("The change set would change more than retention; refusing to execute.")
            for detail in resource.get("Details", []):
                target = detail.get("Target", {})
                if (target.get("Attribute") not in RETENTION
                        or target.get("RequiresRecreation") not in (None, "Never")):
                    raise PreparationError("The change set would change a resource; refusing to execute.")
        if not page.get("NextToken"):
            return
        request["NextToken"] = page["NextToken"]


def template_input(body, *, s3=None, template_bucket=None):
    """Use a private, existing staging bucket only when inline upload cannot fit."""
    data = body.encode("utf-8")
    if len(data) <= 51200:
        return {"TemplateBody": body}
    if not template_bucket or s3 is None:
        raise PreparationError("The deployed template exceeds the inline template limit; "
                               "supply --template-bucket with the existing CFN staging bucket.")
    if len(data) > 1024 * 1024:
        raise PreparationError("The deployed template exceeds CloudFormation's S3 template limit.")
    key = f"retention/{hashlib.sha256(data).hexdigest()}-{uuid.uuid4().hex}.json"
    s3.put_object(Bucket=template_bucket, Key=key, Body=data, ContentType="application/json")
    # Use the SDK's regional endpoint (also handles aws-cn); no public ACL or
    # presigned credentials. The CFN service role needs GetObject as for deploy.
    return {"TemplateURL": f"{s3.meta.endpoint_url.rstrip('/')}/{template_bucket}/{key}"}


def prepare_edge_upgrade(cfn, *, stack_name, role_arn=None, kind="edge",
                         retain_roles=(), s3=None, template_bucket=None):
    """Return True if updated, False if absent/already retained. Raise on failure."""
    targets = retention_targets(kind, retain_roles)
    if not targets:
        return False
    stack = read_stack(cfn, stack_name, allow_absent=True)
    if stack is None:
        return False
    stack_id = stack["StackId"]
    template = read_template(cfn, stack_id)
    inventory = resources(cfn, stack_id)
    if kind == "main":
        current = {p["ParameterKey"]: p.get("ParameterValue", "")
                   for p in stack.get("Parameters", [])}
        for role in targets:
            if current.get(MAIN_ROLE_PARAMETERS[role], ""):
                raise PreparationError(f"{role} already uses a supplied role; recheck the deployment plan.")
    prepared, changed = retention_template(template, inventory, targets)
    if not changed:
        require_unchanged(cfn, stack, template, inventory)
        return False

    body = json.dumps(prepared, separators=(",", ":"))
    require_unchanged(cfn, stack, template, inventory)
    request = {
        "StackName": stack_id,
        "ChangeSetName": f"lens-{kind}-retain-" + uuid.uuid4().hex,
        "ChangeSetType": "UPDATE",
        **template_input(body, s3=s3, template_bucket=template_bucket),
        "Parameters": [{"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
                       for p in stack.get("Parameters", [])],
        "Description": "Install retention without changing resources or parameter values.",
    }
    service_role = stack.get("RoleARN") or role_arn or None
    if service_role:
        request["RoleARN"] = service_role
    if stack.get("Capabilities"):
        request["Capabilities"] = stack["Capabilities"]
    change_set_id = cfn.create_change_set(**request)["Id"]
    execution_attempted = False
    try:
        try:
            cfn.get_waiter("change_set_create_complete").wait(
                StackName=stack_id, ChangeSetName=change_set_id)
        except Exception:
            raise PreparationError("Cannot prepare the retention change set; inspect stack events.") from None
        check_change_set(cfn, stack_id, change_set_id, changed, inventory, targets)
        check_drift(cfn, stack_id, inventory, targets)
        require_unchanged(cfn, stack, template, inventory)
        execution_attempted = True
        cfn.execute_change_set(
            StackName=stack_id, ChangeSetName=change_set_id,
            ClientRequestToken=f"lens-{kind}-retain-" + uuid.uuid4().hex)
        print(f"    waiting for {kind} retention preparation...", flush=True)
        try:
            cfn.get_waiter("stack_update_complete").wait(StackName=stack_id)
        except Exception:
            raise PreparationError(
                "Retention preparation did not complete; inspect stack events before deploying."
            ) from None
        final = read_stack(cfn, stack_id)
        final_inventory = resources(cfn, stack_id)
        expected_stack = {
            **stack, "StackStatus": "UPDATE_COMPLETE", "RoleARN": service_role,
            "LastUpdatedTime": final.get("LastUpdatedTime"),
        }
        if (stack_signature(final) != stack_signature(expected_stack)
                or read_template(cfn, stack_id) != prepared
                or resource_identities(final_inventory) != resource_identities(inventory)
                or any(r["ResourceStatus"] not in READY_STATES for r in final_inventory.values())):
            raise PreparationError("Resources or parameters changed during preparation; stop deployment.")
        check_drift(cfn, stack_id, final_inventory, targets)
        require_unchanged(cfn, final, prepared, final_inventory)
        return True
    finally:
        if not execution_attempted:
            # Delete only the unexecuted change set we created, never stack resources.
            try:
                cfn.delete_change_set(StackName=stack_id, ChangeSetName=change_set_id)
            except Exception:
                print("    could not remove the unexecuted retention change set.", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--role-arn", help="CFN service role, used only if the stack has none.")
    parser.add_argument("--kind", choices=["edge", "main"], default="edge")
    parser.add_argument("--retain-role", action="append", default=[],
                        choices=list(MAIN_ROLE_PARAMETERS),
                        help="Generated main role to retain before removal; repeat for each role.")
    parser.add_argument("--template-bucket",
                        help="Existing private CFN staging bucket in --region, for large templates.")
    args = parser.parse_args(argv)
    try:
        targets = retention_targets(args.kind, args.retain_role)
        if not targets:
            print("    main retention preparation: no roles selected.")
            return 0
        changed = prepare_edge_upgrade(
            boto3.client("cloudformation", region_name=args.region),
            stack_name=args.stack_name, role_arn=args.role_arn,
            kind=args.kind, retain_roles=args.retain_role,
            s3=boto3.client("s3", region_name=args.region) if args.template_bucket else None,
            template_bucket=args.template_bucket)
    except PreparationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Template contents and NoEcho parameter values must not appear in errors.
        print("ERROR: retention preparation failed; check AWS access and stack events. "
              "Do not continue the deployment.", file=sys.stderr)
        return 1
    print(f"    {args.kind} retention preparation complete." if changed
          else f"    {args.kind} retention preparation: no update needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
