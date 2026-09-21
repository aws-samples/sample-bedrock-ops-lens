"""Exercise explicit-account onboarding without AWS credentials or resources."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters


ROOT = Path(__file__).resolve().parents[1]
CENTRAL = "999999999999"
TARGET_A = "111111111111"
TARGET_B = "222222222222"
INGESTER_ROLE = f"arn:aws:iam::{CENTRAL}:role/lens-ingester"
ADMIN_ROLE = f"arn:aws:iam::{CENTRAL}:role/AWSCloudFormationStackSetAdministrationRole"


@pytest.fixture
def rollout():
    spec = importlib.util.spec_from_file_location(
        "lens_rollout_under_test", ROOT / "scripts/setup-multi-account.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def aws_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def args_for(**changes):
    values = dict(
        accounts=f"{TARGET_A},{TARGET_B}", accounts_file=None, region="us-east-1",
        role_name="BedrockOpsLensReader", external_id="", ingester_role_arn=INGESTER_ROLE,
        stack_set_name=None, failure_tolerance=0,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def instance(account, status="CURRENT", detailed_status="SUCCEEDED", region="us-east-1"):
    return {
        "Account": account, "Region": region, "Status": status,
        "StackInstanceStatus": {"DetailedStatus": detailed_status},
    }


def aws_clients(rollout, monkeypatch):
    iam, cfn = Mock(), Mock()
    iam.get_role.return_value = {"Role": {"Arn": ADMIN_ROLE}}
    cfn.describe_stack_set.side_effect = aws_error("StackSetNotFoundException", "DescribeStackSet")
    cfn.create_stack_instances.return_value = {"OperationId": "create-operation"}
    cfn.update_stack_set.return_value = {"OperationId": "update-operation"}
    cfn.describe_stack_set_operation.return_value = {
        "StackSetOperation": {"Status": "SUCCEEDED"}
    }
    cfn.list_stack_set_operation_results.return_value = {
        "Summaries": [{"Account": TARGET_A, "Region": "us-east-1", "Status": "SUCCEEDED"}]
    }
    monkeypatch.setattr(
        rollout.boto3, "client", lambda service, **kwargs: {"iam": iam, "cloudformation": cfn}[service]
    )
    return iam, cfn


def test_account_sources_resolve_to_the_same_ids(rollout, tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text(f"\ufeff# approved accounts\n\n{TARGET_B}\n {TARGET_A} \n{TARGET_A}\n")
    expected = [TARGET_A, TARGET_B]
    assert rollout.account_ids(f" {TARGET_B},{TARGET_A},{TARGET_A} ", None) == expected
    assert rollout.account_ids(None, str(path)) == expected


@pytest.mark.parametrize("value", [
    "", "111", f"{TARGET_A},broken", f"{TARGET_A},", "١١١١١١١١١١١١",
])
def test_invalid_account_ids_are_rejected_not_dropped(rollout, value):
    with pytest.raises(ValueError, match="12 digits"):
        rollout.account_ids(value, None)


def test_ambiguous_or_missing_account_source_is_rejected(rollout, tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text(TARGET_B)
    for accounts, file in [(None, None), (TARGET_A, str(path))]:
        with pytest.raises(ValueError, match="exactly one"):
            rollout.account_ids(accounts, file)


def test_account_validation_does_not_call_aws(rollout, monkeypatch, capsys):
    monkeypatch.setattr(rollout, "caller_identity", Mock(side_effect=AssertionError("AWS called")))
    monkeypatch.setattr(sys, "argv", [
        "setup-multi-account.py", "--scope", "accounts",
        "--accounts", f"{TARGET_B},{TARGET_A}", "--print-account-ids",
    ])
    assert rollout.main() == 0
    assert capsys.readouterr().out.strip() == f"{TARGET_A},{TARGET_B}"


def test_parameters_bind_runtime_trust_and_clear_a_removed_external_id(rollout):
    actual = {p["ParameterKey"]: p["ParameterValue"] for p in
              rollout.parameters(CENTRAL, "BedrockOpsLensReader", "", INGESTER_ROLE)}
    assert actual == {
        "CentralAccountId": CENTRAL, "RoleName": "BedrockOpsLensReader",
        "CentralIngesterRoleArn": INGESTER_ROLE, "ExternalId": "",
    }


def test_missing_administration_role_stops_before_cloudformation(rollout, monkeypatch):
    iam, cfn = aws_clients(rollout, monkeypatch)
    iam.get_role.side_effect = aws_error("NoSuchEntity", "GetRole")
    with pytest.raises(SystemExit, match="iam:PassRole"):
        rollout.scope_accounts(args_for(), CENTRAL)
    cfn.create_stack_set.assert_not_called()
    cfn.create_stack_instances.assert_not_called()


def test_succeeded_operation_with_a_failed_account_on_later_page_is_failure(rollout):
    cfn = Mock()
    cfn.describe_stack_set_operation.return_value = {"StackSetOperation": {"Status": "SUCCEEDED"}}
    cfn.list_stack_set_operation_results.side_effect = [
        {"Summaries": [{"Account": TARGET_A, "Status": "SUCCEEDED"}], "NextToken": "page2"},
        {"Summaries": [{
            "Account": TARGET_B, "Status": "FAILED",
            "StatusReason": "Missing AWSCloudFormationStackSetExecutionRole",
        }]},
    ]
    with pytest.raises(RuntimeError, match="1 account result"):
        rollout.wait_for_stackset_operation(cfn, "readers", None, "operation")
    assert cfn.list_stack_set_operation_results.call_args.kwargs["NextToken"] == "page2"


@pytest.mark.parametrize("status", ["FAILED", "STOPPED"])
def test_terminal_operation_failure_is_not_success(rollout, status):
    cfn = Mock()
    cfn.describe_stack_set_operation.return_value = {"StackSetOperation": {"Status": status}}
    cfn.list_stack_set_operation_results.return_value = {"Summaries": []}
    with pytest.raises(RuntimeError, match=status):
        rollout.wait_for_stackset_operation(cfn, "readers", None, "operation")


def test_operation_status_permission_failure_is_not_hidden(rollout):
    cfn = Mock()
    cfn.describe_stack_set_operation.return_value = {"StackSetOperation": {"Status": "SUCCEEDED"}}
    cfn.list_stack_set_operation_results.side_effect = aws_error(
        "AccessDenied", "ListStackSetOperationResults"
    )
    with pytest.raises(ClientError, match="AccessDenied"):
        rollout.wait_for_stackset_operation(cfn, "readers", None, "operation")


def test_new_stackset_creates_all_targets_and_sets_exact_principal(rollout, monkeypatch):
    _, cfn = aws_clients(rollout, monkeypatch)
    cfn.list_stack_instances.side_effect = [
        {"Summaries": []},
        {"Summaries": [instance(TARGET_A)], "NextToken": "page2"},
        {"Summaries": [instance(TARGET_B)]},
    ]
    rollout.scope_accounts(args_for(), CENTRAL)
    request = cfn.create_stack_set.call_args.kwargs
    assert request["AdministrationRoleARN"] == ADMIN_ROLE
    assert request["ExecutionRoleName"] == "AWSCloudFormationStackSetExecutionRole"
    assert {"ParameterKey": "CentralIngesterRoleArn", "ParameterValue": INGESTER_ROLE} in request["Parameters"]
    assert cfn.create_stack_instances.call_args.kwargs["Accounts"] == [TARGET_A, TARGET_B]
    assert cfn.create_stack_instances.call_args.kwargs["OperationPreferences"] == {
        "FailureToleranceCount": 0, "MaxConcurrentCount": 1,
    }
    model = get_session().get_service_model("cloudformation")
    validate_parameters(request, model.operation_model("CreateStackSet").input_shape)
    validate_parameters(
        cfn.create_stack_instances.call_args.kwargs,
        model.operation_model("CreateStackInstances").input_shape,
    )


def test_rerun_updates_existing_and_only_creates_new_accounts(rollout, monkeypatch):
    _, cfn = aws_clients(rollout, monkeypatch)
    cfn.describe_stack_set.side_effect = None
    cfn.describe_stack_set.return_value = {"StackSet": {"PermissionModel": "SELF_MANAGED"}}
    cfn.list_stack_instances.side_effect = [
        {"Summaries": [instance(TARGET_A)]},
        {"Summaries": [instance(TARGET_A), instance(TARGET_B)]},
    ]
    rollout.scope_accounts(args_for(), CENTRAL)
    cfn.update_stack_set.assert_called_once()
    cfn.create_stack_set.assert_not_called()
    assert cfn.create_stack_instances.call_args.kwargs["Accounts"] == [TARGET_B]


def test_rerun_with_all_accounts_present_does_not_recreate_or_delete(rollout, monkeypatch):
    _, cfn = aws_clients(rollout, monkeypatch)
    cfn.describe_stack_set.side_effect = None
    cfn.describe_stack_set.return_value = {"StackSet": {"PermissionModel": "SELF_MANAGED"}}
    cfn.list_stack_instances.return_value = {"Summaries": [instance(TARGET_A), instance(TARGET_B)]}
    rollout.scope_accounts(args_for(accounts=TARGET_A), CENTRAL)
    cfn.create_stack_instances.assert_not_called()
    cfn.delete_stack_instances.assert_not_called()


def test_wrong_stackset_permission_model_is_rejected(rollout, monkeypatch):
    _, cfn = aws_clients(rollout, monkeypatch)
    cfn.describe_stack_set.side_effect = None
    cfn.describe_stack_set.return_value = {"StackSet": {"PermissionModel": "SERVICE_MANAGED"}}
    with pytest.raises(ValueError, match="not SELF_MANAGED"):
        rollout.scope_accounts(args_for(), CENTRAL)
    cfn.update_stack_set.assert_not_called()


def test_busy_stackset_cannot_advance_setup(rollout, monkeypatch):
    _, cfn = aws_clients(rollout, monkeypatch)
    cfn.list_stack_instances.return_value = {"Summaries": []}
    cfn.create_stack_instances.side_effect = aws_error(
        "OperationInProgressException", "CreateStackInstances"
    )
    with pytest.raises(ClientError, match="OperationInProgressException"):
        rollout.scope_accounts(args_for(), CENTRAL)


@pytest.mark.parametrize("bad_instance", [
    None, instance(TARGET_B, "OUTDATED"), instance(TARGET_B, detailed_status="FAILED"),
    instance(TARGET_B, region="us-west-2"),
])
def test_missing_or_failed_requested_instance_blocks_completion(rollout, monkeypatch, bad_instance):
    _, cfn = aws_clients(rollout, monkeypatch)
    final = [instance(TARGET_A)] + ([bad_instance] if bad_instance else [])
    cfn.list_stack_instances.side_effect = [{"Summaries": []}, {"Summaries": final}]
    with pytest.raises(RuntimeError, match=TARGET_B):
        rollout.scope_accounts(args_for(), CENTRAL)


@pytest.fixture
def pipeline(tmp_path):
    """Run the real shell and JSON code; only AWS and the rollout API are faked."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "scripts").mkdir()
    shutil.copyfile(ROOT / "setup-pipeline.sh", root / "setup-pipeline.sh")
    shutil.copyfile(ROOT / "scripts/setup-multi-account.py", root / "scripts/setup-multi-account.py")
    (root / ".deploy-stack-name").write_text("example")
    (root / "config.yaml").write_text("deploy_region: us-east-1\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    config = {
        "Role": INGESTER_ROLE, "RevisionId": "fixture-revision",
        "Environment": {"Variables": {
            "DB_HOST": "example.invalid",
            "MONITORED_ACCOUNTS_MODE": "discover-org",
            "MONITORED_ACCOUNTS_IDS": "333333333333",
        }},
    }
    state = tmp_path / "function.json"
    state.write_text(json.dumps(config))
    aws = bin_dir / "aws"
    aws.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as stream:
    stream.write(json.dumps({"kind": "aws", "args": args}) + "\n")
