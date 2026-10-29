"""Retention migration tests: mocked clients only; no AWS credentials or calls."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, WaiterError
from botocore.session import get_session
from botocore.validate import validate_parameters
import yaml


ROOT = Path(__file__).resolve().parents[1]
STACK = "lens-edge"
STACK_ID = f"arn:aws:cloudformation:us-east-1:123456789012:stack/{STACK}/fixture"
CHANGE_SET = "arn:aws:cloudformation:us-east-1:123456789012:changeSet/retain/fixture"
SERVICE_ROLE = "arn:aws:iam::123456789012:role/cfn-service"
OTHER_SERVICE_ROLE = "arn:aws:iam::123456789012:role/new-cfn-service"
SECRET = "must-never-appear-in-output"
LEGACY = """\
AWSTemplateFormatVersion: 2010-09-09
Description: The currently deployed template, not the checkout.
Parameters:
  StackNamePrefix: {Type: String}
  PrivateValue: {Type: String, NoEcho: true}
Resources:
  EdgeShaRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {Service: [lambda.amazonaws.com, edgelambda.amazonaws.com]}
            Action: sts:AssumeRole
  EdgeShaFunction:
    Type: AWS::Lambda::Function
    Properties:
      Role: !GetAtt EdgeShaRole.Arn
      Runtime: nodejs20.x
      Handler: index.handler
      Code: {ZipFile: 'exports.handler = async (event) => event;'}
  EdgeShaVersion:
    Type: AWS::Lambda::Version
    Properties: {FunctionName: !Ref EdgeShaFunction}
  WebAcl:
    Type: AWS::WAFv2::WebACL
    Properties:
      Name: !Sub '${StackNamePrefix}-waf'
Outputs:
  EdgeShaVersionArn: {Value: !Ref EdgeShaVersion}
