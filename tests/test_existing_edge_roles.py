"""Offline checks for edge role transitions and pre-created execution roles."""
from __future__ import annotations

from fnmatch import fnmatchcase
from itertools import product
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = "BedrockOpsLens-test"
REGION = "us-east-2"
ACCOUNT = "123456789012"
GENERATED_ROLE = f"arn:aws:iam::{ACCOUNT}:role/lens-edge-generated"
ROLE_A = f"arn:aws:iam::{ACCOUNT}:role/customer/edge-a"
ROLE_B = f"arn:aws:iam::{ACCOUNT}:role/customer/edge-b"


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, name, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {name: value}


CfnLoader.add_multi_constructor("!", _tag)


def _load(path):
    return yaml.load((ROOT / path).read_text(), Loader=CfnLoader)


def _resolve(value, values, conditions):
    """Evaluate only the intrinsics used by the policy and edge assertions."""
    if isinstance(value, list):
        return [_resolve(item, values, conditions) for item in value]
    if not isinstance(value, dict):
        return value
    if "If" in value:
        condition, yes, no = value["If"]
        return _resolve(yes if conditions[condition] else no, values, conditions)
    if "Ref" in value:
        return values[value["Ref"]]
    if "GetAtt" in value:
        key = value["GetAtt"]
        return values[key if isinstance(key, str) else ".".join(key)]
    if "Sub" in value:
        text = value["Sub"]
        scope = values
        if isinstance(text, list):
            text, bindings = text
            scope = {**values, **_resolve(bindings, values, conditions)}
        return re.sub(r"\$\{([^}]+)\}", lambda m: str(scope[m[1]]), text)
    return {k: _resolve(v, values, conditions) for k, v in value.items()}


@pytest.fixture(scope="module")
def edge():
    return _load("infra/edge-bootstrap.yaml")


def _edge_properties(edge, resource, role, generated=GENERATED_ROLE):
    return _resolve(
        edge["Resources"][resource]["Properties"],
        {"EdgeRoleArn": role, "EdgeShaRole.Arn": generated,
         "EdgeShaFunction": "lens-edge-function"},
        {"CreateEdgeRole": not role},
    )


@pytest.mark.parametrize("before,after", [
    ("", ROLE_A), (ROLE_A, ROLE_B), (ROLE_A, ""),
])
def test_role_transition_changes_the_published_snapshot(edge, before, after):
    old_function = _edge_properties(edge, "EdgeShaFunction", before)
    new_function = _edge_properties(edge, "EdgeShaFunction", after)
    old_version = _edge_properties(edge, "EdgeShaVersion", before)
    new_version = _edge_properties(edge, "EdgeShaVersion", after)
    assert old_function["Role"] != new_function["Role"]
    # Description is a replacement property of AWS::Lambda::Version.
    assert old_version["Description"] != new_version["Description"]
    assert new_version["Description"] == new_function["Role"]
    # The function reference orders publishing after the configuration update.
    assert edge["Resources"]["EdgeShaVersion"]["Properties"]["FunctionName"] == {
        "Ref": "EdgeShaFunction"}


def test_recreated_generated_role_gets_a_new_version(edge):
    """Returning from a supplied role must not reuse the retired role's snapshot."""
    old = _edge_properties(edge, "EdgeShaVersion", "")
    new = _edge_properties(edge, "EdgeShaVersion", "", GENERATED_ROLE + "-next")
    assert old["Description"] != new["Description"]


def test_legacy_upgrade_changes_latest_even_without_a_role_change(edge):
    """Legacy $LATEST has no Description; version metadata alone cannot publish."""
    fn = _edge_properties(edge, "EdgeShaFunction", "")
    version = _edge_properties(edge, "EdgeShaVersion", "")
    assert fn["Description"]
    assert version["Description"]
    assert fn["Role"] == GENERATED_ROLE


@pytest.mark.parametrize("resource", [
    "EdgeShaRole", "EdgeShaVersion", "EdgeShaFunction",
])
def test_replica_dependencies_survive_removal_and_replacement(edge, resource):
    definition = edge["Resources"][resource]
    assert definition["DeletionPolicy"] == "Retain"
    assert definition["UpdateReplacePolicy"] == "Retain"


def test_supplied_role_is_not_managed_and_version_is_exported(edge):
    assert edge["Resources"]["EdgeShaRole"]["Condition"] == "CreateEdgeRole"
    assert edge["Conditions"]["CreateEdgeRole"] == {
        "Equals": [{"Ref": "EdgeRoleArn"}, ""]}
    assert edge["Outputs"]["EdgeShaVersionArn"]["Value"] == {"Ref": "EdgeShaVersion"}
    assert _edge_properties(edge, "EdgeShaFunction", ROLE_A)["Role"] == ROLE_A


def test_role_description_fits_lambda_limit(edge):
    param = edge["Parameters"]["EdgeRoleArn"]
    assert param["MaxLength"] <= 256
    role = ROLE_A + "x" * (param["MaxLength"] - len(ROLE_A))
    assert len(_edge_properties(edge, "EdgeShaVersion", role)["Description"]) <= 256
    assert len(_edge_properties(edge, "EdgeShaVersion", "")["Description"]) <= 256