def value(flag):
    return args[args.index(flag) + 1]
if args[:2] == ["sts", "get-caller-identity"]:
    print("999999999999")
elif args[:2] == ["cloudformation", "describe-stacks"]:
    if "`ReaderRoleName`" in value("--query"):
        print(json.dumps(os.environ.get("FAKE_READER_NAME")))
        raise SystemExit(int(os.environ.get("FAKE_READER_RC", "0")))
    else:
        print(os.environ.get("FAKE_ORG_DISCOVERY", "None"))
elif args[:2] == ["lambda", "get-function-configuration"]:
    print(Path(os.environ["FAKE_CONFIG"]).read_text())
elif args[:2] == ["lambda", "update-function-configuration"]:
    if os.environ.get("FAKE_UPDATE_RC"):
        print("PreconditionFailedException: revision changed", file=sys.stderr)
        raise SystemExit(int(os.environ["FAKE_UPDATE_RC"]))
    environment = json.loads(Path(value("--environment").removeprefix("file://")).read_text())
    Path(os.environ["FAKE_WRITTEN_ENV"]).write_text(json.dumps(environment))
    print(environment["Variables"]["MONITORED_ACCOUNTS_MODE"])
elif args[:2] == ["lambda", "wait"]:
    pass
elif args[:2] == ["lambda", "invoke"]:
    case = os.environ.get("FAKE_INGEST", "ok")
    metadata = {"StatusCode": 200}
    payload = {"status": "ok", "failed_count": 0, "runs": [{"module": "cw_metrics", "rc": 0}]}
    if case == "function-error":
        metadata["FunctionError"] = "Unhandled"
        payload = {"errorMessage": "fixture failure", "errorType": "RuntimeError"}
    elif case == "partial":
        payload = {"status": "partial", "failed_count": 1, "runs": [{"module": "cw_metrics", "rc": 1}]}
    elif case == "hidden-module-failure":
        payload["runs"][0]["rc"] = 1
    elif case in ("incomplete", "incomplete-and-failed"):
        payload = {
            "status": "incomplete", "failed_count": 0,
            "incomplete_modules": ["invocation_logs"],
            "runs": [{
                "module": "invocation_logs", "rc": 2,
                "incomplete_reason": "time_budget",
            }],
        }
        if case == "incomplete-and-failed":
            payload["status"] = "partial"
            payload["failed_count"] = 1
            payload["runs"].append({"module": "cw_metrics", "rc": 1})
    elif case in ("unmarked-rc2", "unmarked-rc2-incomplete"):
        payload["runs"][0]["rc"] = 2
        if case == "unmarked-rc2-incomplete":
            payload["status"] = "incomplete"
    elif case == "wrong-incomplete-code":
        payload["status"] = "incomplete"
        payload["runs"][0].update({"rc": 1, "incomplete_reason": "time_budget"})
    elif case == "empty":
        payload["runs"] = []
    Path(args[-1]).write_text("not JSON" if case == "malformed" else json.dumps(payload))
    print(json.dumps(metadata))
