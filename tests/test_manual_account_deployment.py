"""Manual onboarding and Organizations opt-out, using only local recording fakes."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from ingestion import accounts, config


ROOT = Path(__file__).resolve().parents[1]
CENTRAL = "333333333333"
TARGETS = ["111111111111", "222222222222", "444444444444", "555555555555"]
NO_VALUE = object()


def template(path):
    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, name, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return {name: value}

    Loader.add_multi_constructor("!", intrinsic)
    return yaml.load(path.read_text(), Loader=Loader)


def resolve(value, tpl, parameters):
    """Evaluate the template intrinsics used by the properties under test."""
    if isinstance(value, list):
        resolved = [resolve(item, tpl, parameters) for item in value]
        return [item for item in resolved if item is not NO_VALUE]
    if not isinstance(value, dict):
        return value
    if set(value) == {"Ref"}:
        ref = value["Ref"]
        return NO_VALUE if ref == "AWS::NoValue" else parameters.get(ref, ref)
    if set(value) == {"Sub"}:
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda match: str(parameters.get(match[1], match[0])),
            value["Sub"],
        )
    if set(value) == {"If"}:
        condition, yes, no = value["If"]
        enabled = resolve(tpl["Conditions"][condition], tpl, parameters)
        return resolve(yes if enabled else no, tpl, parameters)
    if set(value) == {"Equals"}:
        left, right = resolve(value["Equals"], tpl, parameters)
        return left == right
    if set(value) == {"Not"}:
        return not resolve(value["Not"][0], tpl, parameters)
    return {key: resolve(item, tpl, parameters) for key, item in value.items()}


@pytest.mark.parametrize("enabled,mode", [("false", "single"), ("true", "discover-org")])
def test_organizations_setting_controls_initial_mode_and_iam_grants(enabled, mode):
    tpl = template(ROOT / "infra/cloudformation.yaml")
    parameters = {
        name: item["Default"] for name, item in tpl["Parameters"].items() if "Default" in item
    }
    parameters["EnableOrganizationsDiscovery"] = enabled
    resources = tpl["Resources"]
    environment = resources["IngesterLambda"]["Properties"]["Environment"]["Variables"]
    assert resolve(environment["MONITORED_ACCOUNTS_MODE"], tpl, parameters) == mode

    statements = resources["IngesterLambdaRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    actions = []
    for statement in resolve(statements, tpl, parameters):
        action = statement["Action"]
        actions.extend(action if isinstance(action, list) else [action])
    organization_actions = {action for action in actions if action.startswith("organizations:")}
    assert organization_actions == ({
        "organizations:ListAccounts", "organizations:DescribeOrganization",
        "organizations:ListAccountsForParent",
    } if enabled == "true" else set())
    assert "sts:AssumeRole" in actions
    assert "cloudwatch:GetMetricData" in actions


@pytest.mark.parametrize("role_name", ["BedrockOpsLensReader", "CompanyBedrockReader"])
def test_reader_name_matches_central_permission_runtime_and_target(monkeypatch, role_name):
    tpl = template(ROOT / "infra/cloudformation.yaml")
    parameters = {
        name: item["Default"] for name, item in tpl["Parameters"].items() if "Default" in item
    }
    parameters.update({"ReaderRoleName": role_name, "AWS::Partition": "aws"})
    resources = tpl["Resources"]
    statements = resolve(
        resources["IngesterLambdaRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"],
        tpl, parameters,
    )
    allowed = [item["Resource"] for item in statements if item["Action"] == "sts:AssumeRole"]
    assert allowed == [f"arn:aws:iam::*:role/{role_name}"]
    environment = resolve(
        resources["IngesterLambda"]["Properties"]["Environment"]["Variables"], tpl, parameters,
    )
    assert environment["BEDROCK_OPS_LENS_ROLE_NAME"] == role_name

    reader = template(ROOT / "infra/monitored-account-role.yaml")
    target_name = resolve(
        reader["Resources"]["BedrockOpsLensReaderRole"]["Properties"]["RoleName"],
        reader, {"RoleName": role_name},
    )
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": CENTRAL}
    sts.assume_role.return_value = {"Credentials": {
        "AccessKeyId": "fixture-key", "SecretAccessKey": "fixture-secret",
        "SessionToken": "fixture-token",
    }}
    monkeypatch.setattr(accounts.boto3, "client", lambda service, **kwargs: sts)
    monkeypatch.setattr(accounts.boto3, "Session", Mock())
    accounts._SessionCache().session_for(
        TARGETS[0], role_name=environment["BEDROCK_OPS_LENS_ROLE_NAME"],
    )
    requested_arn = sts.assume_role.call_args.kwargs["RoleArn"]
    assert requested_arn == f"arn:aws:iam::{TARGETS[0]}:role/{target_name}"
    assert requested_arn == allowed[0].replace("::*:", f"::{TARGETS[0]}:")


def test_manual_reader_trust_uses_c_ingester_without_stackset_roles():
    tpl = template(ROOT / "infra/monitored-account-role.yaml")
    role_arn = f"arn:aws:iam::{CENTRAL}:role/lens-ingester"
    parameters = {
        "CentralAccountId": CENTRAL, "CentralIngesterRoleArn": role_arn,
        "ExternalId": "fixture-external-id",
    }
    reader = tpl["Resources"]["BedrockOpsLensReaderRole"]["Properties"]
    trust = resolve(reader["AssumeRolePolicyDocument"], tpl, parameters)
    assert trust["Statement"][0]["Principal"] == {"AWS": role_arn}
    assert trust["Statement"][0]["Action"] == "sts:AssumeRole"
    assert trust["Statement"][0]["Condition"] == {
        "StringEquals": {"sts:ExternalId": "fixture-external-id"}
    }
    role_types = [
        resource["Type"] for resource in tpl["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
    ]
    assert role_types == ["AWS::IAM::Role"]
    assert "AWSCloudFormationStackSet" not in json.dumps(trust)


@pytest.fixture
def deployment_settings(tmp_path):
    """Run the actual discovery-setting and parameter-file blocks from deploy.sh."""
    source = (ROOT / "deploy.sh").read_text()
    setting = source.split("# Organizations discovery policy.", 1)[1].split(
        "# -----------------------------------------------------------------------------", 1
    )[0]
    setting = "# Organizations discovery policy." + setting
    parameters = 'cat > "$PARAMS_JSON" <<EOF\n' + source.split(
        'cat > "$PARAMS_JSON" <<EOF\n', 1
    )[1].split("\nEOF", 1)[0] + "\nEOF\n"
    script = tmp_path / "settings.sh"
    script.write_text("set -euo pipefail\nROOT=\"$1\"\nREGION=us-east-1\n" + setting + parameters)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    aws = bin_dir / "aws"
    aws.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:2] == ["cloudformation", "describe-stacks"], args
with open(os.environ["FAKE_CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
query = args[args.index("--query") + 1]
prefix = "FAKE_READER" if "`ReaderRoleName`" in query else "FAKE_DISCOVERY"
default = "None" if prefix == "FAKE_READER" else "false"
if prefix == "FAKE_READER":
    print(json.dumps(os.environ.get(prefix + "_VALUE")))
else:
    print(os.environ.get(prefix + "_VALUE", default))
raise SystemExit(int(os.environ.get(prefix + "_RC", "0")))
''')
    aws.chmod(0o755)
    result_file = tmp_path / "parameters.json"
    environment = {
        "PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_CALLS": str(calls),
        "PARAMS_JSON": str(result_file), "ALLOWED_EMAIL_DOMAINS": "example.com",
        "ECR_URI": "example.invalid/lens", "BEDROCK_LOGS_BUCKET": "",
        "BEDROCK_LOGS_REGION": "", "COGNITO_DOMAIN_PREFIX": "lens-example",
        "COGNITO_SELF_SIGNUP": "disabled", "MAIN_STACK": "BedrockOpsLens-example",
        "EDGE_SHA_VERSION_ARN": "fixture-edge-arn", "WEB_ACL_ARN": "fixture-waf-arn",
    }

    def run(**overrides):
        completed = subprocess.run(
            ["/bin/bash", str(script), str(tmp_path)],
            env={**environment, **overrides}, capture_output=True, text=True, timeout=10,
        )
        observed = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        params = (
            {item["ParameterKey"]: item["ParameterValue"]
             for item in json.loads(result_file.read_text())}
            if result_file.exists() else {}
        )
        return completed, observed, params

    return SimpleNamespace(run=run, root=tmp_path)


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_explicit_discovery_choice_reaches_cloudformation_without_lookup(deployment_settings, enabled):
    completed, calls, params = deployment_settings.run(ENABLE_ORGANIZATIONS_DISCOVERY=enabled)
    assert completed.returncode == 0, completed.stderr
    assert params["EnableOrganizationsDiscovery"] == enabled
    assert calls == []


def test_redeploy_preserves_disabled_discovery(deployment_settings):
    (deployment_settings.root / ".deploy-stack-name").write_text("existing")
    completed, calls, params = deployment_settings.run(FAKE_DISCOVERY_VALUE="false")
    assert completed.returncode == 0, completed.stderr
    assert params["EnableOrganizationsDiscovery"] == "false"
    assert calls[0][calls[0].index("--stack-name") + 1] == "BedrockOpsLens-existing"


@pytest.mark.parametrize("response,rc", [
    ("None", "0"), ("", "0"), ("Stack with id BedrockOpsLens-existing does not exist", "1"),
])
def test_legacy_or_missing_stack_retains_existing_default(deployment_settings, response, rc):
    completed, _, params = deployment_settings.run(
        STACK_NAME_SUFFIX="existing", FAKE_DISCOVERY_VALUE=response, FAKE_DISCOVERY_RC=rc,
    )
    assert completed.returncode == 0, completed.stderr
    assert params["EnableOrganizationsDiscovery"] == "true"
    assert params["ReaderRoleName"] == "BedrockOpsLensReader"


def test_fresh_default_does_not_require_a_stack_lookup(deployment_settings):
    completed, calls, params = deployment_settings.run()
    assert completed.returncode == 0, completed.stderr
    assert params["EnableOrganizationsDiscovery"] == "true"
    assert params["ReaderRoleName"] == "BedrockOpsLensReader"
    assert calls == []


def test_failed_lookup_cannot_silently_enable_organizations(deployment_settings):
    completed, _, params = deployment_settings.run(
        STACK_NAME_SUFFIX="existing", FAKE_DISCOVERY_VALUE="AccessDenied", FAKE_DISCOVERY_RC="1",
    )
    assert completed.returncode != 0
    assert params == {}
    assert "could not read" in completed.stderr


def test_invalid_discovery_setting_fails_before_parameter_generation(deployment_settings):
    completed, calls, params = deployment_settings.run(ENABLE_ORGANIZATIONS_DISCOVERY="maybe")
    assert completed.returncode != 0
    assert calls == []
    assert params == {}


@pytest.mark.parametrize("role_name", ["CompanyBedrockReader", "Reader+=,.@_-123", "R" * 64])
def test_custom_reader_name_reaches_central_deploy_parameters(deployment_settings, role_name):
    completed, calls, params = deployment_settings.run(BEDROCK_OPS_LENS_ROLE_NAME=role_name)
    assert completed.returncode == 0, completed.stderr
    assert calls == []
    assert params["ReaderRoleName"] == role_name


@pytest.mark.parametrize("role_name", ["CompanyBedrockReader", "None"])
def test_redeploy_preserves_custom_reader_name(deployment_settings, role_name):
    (deployment_settings.root / ".deploy-stack-name").write_text("existing")
    completed, calls, params = deployment_settings.run(FAKE_READER_VALUE=role_name)
    assert completed.returncode == 0, completed.stderr
    assert params["ReaderRoleName"] == role_name
    assert any("`ReaderRoleName`" in args[args.index("--query") + 1] for args in calls)


def test_explicit_reader_override_does_not_read_previous_value(deployment_settings):
    completed, calls, params = deployment_settings.run(
        STACK_NAME_SUFFIX="existing", ENABLE_ORGANIZATIONS_DISCOVERY="false",
        BEDROCK_OPS_LENS_ROLE_NAME="NewReader", FAKE_READER_VALUE="OldReader",
    )
    assert completed.returncode == 0, completed.stderr
    assert calls == []
    assert params["ReaderRoleName"] == "NewReader"


def test_unreadable_reader_parameter_cannot_reset_to_default(deployment_settings):
    completed, _, params = deployment_settings.run(
        STACK_NAME_SUFFIX="existing", ENABLE_ORGANIZATIONS_DISCOVERY="false",
        FAKE_READER_VALUE="AccessDenied", FAKE_READER_RC="1",
    )
    assert completed.returncode != 0
    assert "ReaderRoleName" in completed.stderr
    assert params == {}


@pytest.mark.parametrize("role_name", ["*", "team/Reader", 'Reader"Bad', "R" * 65])
def test_invalid_reader_name_cannot_generate_an_iam_permission(deployment_settings, role_name):
    completed, calls, params = deployment_settings.run(BEDROCK_OPS_LENS_ROLE_NAME=role_name)
    assert completed.returncode != 0
    assert calls == []
    assert params == {}
    assert "valid IAM role name" in completed.stderr
    for path, parameter in [
        ("infra/cloudformation.yaml", "ReaderRoleName"),
        ("infra/monitored-account-role.yaml", "RoleName"),
        ("infra/stackset-execution-role.yaml", "ReaderRoleName"),
    ]:
        rule = template(ROOT / path)["Parameters"][parameter]["AllowedPattern"]
        assert re.fullmatch(rule, role_name) is None


def test_explicit_runtime_discovery_never_uses_organizations(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("monitored_accounts:\n  mode: discover-org\n")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setenv("MONITORED_ACCOUNTS_MODE", "explicit")
    monkeypatch.setenv("MONITORED_ACCOUNTS_IDS", ",".join(TARGETS))
    client = Mock(side_effect=AssertionError("explicit discovery must not call AWS"))
    monkeypatch.setattr(accounts.boto3, "client", client)
    monkeypatch.setattr(accounts, "_session_cache", None)

    resolved = accounts.discover_accounts(SimpleNamespace(
        accounts=None, accounts_config=None, discover_org=False,
    ))

    assert [account.accountId for account in resolved] == TARGETS
    client.assert_not_called()


def test_c_uses_target_reader_roles_and_local_credentials_for_itself(monkeypatch):
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": CENTRAL}
    sts.assume_role.return_value = {"Credentials": {
        "AccessKeyId": "fixture-key", "SecretAccessKey": "fixture-secret",
        "SessionToken": "fixture-token",
    }}
    calls = []

    def client(service, **kwargs):
        calls.append(service)
        assert service == "sts"
        return sts

    session_factory = Mock(side_effect=lambda **kwargs: SimpleNamespace(credentials=kwargs))
    monkeypatch.setattr(accounts.boto3, "client", client)
    monkeypatch.setattr(accounts.boto3, "Session", session_factory)
    monkeypatch.delenv("BEDROCK_OPS_LENS_FORCE_ASSUME_SELF", raising=False)
    cache = accounts._SessionCache()

    for target in TARGETS:
        cache.session_for(
            target, role_name="BedrockOpsLensReader", external_id="fixture-external-id"
        )
    local = cache.session_for(CENTRAL)

    assert calls == ["sts"]
    assert [call.kwargs["RoleArn"] for call in sts.assume_role.call_args_list] == [
        f"arn:aws:iam::{target}:role/BedrockOpsLensReader" for target in TARGETS
    ]
    assert all(
        call.kwargs["ExternalId"] == "fixture-external-id"
        for call in sts.assume_role.call_args_list
    )
    assert local.credentials == {}