def _statements(template, role, org=False, logs=False, proxy=False):
    values = {
        "AWS::Partition": "aws", "AWS::AccountId": ACCOUNT, "AWS::Region": REGION,
        "AWS::StackName": STACK, "LensStackName": STACK, "AWS::NoValue": None,
        "ReaderRoleName": "BedrockOpsLensReader",
        "BedrockLogsBucket": "lens-invocation-logs",
        "ProxyEventsBucket": "lens-proxy-events",
        "DbSecret": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{STACK}/db-AbCdEf",
        "SecretsKey.Arn": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test",
        "UserPool.Arn": f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/test",
        "LivePullFunction.Arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{STACK}-live-pull",
        "SchemaInitDlq.Arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{STACK}-schema-init-dlq",
    }
    conditions = {
        "OrganizationsDiscoveryEnabled": org,
        "HasBedrockLogsBkt": logs, "HasProxyEventsBkt": proxy,
    }
    return [
        resolved
        for policy in template["Resources"][role]["Properties"].get("Policies", [])
        for statement in policy["PolicyDocument"]["Statement"]
        if (resolved := _resolve(statement, values, conditions)) is not None
    ]


def _list(value):
    return value if isinstance(value, list) else [value]


def _actions(statements):
    return {a for st in statements if st["Effect"] == "Allow" for a in _list(st["Action"])}


def _grants_resource(statements, action, resource):
    """Check action/resource matching; conditions are tested separately below."""
    return any(
        st["Effect"] == "Allow"
        and any(fnmatchcase(action, a) for a in _list(st["Action"]))
        and any(fnmatchcase(resource, r) for r in _list(st["Resource"]))
        for st in statements
    )


@pytest.fixture(scope="module")
def roles():
    return _load("infra/examples/existing-execution-roles.yaml")


@pytest.fixture(scope="module")
def main():
    return _load("infra/cloudformation.yaml")


@pytest.mark.parametrize("central,example", [
    ("BackendLambdaRole", "BackendRole"),
    ("IngesterLambdaRole", "IngesterRole"),
    ("SchemaInitLambdaRole", "SchemaInitRole"),
    ("PrewarmLambdaRole", "PrewarmRole"),
    ("PreSignUpFnRole", "PreSignUpRole"),
    ("PostConfirmFnRole", "PostConfirmRole"),
])
@pytest.mark.parametrize("org,logs,proxy", list(product([False, True], repeat=3)))
def test_example_role_permissions_match_main_template(main, roles, central, example, org, logs, proxy):
    expected = _statements(main, central, org, logs, proxy)
    actual = _statements(roles, example, org, logs, proxy)
    assert _actions(actual) == _actions(expected)
    for statement in expected:
        for action in _list(statement["Action"]):
            for resource in _list(statement["Resource"]):
                # The example narrows SSM to this stack's actual app-client
                # parameter instead of the main template's all-stacks wildcard.
                resource = resource.replace(
                    "parameter/bedrock-ops-lens/*",
                    f"parameter/bedrock-ops-lens/{STACK}/cognito-app-client-id",
                )
                assert _grants_resource(actual, action, resource), (example, action, resource)
    lhs = main["Resources"][central]["Properties"]
    rhs = roles["Resources"][example]["Properties"]
    assert lhs["ManagedPolicyArns"] == rhs["ManagedPolicyArns"]
    assert lhs["AssumeRolePolicyDocument"] == rhs["AssumeRolePolicyDocument"]
    for statement in actual:
        if "kms:Decrypt" in _list(statement["Action"]):
            assert statement["Condition"] == {
                "StringEquals": {"kms:ViaService": f"secretsmanager.{REGION}.amazonaws.com"}}


def test_backend_can_invoke_only_its_own_live_pull_function(roles):
    statements = _statements(roles, "BackendRole")
    prefix = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
    assert _grants_resource(statements, "lambda:InvokeFunction", prefix + STACK + "-live-pull")
    for target in [STACK + "-ingester", STACK + "-schema-init", "other-live-pull",
                   STACK + "-live-pull:untrusted"]:
        assert not _grants_resource(statements, "lambda:InvokeFunction", prefix + target)
    assert not _grants_resource(statements, "sts:AssumeRole",
                                f"arn:aws:iam::{ACCOUNT}:role/BedrockOpsLensReader")


def test_shared_ingester_role_reads_only_its_own_configuration(roles):
    statements = _statements(roles, "IngesterRole")
    prefix = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
    assert _grants_resource(statements, "lambda:GetFunctionConfiguration", prefix + STACK + "-ingester")
    assert not _grants_resource(statements, "lambda:GetFunctionConfiguration", prefix + "other-ingester")
    assert not _grants_resource(statements, "lambda:InvokeFunction", prefix + STACK + "-ingester")


def test_ingester_can_discover_application_inference_profiles(roles):
    statements = _statements(roles, "IngesterRole")
    assert _grants_resource(statements, "bedrock:ListInferenceProfiles", "*")
    assert _grants_resource(
        statements, "bedrock:GetInferenceProfile",
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/example",
    )