else:
    raise SystemExit("unexpected fake AWS request: " + repr(args))
''')
    aws.chmod(0o755)
    python = bin_dir / "python3"
    python.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
args = sys.argv[1:]
if args and args[0].endswith("setup-multi-account.py") and "--print-account-ids" not in args:
    with open(os.environ["FAKE_CALLS"], "a") as stream:
        stream.write(json.dumps({"kind": "rollout", "args": args}) + "\n")
    raise SystemExit(int(os.environ.get("FAKE_ROLLOUT_RC", "0")))
os.execv(sys.executable, [sys.executable, *args])
''')
    python.chmod(0o755)
    written = tmp_path / "environment.json"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1",
        "FAKE_CALLS": str(calls), "FAKE_CONFIG": str(state),
        "FAKE_WRITTEN_ENV": str(written), "AWS_EC2_METADATA_DISABLED": "true",
    }

    def run(*args, extra_env=None):
        completed = subprocess.run(
            ["/bin/bash", str(root / "setup-pipeline.sh"), *args],
            cwd=tmp_path, env={**env, **(extra_env or {})}, capture_output=True, text=True, timeout=20,
        )
        observed = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        updated = json.loads(written.read_text())["Variables"] if written.exists() else None
        return completed, observed, updated

    return SimpleNamespace(run=run, tmp=tmp_path, state=state)


