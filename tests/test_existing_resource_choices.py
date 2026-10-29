"""Exercise resource choices and deployment scope without making AWS calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from ingestion.config import Config, IamConfig, MonitoredAccountsConfig, load_config

ROOT = Path(__file__).resolve().parents[1]
ROLE = "arn:aws:iam::123456789012:role/example"
ACL = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/example/12345678-1234-1234-1234-123456789012"
OU = "ou-abcd-12345678"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def run_plan(tmp_path, config, prior=None, edge=None):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    stack = {"Stacks": [{"StackName": "Lens-test", "Parameters": [
        {"ParameterKey": k, "ParameterValue": v} for k, v in prior.items()
    ]}]} if prior is not None else {"Stacks": []}
    (tmp_path / "stack.json").write_text(json.dumps(stack))
    (tmp_path / "edge.json").write_text(json.dumps({"Stacks": [{"Parameters": [
        {"ParameterKey": k, "ParameterValue": v} for k, v in (edge or {}).items()
    ]}]}))
    script = r'''
set -euo pipefail
ROOT="$1"; REGION=us-east-2; EDGE_REGION=us-east-1
ACCOUNT_ID=123456789012; MAIN_STACK=Lens-test; EDGE_STACK=Lens-edge; ACTION=dry-run
source "$2"
ER_PY="$3"
aws() {
    if [[ "$2" == "describe-stack-resource" ]]; then
        if [[ "$*" == *DbSecret* ]]; then
            echo arn:aws:secretsmanager:us-east-2:123456789012:secret:old-secret-ABC123
        else
            echo https://sqs.us-east-2.amazonaws.com/123456789012/old-queue
        fi
    elif [[ "$*" == *Lens-edge* ]]; then
        cat "$ROOT/edge.json"
    elif [[ "$4" == Lens-test ]]; then
        if [[ "$EXISTING_STACK" == yes ]]; then cat "$ROOT/stack.json"
        else echo "Stack does not exist" >&2; return 1; fi
    else return 9
    fi
}
_er_validate() { :; }
_er_summary() { :; }
existing_resources_plan
printf 'RESULT:%s|%s|%s|%s|%s|%s|%s\n' "$ER_WAF" "$ER_STABLE_NAMES" \
    "$ER_ROLE_BACKEND" "$ER_LAMBDA_SG" "$ER_USE_PREVIOUS" "$ER_SECRET_ARN" "$ER_SCHEMA_DLQ_ARN"
printf 'PARAM:%s\n' ${ER_EDGE_PARAMS[@]+"${ER_EDGE_PARAMS[@]}"}
rm -f "$ER_STACK_FILE"
'''
    result = subprocess.run(
        ["bash", "-c", script, "--", str(tmp_path),
         str(ROOT / "scripts/existing-resources.sh"), sys.executable],
        env={**os.environ, "EXISTING_STACK": "yes" if prior is not None else "no",
             "EXISTING_RESOURCES": ""}, text=True, capture_output=True,
    )
    return result


def fields(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return next(x.removeprefix("RESULT:").split("|")
                for x in result.stdout.splitlines() if x.startswith("RESULT:"))


@pytest.mark.parametrize("config", [{}, {"existing_resources": {}},
                                  {"existing_resources": {"roles": {"backend": ""}}}])
def test_first_install_omissions_create(tmp_path, config):
    result = run_plan(tmp_path, config)
    assert fields(result)[:5] == ["new", "false", "", "", "0"]
    assert "PARAM:CreateWebAcl=true" in result.stdout
    assert "PARAM:EdgeRoleArn=\n" in result.stdout


def test_only_waf_none_needs_to_be_supplied(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"waf": "none"}})
    assert fields(result)[0] == "none"
    assert "PARAM:CreateWebAcl=false" in result.stdout


def test_each_supplied_role_is_independent(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"roles": {"backend": ROLE}}})
    assert fields(result)[1:3] == ["true", ROLE]


def test_update_partial_block_preserves_roles_network_sgs_and_naming(tmp_path):
    prior = {"ExistingVpcId": "vpc-12345678",
             "ExistingPrivateSubnetIds": "subnet-12345678,subnet-abcdefgh".replace("abcdefgh", "abcdef12"),
             "ExistingLambdaSecurityGroupId": "sg-12345678", "BackendRoleArn": ROLE,
             "StableResourceNames": "true", "WebAclArn": ""}
    result = run_plan(tmp_path, {"existing_resources": {"waf": "none"}}, prior,
                      {"EdgeRoleArn": ROLE, "CreateWebAcl": "false"})
    assert fields(result)[:5] == ["none", "true", ROLE, "sg-12345678", "0"]
    assert f"PARAM:EdgeRoleArn={ROLE}" in result.stdout
    assert fields(result)[5].endswith("old-secret-ABC123")
    assert fields(result)[6].endswith(":old-queue")


def test_update_single_sg_preserves_omitted_network_ids(tmp_path):
    prior = {"ExistingVpcId": "vpc-12345678",
             "ExistingPrivateSubnetIds": "subnet-12345678,subnet-abcdef12"}
    result = run_plan(tmp_path, {"existing_resources": {
        "network": {"lambda_security_group_id": "sg-12345678"}}}, prior)
    assert fields(result)[3] == "sg-12345678"


@pytest.mark.parametrize("before,after", [("", ROLE), (ROLE, "new")])
def test_role_change_does_not_change_existing_secret_naming(tmp_path, before, after):
    result = run_plan(tmp_path, {"existing_resources": {"roles": {"backend": after}}},
                      {"BackendRoleArn": before, "StableResourceNames": "false"})
    assert fields(result)[1] == "false"


def test_upgrade_from_cc_template_preserves_deterministic_name(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"roles": "new"}},
                      {"BackendRoleArn": ROLE})
    assert fields(result)[1:3] == ["true", ""]


def test_no_block_preserves_all_choices_on_upgrade(tmp_path):
    result = run_plan(tmp_path, {}, {"BackendRoleArn": ROLE})
    assert fields(result)[:5] == ["previous", "true", "", "", "1"]
    assert "PARAM:CreateWebAcl=" not in result.stdout


def test_none_to_new_waf_and_supplied_to_new_edge_are_explicit(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"waf": "new", "roles": {"edge": "new"}}},
                      {"WebAclArn": ""}, {"CreateWebAcl": "false", "EdgeRoleArn": ROLE})
    fields(result)
    assert "PARAM:CreateWebAcl=true" in result.stdout
    assert "PARAM:EdgeRoleArn=\n" in result.stdout


def test_reusing_owned_acl_does_not_delete_it(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"waf": ACL}},
                      {"WebAclArn": ACL}, {"CreateWebAcl": "true"})
    assert fields(result)[0] == "new"
    assert "PARAM:CreateWebAcl=true" in result.stdout


def test_removing_attached_owned_acl_fails_before_any_mutation(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"waf": "none"}},
                      {"WebAclArn": ACL}, {"CreateWebAcl": "true"})
    assert result.returncode == 2
    assert "Removing or replacing it" in result.stderr


def test_blank_optional_prompt_selects_default(tmp_path):
    result = subprocess.run(["bash", "-c", r'''
ROOT="$1"; source "$2"
_er_ask "Choice: " "^(new|none)$" answer "new or none" new
printf '%s' "$answer"
''', "--", str(tmp_path), str(ROOT / "scripts/existing-resources.sh")],
        input="\n", capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == "new"


def test_missing_required_existing_network_identifiers_abort(tmp_path):
    result = run_plan(tmp_path, {"existing_resources": {"network": {"vpc_id": "vpc-12345678"}}})
    assert result.returncode == 2
    assert "private_subnet_ids" in result.stderr


def test_parameter_preservation_uses_validated_stack_snapshot(tmp_path):
    deploy = (ROOT / "deploy.sh").read_text()
    code = deploy.split("<<'PYKEYS'\n", 1)[1].split("\nPYKEYS", 1)[0]
    snapshot = tmp_path / "stack.json"
    snapshot.write_text(json.dumps({"Stacks": [{"Parameters": [
        {"ParameterKey": "ExistingDbSecurityGroupId", "ParameterValue": "sg-12345678"},
        {"ParameterKey": "BackendRoleArn", "ParameterValue": ROLE},
    ]}]}))
    result = subprocess.run([sys.executable, "-c", code, str(snapshot)],
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "ExistingDbSecurityGroupId,BackendRoleArn"
    snapshot.write_text("{}")
    result = subprocess.run([sys.executable, "-c", code, str(snapshot)],
                            text=True, capture_output=True)
    assert result.returncode != 0  # Never reset supplied resources on a failed lookup.


@pytest.mark.parametrize(("previous", "planned", "expected"), [
    (None, [{"ParameterKey": "BackendRoleArn", "ParameterValue": ROLE}], []),
    ({}, [{"ParameterKey": "BackendRoleArn", "ParameterValue": ROLE}], ["BackendLambdaRole"]),
    ({"BackendRoleArn": ROLE}, [{"ParameterKey": "BackendRoleArn", "UsePreviousValue": True}], []),
    ({"BackendRoleArn": ROLE}, [{"ParameterKey": "BackendRoleArn", "ParameterValue": ROLE + "-new"}], []),
    ({"BackendRoleArn": ROLE}, [{"ParameterKey": "BackendRoleArn", "ParameterValue": ""}], []),
    ({}, [{"ParameterKey": "IngesterRoleArn", "ParameterValue": ROLE},
          {"ParameterKey": "SchemaInitRoleArn", "ParameterValue": ROLE + "-schema"}],
     ["IngesterLambdaRole", "SchemaInitLambdaRole"]),
])
def test_retain_only_generated_roles_being_replaced(tmp_path, previous, planned, expected):
    deploy = (ROOT / "deploy.sh").read_text()
    code = deploy.split("<<'PYRETENTION'\n", 1)[1].split("\nPYRETENTION", 1)[0]
    stack = tmp_path / "stack.json"
    stack.write_text(json.dumps({"Stacks": [] if previous is None else [{
        "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in previous.items()]
    }]}))
    params = tmp_path / "params.json"
    params.write_text(json.dumps(planned))
    result = subprocess.run([sys.executable, "-c", code, str(stack), str(params)],
                            text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == expected


@pytest.mark.parametrize(("response", "status", "expected_code", "waited"), [
    ("AccessDenied: role cannot be passed", 1, 1, False),
    ("ValidationError: invalid subnet", 1, 1, False),
    ("No updates are to be performed.", 1, 0, False),
    ("stack-id", 0, 0, True),
])
def test_rejected_main_update_stops_deployment(tmp_path, response, status, expected_code, waited):
    deploy = (ROOT / "deploy.sh").read_text()
    block = deploy.split('    if UPDATE_OUT="$(aws cloudformation update-stack', 1)[1]
    block = 'if UPDATE_OUT="$(aws cloudformation update-stack' + block.split(
        "\nfi\n\n# Sync SPA", 1)[0]
    script = r'''
set -euo pipefail
MAIN_STACK=Lens-test; REGION=us-east-2; MAIN_TEMPLATE_URL=https://example.com/template
PARAMS_JSON=/unused; ER_CFN_ROLE_ARGS=()
aws() {
    if [[ "$2" == update-stack ]]; then
        printf '%s' "$RESPONSE"
        return "$STATUS"
    fi
    printf 'wait\n' >> "$WAIT_LOG"
}
''' + block
    log = tmp_path / "wait.log"
    result = subprocess.run(["bash", "-c", script],
                            env={**os.environ, "RESPONSE": response, "STATUS": str(status),
                                 "WAIT_LOG": str(log)},
                            text=True, capture_output=True)
    assert result.returncode == expected_code, result.stderr
    assert log.exists() is waited
    if expected_code:
        assert "main stack update was rejected" in result.stderr


def test_new_ou_scope_is_present_before_first_ingest():
    cfg = Config(monitored_accounts=MonitoredAccountsConfig(mode="discover-ou", ou_ids=(OU,)))
    params = module("deployment-scope").scope_parameters(cfg, None, {}, True)
    values = {p["ParameterKey"]: p["ParameterValue"] for p in params}
    assert values["MonitoredAccountsMode"] == "discover-ou"
    assert values["MonitoredOuIds"] == OU


def test_upgrades_preserve_runtime_scope_instead_of_old_baked_config():
    cfg = Config(monitored_accounts=MonitoredAccountsConfig(mode="discover-org"),
                 iam=IamConfig(external_id="old-id"))
    runtime = {"Environment": {"Variables": {
        "MONITORED_ACCOUNTS_MODE": "discover-ou", "MONITORED_OU_IDS": OU,
        "BEDROCK_OPS_LENS_EXTERNAL_ID": "customer-value",
    }}}
    params = module("deployment-scope").scope_parameters(cfg, {"StackName": "Lens"}, runtime, True)
    values = {p["ParameterKey"]: p["ParameterValue"] for p in params}
    assert values["MonitoredAccountsMode"] == "discover-ou"
    assert values["MonitoredOuIds"] == OU
    assert values["ReaderExternalId"] == "customer-value"


def test_disabled_organizations_rejects_ou_instead_of_falling_back():
    cfg = Config(monitored_accounts=MonitoredAccountsConfig(mode="discover-ou", ou_ids=(OU,)))
    with pytest.raises(ValueError, match="ENABLE_ORGANIZATIONS"):
        module("deployment-scope").scope_parameters(cfg, None, {}, False)


def test_config_rejects_partial_invalid_ou_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("MONITORED_OU_IDS", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"monitored_accounts": {"mode": "discover-ou", "ou_ids": [OU, "bad"]}}))
    with pytest.raises(ValueError, match="valid OU"):
        load_config(config)


def test_explicit_empty_environment_does_not_reuse_baked_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITORED_OU_IDS", "")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"monitored_accounts": {"ou_ids": [OU]}}))
    assert load_config(config).monitored_accounts.ou_ids == ()


def cfn_template():
    class Loader(yaml.SafeLoader):
        pass

    def tag(loader, name, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        return {name: value}

    Loader.add_multi_constructor("!", tag)
    return yaml.load((ROOT / "infra/cloudformation.yaml").read_text(), Loader=Loader)


@pytest.mark.parametrize("existing_network", [False, True])
def test_both_schema_paths_rerun_migrations_and_gate_consumers(existing_network):
    tpl = cfn_template()
    resource = "SchemaInitExistingNetwork" if existing_network else "SchemaInit"
    assert tpl["Resources"][resource]["Properties"]["SchemaVersion"] == {"Ref": "BackendImageUri"}
    for name in ("BackendLambda", "IngesterLambda"):
        fn = tpl["Resources"][name]
        assert "DependsOn" not in fn  # no unconditional dependency on a disabled resource
        condition, yes, no = fn["Properties"]["Environment"]["Variables"]["SCHEMA_READY"]["If"]
        assert condition == "UseExistingNetwork"
        assert (yes if existing_network else no) == {"Ref": resource}


def test_live_pull_uses_the_same_optional_role_as_ingestion():
    resources = cfn_template()["Resources"]
    assert resources["LivePullFunction"]["Properties"]["Role"] == resources["IngesterLambda"]["Properties"]["Role"]


def test_secret_and_queue_naming_are_independent_of_role_choices():
    tpl = cfn_template()
    assert tpl["Conditions"]["UseStableResourceNames"] == {
        "Equals": [{"Ref": "StableResourceNames"}, "true"]}
    for resource, name in (("DbSecret", "Name"), ("SchemaInitDlq", "QueueName")):
        assert tpl["Resources"][resource]["Properties"][name]["If"][0] == "UseStableResourceNames"


def test_scope_is_explicit_in_template_even_when_org_permissions_are_enabled():
    tpl = cfn_template()
    assert tpl["Parameters"]["MonitoredAccountsMode"]["Default"] == "single"
    assert tpl["Parameters"]["ReaderExternalId"]["NoEcho"] is True
    env = tpl["Resources"]["IngesterLambda"]["Properties"]["Environment"]["Variables"]
    for key, param in (("MONITORED_ACCOUNTS_MODE", "MonitoredAccountsMode"),
                       ("MONITORED_ACCOUNTS_IDS", "MonitoredAccountIds"),
                       ("MONITORED_OU_IDS", "MonitoredOuIds"),
                       ("BEDROCK_OPS_LENS_EXTERNAL_ID", "ReaderExternalId")):
        assert env[key] == {"Ref": param}
