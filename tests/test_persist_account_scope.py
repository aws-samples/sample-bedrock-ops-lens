"""Durable scope updates and wrapper routing, with no AWS credentials or calls."""
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

from test_multi_account_setup import SCOPE_KEYS, pipeline  # noqa: F401 -- shared shell fixture


ROOT = Path(__file__).resolve().parents[1]
STACK = "BedrockOpsLens-example"
STACK_ID = f"arn:aws:cloudformation:us-east-1:999999999999:stack/{STACK}/fixture"
FUNCTION = f"{STACK}-ingester"
ROLE = "arn:aws:iam::999999999999:role/ingester"
SERVICE_ROLE = "arn:aws:iam::999999999999:role/cfn"
READER = "CompanyBedrockReader"
EXTERNAL_ID = "secret-condition-not-for-output"


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("persist_scope_test", ROOT / "scripts/persist-account-scope.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(mode="explicit", external_id=EXTERNAL_ID):
    variables = {
        "MONITORED_ACCOUNTS_MODE": mode,
        "BEDROCK_OPS_LENS_ROLE_NAME": READER,
        "BEDROCK_OPS_LENS_EXTERNAL_ID": external_id,
        "DB_HOST": "do-not-copy-live-overrides-to-cfn.invalid",
    }
    if mode == "explicit":
        variables["MONITORED_ACCOUNTS_IDS"] = "111111111111,222222222222"
    elif mode == "discover-ou":
        variables["MONITORED_OU_IDS"] = "ou-example-12345678,ou-example-87654321"
    return {"Variables": variables}


def stack(values=None):
    parameters = {
        "VpcId": "vpc-existing", "DatabasePassword": "****",
        "ReaderRoleName": READER, "EnableOrganizationsDiscovery": "true",
        "MonitoredAccountsMode": "discover-org", "MonitoredAccountIds": "",
        "MonitoredOuIds": "", "ReaderExternalId": "****",
    }
    parameters.update(values or {})
    return {
        "StackId": STACK_ID, "StackStatus": "UPDATE_COMPLETE",
        "CreationTime": "original", "LastUpdatedTime": "original-update",
        "RoleARN": SERVICE_ROLE, "Capabilities": ["CAPABILITY_NAMED_IAM"],
        "Parameters": [{"ParameterKey": key, "ParameterValue": value}
                       for key, value in parameters.items()],
    }


def stack_responses(*stacks):
    return [{"Stacks": [value]} for value in stacks]


def clients(helper, desired=None):
    desired = desired or environment()
    before = stack()
    after = stack(helper.desired_parameters(desired))
    # DescribeStacks masks secrets even after a successful update.
    for p in after["Parameters"]:
        if p["ParameterKey"] == "ReaderExternalId":
            p["ParameterValue"] = "****"
    after["LastUpdatedTime"] = "scope-update"
    current_function = {
        "Role": ROLE, "RevisionId": "initial", "LastUpdateStatus": "Successful",
        "Environment": environment("discover-org"),
    }
    final_function = {
        **current_function, "RevisionId": "updated", "Environment": deepcopy(desired),
    }
    cfn, lam = Mock(), Mock()
    cfn.describe_stacks.side_effect = stack_responses(before, deepcopy(before), after)
    lam.get_function_configuration.side_effect = [current_function, final_function]
    cfn.update_stack.return_value = {"StackId": STACK_ID}
    return SimpleNamespace(
        cfn=cfn, lam=lam, before=before, after=after,
        current_function=current_function, final_function=final_function, desired=desired,
    )


def persist(helper, state):
    return helper.persist_scope(
        state.cfn, state.lam, stack_name=STACK, function_name=FUNCTION,
        environment=state.desired, expected_revision="initial", expected_role=ROLE,
    )


def no_updates():
    return ClientError(
        {"Error": {"Code": "ValidationError", "Message": "No updates are to be performed."}},
        "UpdateStack",
    )


@pytest.mark.parametrize("mode", ["single", "explicit", "discover-org", "discover-ou"])
def test_persists_all_scope_parameters_reusing_template_service_role_and_every_other_parameter(helper, mode):
    state = clients(helper, environment(mode))
    assert persist(helper, state) is True
    request = state.cfn.update_stack.call_args.kwargs
    assert request["StackName"] == STACK_ID
    assert request["UsePreviousTemplate"] is True
    assert "TemplateBody" not in request and "TemplateURL" not in request
    assert request["RoleARN"] == SERVICE_ROLE
    assert request["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert request["ClientRequestToken"].startswith("lens-scope-")
    actual = {p["ParameterKey"]: p for p in request["Parameters"]}
    for key, value in helper.desired_parameters(state.desired).items():
        assert actual.pop(key) == {"ParameterKey": key, "ParameterValue": value}
    assert actual == {
        key: {"ParameterKey": key, "UsePreviousValue": True}
        for key in ("VpcId", "DatabasePassword", "ReaderRoleName", "EnableOrganizationsDiscovery")
    }
    model = get_session().get_service_model("cloudformation")
    validate_parameters(request, model.operation_model("UpdateStack").input_shape)
    state.cfn.get_waiter.assert_called_once_with("stack_update_complete")
    state.cfn.get_waiter.return_value.wait.assert_called_once_with(StackName=STACK_ID)
    state.lam.update_function_configuration.assert_not_called()
    assert state.lam.get_function_configuration.call_count == 2


def test_does_not_invent_a_service_role_for_stack_without_one(helper):
    state = clients(helper)
    state.before.pop("RoleARN")
    state.after.pop("RoleARN")
    state.cfn.describe_stacks.side_effect = stack_responses(state.before, state.before, state.after)
    persist(helper, state)
    assert "RoleARN" not in state.cfn.update_stack.call_args.kwargs


@pytest.mark.parametrize("external_id", ["", EXTERNAL_ID])
def test_external_id_is_explicitly_persisted_or_cleared_never_copied_from_noecho(helper, external_id):
    state = clients(helper, environment(external_id=external_id))
    persist(helper, state)
    param = next(p for p in state.cfn.update_stack.call_args.kwargs["Parameters"]
                 if p["ParameterKey"] == "ReaderExternalId")
    assert param == {"ParameterKey": "ReaderExternalId", "ParameterValue": external_id}


def test_noop_is_established_by_cfn_and_still_verifies_live_scope(helper):
    state = clients(helper)
    state.cfn.update_stack.side_effect = no_updates()
    assert persist(helper, state) is False
    state.cfn.get_waiter.assert_not_called()
    assert state.lam.get_function_configuration.call_count == 2


@pytest.mark.parametrize("noop", [False, True])
@pytest.mark.parametrize("changed", [
    "MONITORED_ACCOUNTS_MODE", "MONITORED_ACCOUNTS_IDS", "MONITORED_OU_IDS",
    "BEDROCK_OPS_LENS_EXTERNAL_ID", "BEDROCK_OPS_LENS_ROLE_NAME",
])
def test_success_or_noop_cannot_hide_live_scope_drift(helper, noop, changed):
    state = clients(helper)
    if noop:
        state.cfn.update_stack.side_effect = no_updates()
    state.final_function["Environment"]["Variables"][changed] = "unexpected"
    with pytest.raises(helper.ScopeError, match="Live ingester scope"):
        persist(helper, state)
    state.lam.update_function_configuration.assert_not_called()


def test_missing_empty_env_keys_are_equivalent_to_empty_parameters(helper):
    state = clients(helper, environment("single", ""))
    state.final_function["Environment"]["Variables"].pop("BEDROCK_OPS_LENS_EXTERNAL_ID")
    assert persist(helper, state)


@pytest.mark.parametrize("change", [
    {"RevisionId": "concurrent"},
    {"Role": "another-role"},
    {"Environment": {"Error": {"Message": EXTERNAL_ID}}},
    {"LastUpdateStatus": "InProgress"},
    {"LastUpdateStatus": "Failed"},
])
def test_lambda_concurrency_and_access_guards_prevent_stack_update(helper, change):
    state = clients(helper)
    state.current_function.update(change)
    with pytest.raises(helper.ScopeError):
        persist(helper, state)
    state.cfn.update_stack.assert_not_called()


@pytest.mark.parametrize("change", [
    {"LastUpdatedTime": "intervening-deploy"},
    {"RoleARN": "different-cfn-role"},
    {"StackStatus": "UPDATE_IN_PROGRESS"},
])
def test_stack_concurrency_guard_prevents_update(helper, change):
    state = clients(helper)
    changed = {**state.before, **change}
    state.cfn.describe_stacks.side_effect = stack_responses(state.before, changed)
    with pytest.raises(helper.ScopeError):
        persist(helper, state)
    state.cfn.update_stack.assert_not_called()


def test_parameter_order_changes_are_not_treated_as_a_concurrent_update(helper):
    state = clients(helper)
    reordered = deepcopy(state.before)
    reordered["Parameters"].reverse()
    state.cfn.describe_stacks.side_effect = stack_responses(state.before, reordered, state.after)
    assert persist(helper, state)


@pytest.mark.parametrize("status", ["UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_FAILED", "ROLLBACK_COMPLETE"])
def test_busy_or_failed_stacks_are_rejected(helper, status):
    state = clients(helper)
    state.before["StackStatus"] = status
    with pytest.raises(helper.ScopeError, match="busy or not updateable"):
        persist(helper, state)
    state.cfn.update_stack.assert_not_called()


@pytest.mark.parametrize("key,value,message", [
    ("ReaderRoleName", "ChangedReader", "ReaderRoleName changed"),
    ("EnableOrganizationsDiscovery", "false", "discovery is disabled"),
])
def test_deployed_role_and_discovery_permissions_are_enforced(helper, key, value, message):
    state = clients(helper, environment("discover-org"))
    state.cfn.describe_stacks.side_effect = stack_responses(stack({key: value}))
    with pytest.raises(helper.ScopeError, match=message):
        persist(helper, state)
    state.cfn.update_stack.assert_not_called()


def test_helper_rejects_missing_durable_parameter(helper):
    state = clients(helper)
    state.before["Parameters"] = [p for p in state.before["Parameters"] if p["ParameterKey"] != "ReaderExternalId"]
    with pytest.raises(helper.ScopeError, match="lacks durable scope"):
        persist(helper, state)
    state.cfn.update_stack.assert_not_called()


@pytest.mark.parametrize("mode,changes", [
    ("invalid", {}),
    ("explicit", {"MONITORED_ACCOUNTS_IDS": ""}),
    ("explicit", {"MONITORED_ACCOUNTS_IDS": "111111111111,invalid"}),
    ("single", {"MONITORED_ACCOUNTS_IDS": "111111111111"}),
    ("discover-ou", {"MONITORED_OU_IDS": "r-root"}),
    ("discover-ou", {"MONITORED_OU_IDS": ""}),
    ("single", {"MONITORED_OU_IDS": "ou-example-12345678"}),
    ("single", {"BEDROCK_OPS_LENS_ROLE_NAME": "path/reader"}),
    ("single", {"BEDROCK_OPS_LENS_EXTERNAL_ID": None}),
])
def test_invalid_inputs_never_reach_aws(helper, mode, changes):
    state = clients(helper)
    state.desired = environment(mode)
    state.desired["Variables"].update(changes)
    with pytest.raises(helper.ScopeError):
        persist(helper, state)
    state.cfn.describe_stacks.assert_not_called()


def test_waiter_rollback_is_a_failure_and_never_reads_live_scope_as_success(helper):
    state = clients(helper)
    state.cfn.get_waiter.return_value.wait.side_effect = WaiterError(
        name="stack_update_complete", reason=EXTERNAL_ID,
        last_response={"Stacks": [{"StackStatus": "UPDATE_ROLLBACK_COMPLETE"}]},
    )
    with pytest.raises(helper.ScopeError, match="did not complete successfully") as error:
        persist(helper, state)
    assert EXTERNAL_ID not in str(error.value)
    assert state.lam.get_function_configuration.call_count == 1


@pytest.mark.parametrize("key,value", [
    ("MonitoredAccountsMode", "discover-org"),
    ("MonitoredAccountIds", "333333333333"),
    ("ReaderRoleName", "ChangedReader"),
])
def test_final_stack_values_must_match_even_if_live_lambda_matches(helper, key, value):
    state = clients(helper)
    for p in state.after["Parameters"]:
        if p["ParameterKey"] == key:
            p["ParameterValue"] = value
    with pytest.raises(helper.ScopeError, match="Stack scope changed"):
        persist(helper, state)


def test_final_stack_must_still_contain_noecho_parameter(helper):
    state = clients(helper)
    state.after["Parameters"] = [p for p in state.after["Parameters"] if p["ParameterKey"] != "ReaderExternalId"]
    with pytest.raises(helper.ScopeError, match="Stack scope changed"):
        persist(helper, state)


def test_missing_runtime_reader_name_is_not_verified_as_default(helper):
    state = clients(helper)
    state.final_function["Environment"]["Variables"].pop("BEDROCK_OPS_LENS_ROLE_NAME")
    with pytest.raises(helper.ScopeError, match="Live ingester scope"):
        persist(helper, state)


def cli_args(tmp_path, desired):
    path = tmp_path / "private-env.json"
    path.write_text(json.dumps(desired))
    return [
        "--stack-name", STACK, "--function-name", FUNCTION, "--region", "us-east-1",
        "--environment-file", str(path), "--expected-revision", "initial",
        "--expected-role-arn", ROLE,
    ]


@pytest.mark.parametrize("failure_at", ["client", "read", "update", "wait", "verify"])
def test_cli_never_prints_external_id_from_sdk_errors(helper, monkeypatch, tmp_path, capsys, failure_at):
    state = clients(helper)
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": EXTERNAL_ID}}, "FakeAPI")
    client_factory = Mock(side_effect=lambda service, **kwargs: {"cloudformation": state.cfn, "lambda": state.lam}[service])
    monkeypatch.setattr(helper.boto3, "client", client_factory)
    if failure_at == "client":
        client_factory.side_effect = error
    elif failure_at == "read":
        state.cfn.describe_stacks.side_effect = error
    elif failure_at == "update":
        state.cfn.update_stack.side_effect = error
    elif failure_at == "wait":
        state.cfn.get_waiter.return_value.wait.side_effect = error
    else:
        state.lam.get_function_configuration.side_effect = [state.current_function, error]
    assert helper.main(cli_args(tmp_path, state.desired)) == 1
    output = capsys.readouterr()
    assert "ERROR:" in output.err
    assert EXTERNAL_ID not in output.out + output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("noop", [False, True])
def test_cli_success_is_sanitized_and_uses_requested_region(helper, monkeypatch, tmp_path, capsys, noop):
    state = clients(helper)
    if noop:
        state.cfn.update_stack.side_effect = no_updates()
    factory = Mock(side_effect=[state.cfn, state.lam])
    monkeypatch.setattr(helper.boto3, "client", factory)
    assert helper.main(cli_args(tmp_path, state.desired)) == 0
    output = capsys.readouterr()
    assert "live ingester scope verified" in output.out
    assert EXTERNAL_ID not in output.out + output.err
    assert all(call.kwargs == {"region_name": "us-east-1"} for call in factory.call_args_list)


def test_modern_wrapper_calls_persistence_without_direct_lambda_mutation(pipeline):
    completed, calls, variables = pipeline.run(
        "--scope", "accounts", "--accounts", "111111111111", "--skip-rollout",
        extra_env={"FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS), "BEDROCK_OPS_LENS_EXTERNAL_ID": EXTERNAL_ID},
    )
    assert completed.returncode == 0, completed.stderr
    assert variables["BEDROCK_OPS_LENS_EXTERNAL_ID"] == EXTERNAL_ID
    assert variables["MONITORED_ACCOUNTS_IDS"] == "111111111111"
    persistence = next(c["args"] for c in calls if c["kind"] == "persist")
    assert persistence[persistence.index("--expected-revision") + 1] == "fixture-revision"
    assert persistence[persistence.index("--stack-name") + 1] == STACK
    assert EXTERNAL_ID not in repr(persistence)
    assert EXTERNAL_ID not in completed.stdout + completed.stderr
    assert not any(c["args"][:2] == ["lambda", "update-function-configuration"] for c in calls)
    assert any(c["args"][:2] == ["lambda", "invoke"] for c in calls)


def test_modern_wrapper_failure_never_falls_back_to_direct_update_or_ingestion(pipeline):
    completed, calls, variables = pipeline.run(
        "--scope", "accounts", "--accounts", "111111111111", "--skip-rollout",
        extra_env={"FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS), "FAKE_PERSIST_RC": "1"},
    )
    assert completed.returncode != 0
    assert variables is None
    assert not any(c["args"][:2] in [
        ["lambda", "invoke"], ["lambda", "update-function-configuration"],
    ] for c in calls)
    assert "PIPELINE CONFIGURED" not in completed.stdout


def test_partial_template_fails_before_rollout(pipeline):
    completed, calls, variables = pipeline.run(
        "--scope", "accounts", "--accounts", "111111111111",
        extra_env={"FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS[:-1])},
    )
    assert completed.returncode != 0
    assert "incomplete scope parameters" in completed.stderr
    assert variables is None
    assert all(c["kind"] == "aws" for c in calls)
    assert all(c["args"][0] in ("cloudformation", "sts") for c in calls)


@pytest.mark.parametrize("scope,args,mode", [
    ("single", [], "single"),
    ("org-root", [], "discover-org"),
    ("accounts", ["--accounts", "111111111111", "--skip-rollout"], "explicit"),
])
def test_legacy_wrapper_warns_and_retains_revision_guard(pipeline, scope, args, mode):
    completed, calls, variables = pipeline.run(
        "--scope", scope, *args,
    )
    assert completed.returncode == 0, completed.stderr
    assert variables["MONITORED_ACCOUNTS_MODE"] == mode
    assert "Apply the new deploy template" in completed.stderr
    assert "redeploy can reset" in completed.stderr
    update = next(c["args"] for c in calls if c["args"][:2] == ["lambda", "update-function-configuration"])
    assert update[update.index("--revision-id") + 1] == "fixture-revision"
    assert not any(c["kind"] == "persist" for c in calls)
    assert any(c["args"][:2] == ["lambda", "invoke"] for c in calls)


@pytest.mark.parametrize("parameter_keys", [[], ["ReaderRoleName", "EnableOrganizationsDiscovery"]])
@pytest.mark.parametrize("args", [[], ["--skip-ingest"], ["--dry-run"], ["--delegated-admin"]])
def test_legacy_ou_setup_requires_upgrade_before_any_side_effect(pipeline, parameter_keys, args):
    original_config = pipeline.state.read_text()
    completed, calls, variables = pipeline.run(
        "--scope", "ou", "--ou-id", "ou-example-12345678", *args,
        extra_env={"FAKE_PARAMETER_KEYS": json.dumps(parameter_keys)},
    )
    assert completed.returncode != 0
    assert "--scope ou requires an upgraded central deployment" in completed.stderr
    assert "./deploy.sh" in completed.stderr
    assert "central template and ingester image" in completed.stderr
    assert variables is None
    assert pipeline.state.read_text() == original_config
    assert calls
    assert all(c["kind"] == "aws" and c["args"][:2] in [
        ["sts", "get-caller-identity"], ["cloudformation", "describe-stacks"],
    ] for c in calls)
    assert "PIPELINE CONFIGURED" not in completed.stdout
    assert "legacy direct Lambda update" not in completed.stderr


def test_modern_dry_run_only_previews_durable_change(pipeline):
    completed, calls, variables = pipeline.run(
        "--scope", "accounts", "--accounts", "111111111111", "--skip-rollout", "--dry-run",
        extra_env={"FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS), "BEDROCK_OPS_LENS_EXTERNAL_ID": EXTERNAL_ID},
    )
    assert completed.returncode == 0, completed.stderr
    assert variables is None
    assert all(c["kind"] == "aws" for c in calls)
    assert "Persist CloudFormation scope" in completed.stdout
    assert "legacy" not in completed.stderr
    assert EXTERNAL_ID not in completed.stdout + completed.stderr


EMPTY_OU_RESPONSE = {
    "runs": [], "skipped_reason": "empty_ou_scope", "status": "ok", "failed_count": 0,
}


def test_empty_ou_scope_is_a_successful_noop_only_for_ou_setup(pipeline):
    completed, _, variables = pipeline.run(
        "--scope", "ou", "--ou-id", "ou-example-12345678",
        extra_env={
            "FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS),
            "FAKE_INGEST_PAYLOAD": json.dumps(EMPTY_OU_RESPONSE),
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert variables["MONITORED_ACCOUNTS_MODE"] == "discover-ou"
    assert "PIPELINE CONFIGURED — OU scope is empty; ingestion skipped" in completed.stdout
    assert "no ingestion modules ran" in completed.stdout
    assert "ingestion modules completed successfully" not in completed.stdout


@pytest.mark.parametrize("scope,args", [
    ("single", []), ("org-root", []),
    ("accounts", ["--accounts", "111111111111", "--skip-rollout"]),
])
def test_empty_ou_marker_cannot_make_other_requested_scopes_succeed(pipeline, scope, args):
    completed, _, _ = pipeline.run(
        "--scope", scope, *args,
        extra_env={"FAKE_INGEST_PAYLOAD": json.dumps(EMPTY_OU_RESPONSE)},
    )
    assert completed.returncode != 0
    assert "PIPELINE CONFIGURED" not in completed.stdout


@pytest.mark.parametrize("overrides", [
    {"skipped_reason": None},
    {"skipped_reason": "unknown"},
    {"runs": None},
    {"runs": {}},
    {"status": "partial"},
    {"status": "incomplete"},
    {"failed_count": 1},
    {"runs": [{"module": "cw_metrics", "rc": 1}]},
])
def test_empty_ou_scope_does_not_allow_malformed_or_failed_responses(pipeline, overrides):
    completed, calls, _ = pipeline.run(
        "--scope", "ou", "--ou-id", "ou-example-12345678",
        extra_env={
            "FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS),
            "FAKE_INGEST_PAYLOAD": json.dumps({**EMPTY_OU_RESPONSE, **overrides}),
        },
    )
    assert completed.returncode != 0
    assert any(c["args"][:2] == ["lambda", "invoke"] for c in calls)
    assert "PIPELINE CONFIGURED" not in completed.stdout


@pytest.mark.parametrize("failure", ["missing-runs", "function-error"])
def test_empty_ou_scope_requires_run_list_and_successful_invocation(pipeline, failure):
    payload = dict(EMPTY_OU_RESPONSE)
    if failure == "missing-runs":
        payload.pop("runs")
    completed, calls, _ = pipeline.run(
        "--scope", "ou", "--ou-id", "ou-example-12345678",
        extra_env={
            "FAKE_PARAMETER_KEYS": json.dumps(SCOPE_KEYS),
            "FAKE_INGEST": failure, "FAKE_INGEST_PAYLOAD": json.dumps(payload),
        },
    )
    assert completed.returncode != 0
    assert any(c["args"][:2] == ["lambda", "invoke"] for c in calls)
    assert "PIPELINE CONFIGURED" not in completed.stdout