def assert_configured(pipeline_result):
    completed, calls, env = pipeline_result
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert env["MONITORED_ACCOUNTS_MODE"] == "explicit"
    assert env["MONITORED_ACCOUNTS_IDS"] == f"{TARGET_A},{TARGET_B}"
    assert env["DB_HOST"] == "example.invalid"
    assert env["BEDROCK_OPS_LENS_ROLE_NAME"] == "BedrockOpsLensReader"
    rollout_calls = [c["args"] for c in calls if c["kind"] == "rollout"]
    assert len(rollout_calls) == 1
    args = rollout_calls[0]
    assert args[args.index("--accounts") + 1] == env["MONITORED_ACCOUNTS_IDS"]
    assert args[args.index("--ingester-role-arn") + 1] == INGESTER_ROLE
    assert args[args.index("--role-name") + 1] == env["BEDROCK_OPS_LENS_ROLE_NAME"]
    updates = [c["args"] for c in calls if c["args"][:2] == ["lambda", "update-function-configuration"]]
    assert updates[0][updates[0].index("--revision-id") + 1] == "fixture-revision"


def test_wrapper_csv_configures_both_sides_without_exported_shell_variables(pipeline):
    result = pipeline.run("--scope", "accounts", "--accounts", f"{TARGET_B},{TARGET_A}")
    assert_configured(result)
    assert "ingestion modules completed successfully" in result[0].stdout