"""


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location(
        "prepare_edge_upgrade_test", ROOT / "scripts/prepare-edge-upgrade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(helper):
    template = yaml.load(LEGACY, Loader=helper.TemplateLoader)
    stack = {
        "StackId": STACK_ID, "StackStatus": "CREATE_COMPLETE",
        "CreationTime": "original", "RoleARN": SERVICE_ROLE,
        "Capabilities": ["CAPABILITY_IAM"], "Tags": [{"Key": "app", "Value": "lens"}],
        "Parameters": [{"ParameterKey": "StackNamePrefix", "ParameterValue": STACK},
                       {"ParameterKey": "PrivateValue", "ParameterValue": "****"}],
    }
    inventory = [
        {"LogicalResourceId": key, "ResourceType": value["Type"],
         "ResourceStatus": "CREATE_COMPLETE", "PhysicalResourceId": key + "-unpredictable-id"}
        for key, value in template["Resources"].items()
    ]
    s = SimpleNamespace(cfn=Mock(), stack=stack, template=template, inventory=inventory,
                        targets=set(helper.EDGE_TYPES), after_update=lambda: None)
    s.cfn.describe_stacks.side_effect = lambda **kwargs: {"Stacks": [deepcopy(s.stack)]}
    s.cfn.get_template.side_effect = lambda **kwargs: {"TemplateBody": deepcopy(s.template)}
    s.cfn.list_stack_resources.side_effect = lambda **kwargs: {
        "StackResourceSummaries": deepcopy(s.inventory)}
    s.cfn.create_change_set.return_value = {"Id": CHANGE_SET, "StackId": STACK_ID}
    s.cfn.describe_change_set.side_effect = lambda **kwargs: {
        "Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE",
        "Changes": [
            {"Type": "Resource", "ResourceChange": {
                "LogicalResourceId": r["LogicalResourceId"],
                "PhysicalResourceId": r["PhysicalResourceId"],
                "ResourceType": r["ResourceType"], "Action": "Modify",
                "Replacement": "False", "Scope": ["DeletionPolicy", "UpdateReplacePolicy"],
                "Details": [{"Target": {"Attribute": "DeletionPolicy", "RequiresRecreation": "Never"}}],
            }} for r in s.inventory if r["LogicalResourceId"] in s.targets
        ],
    }

    def drift(**kwargs):
        r = next(r for r in s.inventory if r["LogicalResourceId"] == kwargs["LogicalResourceId"])
        return {"StackResourceDrift": {
            "StackResourceDriftStatus": "IN_SYNC", "PhysicalResourceId": r["PhysicalResourceId"]}}

    s.cfn.detect_stack_resource_drift.side_effect = drift

    def execute(**kwargs):
        request = s.cfn.create_change_set.call_args.kwargs
        s.template = json.loads(request.get("TemplateBody") or s.uploaded_template)
        s.stack["LastUpdatedTime"] = "preparation-update"
        s.stack["StackStatus"] = "UPDATE_COMPLETE"
        if "RoleARN" in request:
            s.stack["RoleARN"] = request["RoleARN"]
        for r in s.inventory:
            if r["LogicalResourceId"] in s.targets:
                r["ResourceStatus"] = "UPDATE_COMPLETE"
        s.after_update()

    s.cfn.execute_change_set.side_effect = execute
    return s


def prepare(helper, state, **kwargs):
    return helper.prepare_edge_upgrade(state.cfn, stack_name=STACK, **kwargs)


def aws_error(message, code="ValidationError"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "CloudFormation")


def test_legacy_migration_changes_only_retention_and_uses_previous_parameters(helper, state):
    original = deepcopy(state.template)
    assert prepare(helper, state) is True
    request = state.cfn.create_change_set.call_args.kwargs
    expected = deepcopy(original)
    for logical_id in helper.EDGE_TYPES:
        expected["Resources"][logical_id].update(
            DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    assert json.loads(request["TemplateBody"]) == expected
    assert request["StackName"] == STACK_ID
    assert request["ChangeSetType"] == "UPDATE"
    assert request["Parameters"] == [
        {"ParameterKey": "StackNamePrefix", "UsePreviousValue": True},
        {"ParameterKey": "PrivateValue", "UsePreviousValue": True},
    ]
    assert request["RoleARN"] == SERVICE_ROLE
    assert request["Capabilities"] == ["CAPABILITY_IAM"]
    assert "Description" not in expected["Resources"]["EdgeShaFunction"]["Properties"]
    assert "Description" not in expected["Resources"]["EdgeShaVersion"]["Properties"]
    assert state.cfn.execute_change_set.call_args.kwargs["ChangeSetName"] == CHANGE_SET
    waiter_calls = [c.args[0] for c in state.cfn.get_waiter.call_args_list]
    assert waiter_calls == ["change_set_create_complete", "stack_update_complete"]
    state.cfn.delete_change_set.assert_not_called()
    # Validate mutation requests against the installed SDK's real input schemas.
    model = get_session().get_service_model("cloudformation")
    for method, operation in [("create_change_set", "CreateChangeSet"),
                              ("execute_change_set", "ExecuteChangeSet")]:
        validate_parameters(getattr(state.cfn, method).call_args.kwargs,
                            model.operation_model(operation).input_shape)


@pytest.mark.parametrize("existing,requested,expected", [
    (SERVICE_ROLE, None, SERVICE_ROLE),
    (SERVICE_ROLE, OTHER_SERVICE_ROLE, SERVICE_ROLE),
    (None, OTHER_SERVICE_ROLE, OTHER_SERVICE_ROLE),
    (None, None, None),
])
def test_preserves_existing_service_role_with_optional_fallback(helper, state, existing, requested, expected):
    if not existing:
        state.stack.pop("RoleARN")
    assert prepare(helper, state, role_arn=requested)
    assert state.cfn.create_change_set.call_args.kwargs.get("RoleARN") == expected


def test_absent_stack_is_noop(helper, state):
    state.cfn.describe_stacks.side_effect = aws_error(f"Stack with id {STACK} does not exist")
    assert prepare(helper, state) is False
    state.cfn.get_template.assert_not_called()
    state.cfn.create_change_set.assert_not_called()


@pytest.mark.parametrize("message,code", [
    ("not authorized", "AccessDenied"),
    ("unrelated failure", "ValidationError"),
    ("Stack with id a-different-stack does not exist", "ValidationError"),
])
def test_does_not_treat_errors_as_absent_stack(helper, state, message, code):
    state.cfn.describe_stacks.side_effect = aws_error(message, code)
    with pytest.raises(ClientError):
        prepare(helper, state)
    state.cfn.create_change_set.assert_not_called()


def test_already_retained_is_idempotent(helper, state):
    assert prepare(helper, state)
    state.cfn.reset_mock()
    assert prepare(helper, state) is False
    state.cfn.create_change_set.assert_not_called()
    state.cfn.execute_change_set.assert_not_called()


def test_no_edge_resources_is_noop(helper, state):
    state.inventory = [r for r in state.inventory if r["LogicalResourceId"] == "WebAcl"]
    assert prepare(helper, state) is False
    state.cfn.create_change_set.assert_not_called()


def test_supplied_role_mode_does_not_touch_nonexistent_generated_role(helper, state):
    state.inventory = [r for r in state.inventory if r["LogicalResourceId"] != "EdgeShaRole"]
    state.template["Resources"]["EdgeShaFunction"]["Properties"]["Role"] = OTHER_SERVICE_ROLE
    role_before = deepcopy(state.template["Resources"]["EdgeShaRole"])
    assert prepare(helper, state)
    assert state.template["Resources"]["EdgeShaRole"] == role_before
    assert [call.kwargs["LogicalResourceId"] for call in
            state.cfn.detect_stack_resource_drift.call_args_list] == [
                "EdgeShaFunction", "EdgeShaFunction"]


def test_inventory_pagination_uses_actual_physical_ids(helper, state):
    def pages(**kwargs):
        if "NextToken" not in kwargs:
            return {"StackResourceSummaries": deepcopy(state.inventory[:1]), "NextToken": "page-two"}
        assert kwargs["NextToken"] == "page-two"
        return {"StackResourceSummaries": deepcopy(state.inventory[1:])}
    state.cfn.list_stack_resources.side_effect = pages
    assert prepare(helper, state)


@pytest.mark.parametrize("status", ["UPDATE_IN_PROGRESS", "ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED"])
def test_busy_and_failed_stacks_refused(helper, state, status):
    state.stack["StackStatus"] = status
    with pytest.raises(helper.PreparationError, match="busy or not updateable"):
        prepare(helper, state)
    state.cfn.create_change_set.assert_not_called()


@pytest.mark.parametrize("mutation", [
    lambda s: s.stack.update(LastUpdatedTime="other-deploy"),
    lambda s: s.stack.update(RoleARN=OTHER_SERVICE_ROLE),
    lambda s: s.stack["Parameters"][0].update(ParameterValue="different"),
    lambda s: s.template.update(Description="concurrent-edit"),
    lambda s: s.inventory[0].update(PhysicalResourceId="replacement"),
])
def test_concurrent_change_before_execution_refused(helper, state, mutation):
    original_drift = state.cfn.detect_stack_resource_drift.side_effect
    count = 0

    def drift(**kwargs):
        nonlocal count
        result = original_drift(**kwargs)
        count += 1
        if count == 2:
            mutation(state)
        return result

    state.cfn.detect_stack_resource_drift.side_effect = drift
    with pytest.raises(helper.PreparationError, match="changed during preparation"):
        prepare(helper, state)
    state.cfn.execute_change_set.assert_not_called()
    state.cfn.delete_change_set.assert_called_once_with(StackName=STACK_ID, ChangeSetName=CHANGE_SET)


@pytest.mark.parametrize("drift_status", ["MODIFIED", "DELETED", "NOT_CHECKED"])
def test_drift_or_unavailable_drift_check_prevents_execution(helper, state, drift_status):
    state.cfn.detect_stack_resource_drift.side_effect = None
    state.cfn.detect_stack_resource_drift.return_value = {
        "StackResourceDrift": {"StackResourceDriftStatus": drift_status,
                               "PhysicalResourceId": state.inventory[0]["PhysicalResourceId"]}}
    with pytest.raises(helper.PreparationError, match="drifted or could not be checked"):
        prepare(helper, state)
    state.cfn.execute_change_set.assert_not_called()


@pytest.mark.parametrize("change", [
    {"Action": "Remove"}, {"Action": "Add"}, {"Replacement": "Conditional"},
    {"Replacement": "True"}, {"Scope": ["Properties"]}, {"Scope": ["Tags"]},
    {"Scope": []}, {"PhysicalResourceId": "a-different-version"},
    {"LogicalResourceId": "WebAcl"},
    {"Details": [{"Target": {"Attribute": "Properties", "RequiresRecreation": "Never"}}]},
])
def test_change_set_rejects_any_resource_mutation(helper, state, change):
    page = state.cfn.describe_change_set()
    page["Changes"][0]["ResourceChange"].update(change)
    state.cfn.describe_change_set.side_effect = None
    state.cfn.describe_change_set.return_value = page
    with pytest.raises(helper.PreparationError, match="change set would"):
        prepare(helper, state)
    state.cfn.execute_change_set.assert_not_called()


def test_change_set_checks_every_page(helper, state):
    first = state.cfn.describe_change_set()
    first["NextToken"] = "last"
    last = deepcopy(first)
    last.pop("NextToken")
    last["Changes"][0]["ResourceChange"]["Action"] = "Remove"
    state.cfn.describe_change_set.side_effect = [first, last]
    with pytest.raises(helper.PreparationError):
        prepare(helper, state)
    state.cfn.execute_change_set.assert_not_called()
    assert state.cfn.describe_change_set.call_args.kwargs["NextToken"] == "last"


@pytest.mark.parametrize("waiter", ["change_set_create_complete", "stack_update_complete"])
def test_waiter_failure_never_reports_success(helper, state, waiter):
    waiters = {name: Mock() for name in ("change_set_create_complete", "stack_update_complete")}
    waiters[waiter].wait.side_effect = WaiterError(name=waiter, reason="failure", last_response={})
    state.cfn.get_waiter.side_effect = lambda name: waiters[name]
    with pytest.raises(helper.PreparationError, match="change set|did not complete"):
        prepare(helper, state)
    if waiter == "change_set_create_complete":
        state.cfn.execute_change_set.assert_not_called()


@pytest.mark.parametrize("mutation", [
    lambda s: s.stack.update(StackStatus="UPDATE_ROLLBACK_COMPLETE"),
    lambda s: s.stack.update(RoleARN=OTHER_SERVICE_ROLE),
    lambda s: s.stack["Parameters"][0].update(ParameterValue="different"),
    lambda s: s.stack["Tags"][0].update(Value="different"),
    lambda s: s.stack.update(NotificationARNs=["unexpected-topic"]),
    lambda s: s.template["Resources"]["EdgeShaVersion"].pop("UpdateReplacePolicy"),
    lambda s: s.inventory[1].update(PhysicalResourceId="replacement"),
])
def test_post_update_verification_refuses_drift(helper, state, mutation):
    state.after_update = lambda: mutation(state)
    with pytest.raises(helper.PreparationError, match="changed during preparation"):
        prepare(helper, state)


def test_detects_resource_drift_that_arrives_during_update(helper, state):
    original = state.cfn.detect_stack_resource_drift.side_effect

    def drift(**kwargs):
        result = original(**kwargs)
        if state.cfn.execute_change_set.called:
            result["StackResourceDrift"]["StackResourceDriftStatus"] = "MODIFIED"
        return result

    state.cfn.detect_stack_resource_drift.side_effect = drift
    with pytest.raises(helper.PreparationError, match="drifted or could not be checked"):
        prepare(helper, state)


def test_stale_change_set_cannot_be_executed(helper, state):
    state.cfn.describe_change_set.side_effect = None
    state.cfn.describe_change_set.return_value = {
        "Status": "CREATE_COMPLETE", "ExecutionStatus": "OBSOLETE", "Changes": []}
    with pytest.raises(helper.PreparationError, match="not available"):
        prepare(helper, state)
    state.cfn.execute_change_set.assert_not_called()


def test_changed_stack_before_change_set_creation_does_not_submit(helper, state):
    initial = deepcopy(state.stack)
    state.cfn.describe_stacks.side_effect = [
        {"Stacks": [initial]}, {"Stacks": [{**initial, "LastUpdatedTime": "concurrent"}]}]
    with pytest.raises(helper.PreparationError, match="changed during preparation"):
        prepare(helper, state)
    state.cfn.create_change_set.assert_not_called()


def test_patches_partial_retention_without_changing_properties(helper, state):
    for r in state.template["Resources"].values():
        r["DeletionPolicy"] = "Retain"
    original = deepcopy(state.template)
    assert prepare(helper, state)
    for key, resource in original["Resources"].items():
        expected = deepcopy(resource)
        if key in helper.EDGE_TYPES:
            expected["UpdateReplacePolicy"] = "Retain"
        assert state.template["Resources"][key] == expected


def test_yaml_conversion_preserves_intrinsics_and_dates(helper, state):
    state.cfn.get_template.side_effect = None
    state.cfn.get_template.return_value = {"TemplateBody": LEGACY}
    template = helper.read_template(state.cfn, STACK_ID)
    assert template["AWSTemplateFormatVersion"] == "2010-09-09"
    assert template["Resources"]["EdgeShaFunction"]["Properties"]["Role"] == {
        "Fn::GetAtt": ["EdgeShaRole", "Arn"]}
    assert template["Resources"]["WebAcl"]["Properties"]["Name"] == {
        "Fn::Sub": "${StackNamePrefix}-waf"}


@pytest.mark.parametrize("change", [
    {"Transform": "SomeMacro"},
    {"Description": "x" * 51201},
])
def test_cannot_reprocess_macros_or_upload_oversized_templates(helper, state, change):
    state.template.update(change)
    with pytest.raises(helper.PreparationError):
        prepare(helper, state)
    state.cfn.create_change_set.assert_not_called()


def test_cli_forwards_interface_and_suppresses_sdk_details(helper, state, monkeypatch, capsys):
    client = Mock(return_value=state.cfn)
    monkeypatch.setattr(helper.boto3, "client", client)
    args = ["--stack-name", STACK, "--region", "us-east-1", "--role-arn", OTHER_SERVICE_ROLE]
    assert helper.main(args) == 0
    client.assert_called_once_with("cloudformation", region_name="us-east-1")
    state.cfn.describe_stacks.side_effect = aws_error(SECRET)
    assert helper.main(args) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "Do not continue" in captured.err


@pytest.fixture
def main_state(helper, state):
    role = deepcopy(state.template["Resources"]["EdgeShaRole"])
    state.template["Resources"] = {
        name: deepcopy(role) for name in helper.MAIN_ROLE_PARAMETERS}
    state.template["Resources"].update({
        "BackendLambda": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Role": {"Fn::GetAtt": ["BackendLambdaRole", "Arn"]},
                "Code": {"ZipFile": "existing backend code"}, "Runtime": "python3.13",
            },
        },
        "BackendVersion": {
            "Type": "AWS::Lambda::Version",
            "Properties": {"FunctionName": {"Ref": "BackendLambda"}},
        },
        "BackendLiveAlias": {
            "Type": "AWS::Lambda::Alias",
            "Properties": {"Name": "live", "FunctionName": {"Ref": "BackendLambda"},
                           "FunctionVersion": {"Fn::GetAtt": ["BackendVersion", "Version"]}},
        },
    })
    state.template.pop("Outputs")
    for parameter in helper.MAIN_ROLE_PARAMETERS.values():
        state.template["Parameters"][parameter] = {"Type": "String", "Default": ""}
        state.stack["Parameters"].append({"ParameterKey": parameter, "ParameterValue": ""})
    state.inventory = [
        {"LogicalResourceId": name, "PhysicalResourceId": name + "-generated-id",
         "ResourceType": resource["Type"], "ResourceStatus": "CREATE_COMPLETE"}
        for name, resource in state.template["Resources"].items()
    ]
    return state


def prepare_main(helper, state, roles, **kwargs):
    state.targets = set(roles)
    return prepare(helper, state, kind="main", retain_roles=roles, **kwargs)


@pytest.mark.parametrize("roles", [
    ["BackendLambdaRole"],
    ["IngesterLambdaRole", "PreSignUpFnRole"],
    ["BackendLambdaRole", "IngesterLambdaRole", "SchemaInitLambdaRole",
     "PrewarmLambdaRole", "PreSignUpFnRole", "PostConfirmFnRole"],
])
def test_main_retains_only_selected_roles_preserving_alias_and_function(helper, main_state, roles):
    original = deepcopy(main_state.template)
    assert prepare_main(helper, main_state, roles) is True
    expected = deepcopy(original)
    for name in roles:
        expected["Resources"][name].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    assert main_state.template == expected
    assert {call.kwargs["LogicalResourceId"]
            for call in main_state.cfn.detect_stack_resource_drift.call_args_list} == set(roles)
    request = main_state.cfn.create_change_set.call_args.kwargs
    assert request["ChangeSetName"].startswith("lens-main-retain-")
    assert all(p == {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
               for p in request["Parameters"])
    assert main_state.cfn.execute_change_set.call_args.kwargs["ClientRequestToken"].startswith(
        "lens-main-retain-")


@pytest.mark.parametrize("role", [
    "BackendLambdaRole", "IngesterLambdaRole", "SchemaInitLambdaRole",
    "PrewarmLambdaRole", "PreSignUpFnRole", "PostConfirmFnRole",
])
def test_main_rejects_selected_role_when_prior_parameter_is_already_supplied(helper, main_state, role):
    parameter = helper.MAIN_ROLE_PARAMETERS[role]
    for p in main_state.stack["Parameters"]:
        if p["ParameterKey"] == parameter:
            p["ParameterValue"] = OTHER_SERVICE_ROLE
    with pytest.raises(helper.PreparationError, match="already uses a supplied role"):
        prepare_main(helper, main_state, [role])
    main_state.cfn.create_change_set.assert_not_called()


def test_main_legacy_template_without_optional_role_parameters_can_be_prepared(helper, main_state):
    for param in helper.MAIN_ROLE_PARAMETERS.values():
        main_state.template["Parameters"].pop(param)
    main_state.stack["Parameters"] = [
        p for p in main_state.stack["Parameters"]
        if p["ParameterKey"] not in helper.MAIN_ROLE_PARAMETERS.values()]
    assert prepare_main(helper, main_state, ["BackendLambdaRole"])


def test_main_absent_selected_role_does_not_create_or_protect_other_roles(helper, main_state):
    main_state.inventory = [
        r for r in main_state.inventory if r["LogicalResourceId"] != "BackendLambdaRole"]
    assert prepare_main(helper, main_state, ["BackendLambdaRole"]) is False
    main_state.cfn.create_change_set.assert_not_called()


def test_main_empty_selection_does_not_even_read_aws(helper, main_state):
    assert prepare_main(helper, main_state, []) is False
    assert main_state.cfn.mock_calls == []


@pytest.mark.parametrize("kind,roles", [
    ("edge", ["BackendLambdaRole"]),
    ("main", ["BackendLambda"]),
    ("main", ["ArbitraryRole"]),
    ("unknown", []),
])
def test_invalid_retention_scope_rejected_before_any_aws_call(helper, state, kind, roles):
    with pytest.raises(helper.PreparationError):
        prepare(helper, state, kind=kind, retain_roles=roles)
    assert state.cfn.mock_calls == []


def test_main_detects_selected_role_drift(helper, main_state):
    main_state.cfn.detect_stack_resource_drift.side_effect = None
    main_state.cfn.detect_stack_resource_drift.return_value = {
        "StackResourceDrift": {"StackResourceDriftStatus": "MODIFIED"}}
    with pytest.raises(helper.PreparationError, match="drifted"):
        prepare_main(helper, main_state, ["BackendLambdaRole"])
    main_state.cfn.execute_change_set.assert_not_called()


def test_main_rejects_change_set_that_would_modify_unselected_role(helper, main_state):
    main_state.targets = {"IngesterLambdaRole"}
    page = main_state.cfn.describe_change_set()
    main_state.cfn.describe_change_set.side_effect = None
    main_state.cfn.describe_change_set.return_value = page
    with pytest.raises(helper.PreparationError, match="more than retention"):
        prepare_main(helper, main_state, ["BackendLambdaRole"])
    main_state.cfn.execute_change_set.assert_not_called()


def test_main_rejects_concurrent_parameter_change_after_planning(helper, main_state):
    drift = main_state.cfn.detect_stack_resource_drift.side_effect

    def concurrent(**kwargs):
        result = drift(**kwargs)
        main_state.stack["Parameters"][-1]["ParameterValue"] = OTHER_SERVICE_ROLE
        return result

    main_state.cfn.detect_stack_resource_drift.side_effect = concurrent
    with pytest.raises(helper.PreparationError, match="changed during preparation"):
        prepare_main(helper, main_state, ["BackendLambdaRole"])
    main_state.cfn.execute_change_set.assert_not_called()


def staging_client(state):
    s3 = Mock()
    s3.meta.endpoint_url = "https://s3.us-east-2.amazonaws.com"

    def upload(**kwargs):
        state.uploaded_template = kwargs["Body"]
        return {}

    s3.put_object.side_effect = upload
    return s3


def test_large_main_template_stages_only_prepared_template_in_existing_bucket(helper, main_state):
    main_state.template["Description"] = "x" * 51200
    original = deepcopy(main_state.template)
    s3 = staging_client(main_state)
    bucket = "existing-private-cfn-bucket"
    assert prepare_main(helper, main_state, ["BackendLambdaRole"],
                        s3=s3, template_bucket=bucket)
    uploaded = s3.put_object.call_args.kwargs
    expected = deepcopy(original)
    expected["Resources"]["BackendLambdaRole"].update(
        DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    assert json.loads(uploaded["Body"]) == expected
    assert uploaded["Bucket"] == bucket
    assert uploaded["Key"].startswith("retention/") and uploaded["Key"].endswith(".json")
    assert uploaded["ContentType"] == "application/json"
    assert "ACL" not in uploaded and "ServerSideEncryption" not in uploaded
    assert [c[0] for c in s3.mock_calls] == ["put_object"]
    request = main_state.cfn.create_change_set.call_args.kwargs
    assert "TemplateBody" not in request
    assert request["TemplateURL"] == f"https://s3.us-east-2.amazonaws.com/{bucket}/{uploaded['Key']}"
    model = get_session().get_service_model("cloudformation")
    validate_parameters(request, model.operation_model("CreateChangeSet").input_shape)
    model = get_session().get_service_model("s3")
    validate_parameters(uploaded, model.operation_model("PutObject").input_shape)


def test_large_template_without_bucket_fails_before_change_set(helper, main_state):
    main_state.template["Description"] = "x" * 51200
    with pytest.raises(helper.PreparationError, match="--template-bucket"):
        prepare_main(helper, main_state, ["BackendLambdaRole"])
    main_state.cfn.create_change_set.assert_not_called()


def test_staging_failure_prevents_change_set_submission(helper, main_state):
    main_state.template["Description"] = "x" * 51200
    s3 = staging_client(main_state)
    s3.put_object.side_effect = aws_error("access denied", "AccessDenied")
    with pytest.raises(ClientError):
        prepare_main(helper, main_state, ["BackendLambdaRole"], s3=s3, template_bucket="existing-bucket")
    main_state.cfn.create_change_set.assert_not_called()


def test_small_template_does_not_upload_even_when_bucket_supplied(helper, main_state):
    s3 = staging_client(main_state)
    assert prepare_main(helper, main_state, ["BackendLambdaRole"], s3=s3, template_bucket="existing-bucket")
    assert s3.mock_calls == []
    assert "TemplateBody" in main_state.cfn.create_change_set.call_args.kwargs


def test_oversize_s3_template_is_rejected_before_upload(helper, main_state):
    main_state.template["Description"] = "x" * (1024 * 1024)
    s3 = staging_client(main_state)
    with pytest.raises(helper.PreparationError, match="S3 template limit"):
        prepare_main(helper, main_state, ["BackendLambdaRole"], s3=s3, template_bucket="existing-bucket")
    assert s3.mock_calls == []


def test_current_main_template_can_be_prepared_without_other_semantic_changes(helper):
    template = yaml.load((ROOT / "infra/cloudformation.yaml").read_text(), Loader=helper.TemplateLoader)
    inventory = {
        key: {"ResourceType": "AWS::IAM::Role", "ResourceStatus": "CREATE_COMPLETE",
              "PhysicalResourceId": key + "-generated"}
        for key in helper.MAIN_ROLE_PARAMETERS
    }
    prepared, changed = helper.retention_template(
        template, inventory, helper.retention_targets("main", ["BackendLambdaRole"]))
    assert changed == {"BackendLambdaRole"}
    original_role = deepcopy(template["Resources"]["BackendLambdaRole"])
    prepared["Resources"]["BackendLambdaRole"].pop("DeletionPolicy")
    prepared["Resources"]["BackendLambdaRole"].pop("UpdateReplacePolicy")
    assert prepared == template
    assert original_role == template["Resources"]["BackendLambdaRole"]


def test_main_cli_forwards_repeated_roles_and_staging_bucket(helper, main_state, monkeypatch):
    main_state.targets = {"BackendLambdaRole", "IngesterLambdaRole"}
    s3 = staging_client(main_state)
    client = Mock(side_effect=lambda service, **kwargs: main_state.cfn if service == "cloudformation" else s3)
    monkeypatch.setattr(helper.boto3, "client", client)
    args = ["--stack-name", STACK, "--region", "us-east-2", "--kind", "main",
            "--retain-role", "BackendLambdaRole", "--retain-role", "IngesterLambdaRole",
            "--template-bucket", "existing-bucket"]
    assert helper.main(args) == 0
    assert [c.args for c in client.call_args_list] == [("cloudformation",), ("s3",)]
    for role in main_state.targets:
        assert main_state.template["Resources"][role]["DeletionPolicy"] == "Retain"


def test_main_cli_empty_selection_avoids_even_client_creation(helper, monkeypatch):
    client = Mock()
    monkeypatch.setattr(helper.boto3, "client", client)
    assert helper.main(["--stack-name", STACK, "--region", "us-east-2", "--kind", "main"]) == 0
    client.assert_not_called()