def test_wrapper_distinguishes_configured_from_completed_ingestion(pipeline):
    completed, _, _ = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A,
        extra_env={"FAKE_INGEST": "incomplete"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PIPELINE CONFIGURED — ingestion incomplete" in completed.stdout
    assert "data coverage is incomplete for: invocation_logs" in completed.stdout
    assert "resume" in completed.stdout
    assert "ingestion modules completed successfully" not in completed.stdout


def test_wrapper_accounts_file_configures_the_same_targets(pipeline):
    (pipeline.tmp / "accounts.txt").write_text(f"# targets\n{TARGET_B}\n{TARGET_A}\n{TARGET_A}\n")
    assert_configured(pipeline.run("--scope", "accounts", "--accounts-file", "accounts.txt"))


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("role_args", [[], ["--role-name", "CompanyBedrockReader"]])
def test_wrapper_uses_deployed_custom_name_for_rollout_and_runtime(pipeline, manual, role_args):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, *role_args,
        *(["--skip-rollout"] if manual else []),
        extra_env={"FAKE_READER_NAME": "CompanyBedrockReader", "FAKE_ORG_DISCOVERY": "false"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert env["BEDROCK_OPS_LENS_ROLE_NAME"] == "CompanyBedrockReader"
    assert env["DB_HOST"] == "example.invalid"
    assert "CompanyBedrockReader" in completed.stdout
    rollout_calls = [call["args"] for call in calls if call["kind"] == "rollout"]
    if manual:
        assert rollout_calls == []
    else:
        assert len(rollout_calls) == 1
        args = rollout_calls[0]
        assert args[args.index("--role-name") + 1] == "CompanyBedrockReader"


@pytest.mark.parametrize("source", ["flag", "environment"])
@pytest.mark.parametrize("deployed", [None, "CompanyBedrockReader"])
def test_reader_name_mismatch_stops_before_rollout_or_lambda_change(pipeline, source, deployed):
    args = ["--role-name", "DifferentReader"] if source == "flag" else []
    env = {} if deployed is None else {"FAKE_READER_NAME": deployed}
    if source == "environment":
        env["BEDROCK_OPS_LENS_ROLE_NAME"] = "DifferentReader"
    completed, calls, updated = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, *args, extra_env=env,
    )
    assert completed.returncode != 0
    assert "does not match central ReaderRoleName" in completed.stderr
    assert "Redeploy the central stack" in completed.stderr
    assert updated is None
    assert all(call["kind"] == "aws" and call["args"][:2] in [
        ["sts", "get-caller-identity"], ["cloudformation", "describe-stacks"],
    ] for call in calls)


def test_reader_flag_takes_precedence_over_shell_environment(pipeline):
    completed, _, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, "--skip-rollout",
        "--role-name", "CompanyBedrockReader",
        extra_env={
            "FAKE_READER_NAME": "CompanyBedrockReader",
            "BEDROCK_OPS_LENS_ROLE_NAME": "OldReader",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert env["BEDROCK_OPS_LENS_ROLE_NAME"] == "CompanyBedrockReader"


def test_valid_name_none_is_not_mistaken_for_a_missing_parameter(pipeline):
    completed, _, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, "--skip-rollout",
        extra_env={"FAKE_READER_NAME": "None"},
    )
    assert completed.returncode == 0, completed.stderr
    assert env["BEDROCK_OPS_LENS_ROLE_NAME"] == "None"


def test_unreadable_reader_name_stops_before_rollout(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A,
        extra_env={"FAKE_READER_NAME": "AccessDenied", "FAKE_READER_RC": "1"},
    )
    assert completed.returncode != 0
    assert "cannot read ReaderRoleName" in completed.stderr
    assert env is None
    assert not any(call["kind"] == "rollout" for call in calls)


@pytest.mark.parametrize("role_name", ["*", "path/Reader", "R" * 65])
def test_invalid_reader_names_stop_before_aws(pipeline, rollout, monkeypatch, role_name):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, "--role-name", role_name,
    )
    assert completed.returncode != 0
    assert "valid IAM role name" in completed.stderr
    assert calls == []
    assert env is None
    caller = Mock(side_effect=AssertionError("invalid role must not reach AWS"))
    monkeypatch.setattr(rollout, "caller_identity", caller)
    monkeypatch.setattr(sys, "argv", [
        "setup-multi-account.py", "--scope", "accounts", "--accounts", TARGET_A,
        "--role-name", role_name,
    ])
    with pytest.raises(SystemExit) as error:
        rollout.main()
    assert error.value.code == 2
    caller.assert_not_called()


@pytest.mark.parametrize("source", ["csv", "file"])
def test_manual_roles_configure_ingestion_without_any_rollout(pipeline, source):
    if source == "file":
        (pipeline.tmp / "accounts.txt").write_text(f"# approved\n{TARGET_B}\n{TARGET_A}\n{TARGET_B}\n")
        args = ["--accounts-file", "accounts.txt"]
    else:
        args = ["--accounts", f"{TARGET_B},{TARGET_A},{TARGET_B}"]
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--skip-rollout", *args,
        extra_env={"FAKE_ROLLOUT_RC": "99", "FAKE_ORG_DISCOVERY": "false"},
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert env["MONITORED_ACCOUNTS_MODE"] == "explicit"
    assert env["MONITORED_ACCOUNTS_IDS"] == f"{TARGET_A},{TARGET_B}"
    assert env["DB_HOST"] == "example.invalid"
    assert env["BEDROCK_OPS_LENS_ROLE_NAME"] == "BedrockOpsLensReader"
    assert all(call["kind"] == "aws" for call in calls)
    allowed = [
        ["sts", "get-caller-identity"], ["cloudformation", "describe-stacks"],
        ["lambda", "get-function-configuration"],
        ["lambda", "update-function-configuration"],
        ["lambda", "wait"], ["lambda", "invoke"],
    ]
    assert all(call["args"][:2] in allowed for call in calls)
    update = next(call["args"] for call in calls
                  if call["args"][:2] == ["lambda", "update-function-configuration"])
    assert update[update.index("--revision-id") + 1] == "fixture-revision"
    assert any(call["args"][:2] == ["lambda", "invoke"] for call in calls)
    assert "using existing BedrockOpsLensReader roles" in completed.stdout
    assert "rolling out" not in completed.stdout


def test_manual_roles_can_include_the_central_account_without_a_self_reader(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--skip-rollout", "--accounts", f"{CENTRAL},{TARGET_A}",
    )
    assert completed.returncode == 0, completed.stderr
    assert env["MONITORED_ACCOUNTS_IDS"] == f"{TARGET_A},{CENTRAL}"
    assert all(call["kind"] == "aws" for call in calls)


@pytest.mark.parametrize("old,new,expected", [
    ("existing-condition", None, "existing-condition"),
    ("existing-condition", "new-condition", "new-condition"),
    ("existing-condition", "", ""),
])
def test_manual_roles_preserve_or_explicitly_change_only_the_ingester_external_id(
    pipeline, old, new, expected,
):
    config = json.loads(pipeline.state.read_text())
    config["Environment"]["Variables"]["BEDROCK_OPS_LENS_EXTERNAL_ID"] = old
    pipeline.state.write_text(json.dumps(config))
    override = {} if new is None else {"BEDROCK_OPS_LENS_EXTERNAL_ID": new}
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--skip-rollout", "--accounts", TARGET_A,
        "--skip-ingest", extra_env=override,
    )
    assert completed.returncode == 0, completed.stderr
    assert env.get("BEDROCK_OPS_LENS_EXTERNAL_ID", "") == expected
    assert all(call["kind"] == "aws" for call in calls)
    assert "ingestion has not been verified" in completed.stdout
    assert not any(call["args"][:2] == ["lambda", "invoke"] for call in calls)


def test_manual_preview_has_no_writes_and_does_not_claim_to_deploy_roles(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--skip-rollout", "--accounts", TARGET_A, "--dry-run",
    )
    assert completed.returncode == 0, completed.stderr
    assert env is None
    assert all(call["kind"] == "aws" for call in calls)
    assert all(call["args"][:2] in [
        ["sts", "get-caller-identity"], ["cloudformation", "describe-stacks"],
        ["lambda", "get-function-configuration"],
    ] for call in calls)
    assert "Use existing BedrockOpsLensReader roles" in completed.stdout
    assert "Roll out" not in completed.stdout


def test_manual_revision_conflict_stops_before_ingestion(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--skip-rollout", "--accounts", TARGET_A,
        extra_env={"FAKE_UPDATE_RC": "1"},
    )
    assert completed.returncode != 0
    assert env is None
    assert "revision changed" in completed.stderr
    assert not any(call["args"][:2] == ["lambda", "invoke"] for call in calls)
    assert "PIPELINE CONFIGURED" not in completed.stdout


@pytest.mark.parametrize("failure", ["function-error", "partial", "unmarked-rc2", "empty"])
def test_manual_mode_still_rejects_ingestion_failures(pipeline, failure):
    completed, calls, _ = pipeline.run(
        "--scope", "accounts", "--skip-rollout", "--accounts", TARGET_A,
        extra_env={"FAKE_INGEST": failure},
    )
    assert completed.returncode != 0
    assert all(call["kind"] == "aws" for call in calls)
    assert "PIPELINE CONFIGURED" not in completed.stdout


@pytest.mark.parametrize("args", [
    ["--scope", "single"],
    ["--scope", "ou", "--ou-id", "ou-example-12345678"],
    ["--scope", "org-root"],
    ["--scope", "accounts", "--accounts", f"{TARGET_A},invalid"],
    ["--scope", "accounts", "--accounts", TARGET_A, "--accounts-file", "accounts.txt"],
    ["--scope", "accounts", "--accounts-file", "missing.txt"],
])
def test_manual_mode_rejects_bad_scope_or_targets_before_aws(pipeline, args):
    completed, calls, env = pipeline.run("--skip-rollout", *args)
    assert completed.returncode != 0
    assert calls == []
    assert env is None


@pytest.mark.parametrize("scope,arguments", [
    ("ou", ["--ou-id", "ou-example-12345678"]), ("org-root", []),
])
def test_disabled_organizations_rejects_org_scopes_before_rollout(pipeline, scope, arguments):
    completed, calls, env = pipeline.run(
        "--scope", scope, *arguments, extra_env={"FAKE_ORG_DISCOVERY": "false"},
    )
    assert completed.returncode != 0
    assert "Organizations discovery is disabled" in completed.stderr
    assert env is None
    assert all(call["kind"] == "aws" for call in calls)
    assert all(call["args"][:2] in [
        ["sts", "get-caller-identity"], ["cloudformation", "describe-stacks"],
    ] for call in calls)


@pytest.mark.parametrize("extra_args", [
    ["--accounts", f"{TARGET_A},bad"],
    ["--accounts", TARGET_A, "--accounts-file", "accounts.txt"],
    ["--accounts-file", "missing.txt"],
    [],
])
def test_wrapper_bad_targets_fail_before_any_aws_calls(pipeline, extra_args):
    completed, calls, env = pipeline.run("--scope", "accounts", *extra_args)
    assert completed.returncode != 0
    assert calls == []
    assert env is None


def test_wrapper_rollout_failure_never_reconfigures_or_invokes_lambda(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, extra_env={"FAKE_ROLLOUT_RC": "1"}
    )
    assert completed.returncode != 0
    assert env is None
    assert not any(c["args"][:2] == ["lambda", "invoke"] for c in calls)


def test_unreadable_lambda_environment_stops_before_rollout(pipeline):
    config = json.loads(pipeline.state.read_text())
    config["Environment"] = {"Error": {"ErrorCode": "KMSAccessDeniedException"}}
    pipeline.state.write_text(json.dumps(config))
    completed, calls, env = pipeline.run("--scope", "accounts", "--accounts", TARGET_A)
    assert completed.returncode != 0
    assert env is None
    assert not any(c["kind"] == "rollout" for c in calls)


@pytest.mark.parametrize("args", [
    ["--scope"], ["--scope", "accounts", "--accounts"],
    ["--scope", "single", "--accounts", TARGET_A],
    ["--scope", "accounts", "--accounts", TARGET_A, "--delegated-admin"],
])
def test_missing_or_inapplicable_options_fail_without_aws(pipeline, args):
    completed, calls, env = pipeline.run(*args)
    assert completed.returncode != 0
    assert calls == []
    assert env is None


@pytest.mark.parametrize("failure", [
    "function-error", "partial", "hidden-module-failure", "empty", "malformed",
    "incomplete-and-failed", "unmarked-rc2", "unmarked-rc2-incomplete",
    "wrong-incomplete-code",
])
def test_wrapper_lambda_errors_cannot_print_success(pipeline, failure):
    completed, _, _ = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, extra_env={"FAKE_INGEST": failure}
    )
    assert completed.returncode != 0
    assert "PIPELINE CONFIGURED" not in completed.stdout


def test_wrapper_dry_run_reads_but_does_not_modify_or_invoke(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, "--dry-run", "--skip-ingest"
    )
    assert completed.returncode == 0, completed.stderr
    assert env is None
    assert all(c["kind"] == "aws" for c in calls)
    assert all(c["args"][:2] in [
        ["sts", "get-caller-identity"], ["cloudformation", "describe-stacks"],
        ["lambda", "get-function-configuration"],
    ] for c in calls)


def test_wrapper_skip_ingest_discloses_unverified_ingestion(pipeline):
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, "--skip-ingest"
    )
    assert completed.returncode == 0, completed.stderr
    assert env["MONITORED_ACCOUNTS_IDS"] == TARGET_A
    assert "ingestion has not been verified" in completed.stdout
    assert not any(c["args"][:2] == ["lambda", "invoke"] for c in calls)


@pytest.mark.parametrize("mode,args", [("single", []), ("ou", ["--ou-id", "ou-example-12345678"])])
def test_switching_from_explicit_removes_stale_ids(pipeline, mode, args):
    completed, _, env = pipeline.run("--scope", mode, *args, "--skip-ingest")
    assert completed.returncode == 0, completed.stderr
    assert env["MONITORED_ACCOUNTS_MODE"] == ("single" if mode == "single" else "discover-org")
    assert "MONITORED_ACCOUNTS_IDS" not in env


@pytest.mark.parametrize("old,new,expected", [
    ("existing-condition", None, "existing-condition"),
    ("existing-condition", "new-condition", "new-condition"),
    ("existing-condition", "", ""),
])
def test_external_id_is_consistent_on_reader_and_lambda(pipeline, old, new, expected):
    config = json.loads(pipeline.state.read_text())
    config["Environment"]["Variables"]["BEDROCK_OPS_LENS_EXTERNAL_ID"] = old
    pipeline.state.write_text(json.dumps(config))
    override = {} if new is None else {"BEDROCK_OPS_LENS_EXTERNAL_ID": new}
    completed, calls, env = pipeline.run(
        "--scope", "accounts", "--accounts", TARGET_A, "--skip-ingest", extra_env=override
    )
    assert completed.returncode == 0, completed.stderr
    rollout_args = next(c["args"] for c in calls if c["kind"] == "rollout")
    assert rollout_args[rollout_args.index("--external-id") + 1] == expected
    assert env.get("BEDROCK_OPS_LENS_EXTERNAL_ID", "") == expected
