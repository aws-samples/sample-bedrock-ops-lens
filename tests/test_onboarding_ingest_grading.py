"""Exercise real ingestion outcomes and the setup gate without AWS or a database."""
from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import boto3
import pytest

from ingestion import invocation_logs, lambda_handler
from ingestion.outcomes import IngestOutcome


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "111111111111"


@pytest.mark.parametrize("arguments", [
    ["-m", "ingestion.invocation_logs"], ["ingestion/invocation_logs.py"],
])
def test_invocation_log_cli_entry_points(arguments):
    completed = subprocess.run(
        [sys.executable, *arguments, "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--max-seconds" in completed.stdout


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("network forbidden"))
    )
    monkeypatch.setattr(
        boto3, "client", Mock(side_effect=AssertionError("AWS calls must be faked"))
    )
    monkeypatch.setattr(
        asyncpg, "connect", AsyncMock(side_effect=AssertionError("database must be faked"))
    )


def run_handler(monkeypatch, *modules):
    """Replace scheduling only; execute the real module runner and handler."""
    async def orchestrate(**kwargs):
        return {
            "runs": [
                await lambda_handler._run_module(name, main)
                for name, main in modules
            ]
        }

    monkeypatch.setattr(lambda_handler, "_orchestrate", orchestrate)
    return lambda_handler.handler({}, None)


def run_setup_gate(tmp_path, payload, metadata=None):
    """Execute the shipped shell heredoc against the Lambda response."""
    script = (ROOT / "setup-pipeline.sh").read_text()
    blocks = [part.split("\nPY\n", 1)[0] for part in script.split("<<'PY'\n")[1:]]
    gates = [block for block in blocks if "every reported module" in block]
    assert len(gates) == 1
    invocation = tmp_path / "invoke.json"
    result = tmp_path / "result.json"
    status = tmp_path / "status.txt"
    status.unlink(missing_ok=True)
    invocation.write_text(json.dumps(metadata if metadata is not None else {"StatusCode": 200}))
    result.write_text(json.dumps(payload))
    completed = subprocess.run(
        [sys.executable, "-", str(invocation), str(result), str(status)],
        input=gates[0], capture_output=True, text=True, timeout=10,
    )
    return completed, status.read_text().strip() if status.exists() else None


@pytest.mark.parametrize("name", [
    "cw_metrics", "cw_mantle_metrics", "cw_agentcore", "cw_guardrails", "quotas",
])
def test_empty_account_discovery_is_a_failure(monkeypatch, tmp_path, capsys, name):
    module = importlib.import_module(f"ingestion.{name}")
    monkeypatch.setattr(module, "discover_accounts", Mock(return_value=[]))
    monkeypatch.setattr(sys, "argv", [name])

    result = run_handler(monkeypatch, (name, module.main))

    assert "no monitored accounts resolved" in capsys.readouterr().err
    assert result["runs"][0]["rc"] == 2
    assert "incomplete_reason" not in result["runs"][0]
    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    assert result["incomplete_modules"] == []
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode != 0
    assert status is None


def test_invocation_log_argument_error_is_a_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["invocation_logs"])

    result = run_handler(monkeypatch, ("invocation_logs", invocation_logs.main))

    assert "the following arguments are required: --bucket" in capsys.readouterr().err
    assert result["runs"][0]["rc"] == 2
    assert "incomplete_reason" not in result["runs"][0]
    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode != 0
    assert status is None


@pytest.fixture
def log_io(monkeypatch):
    """Persist the object ledger across calls; fake only S3 and database I/O."""
    state = SimpleNamespace(
        now=0.0, expire_after_first=True, fail_ledger=False,
        keys=["first.json", "second.json"], ledger=set(), reads=[], tagged_rows=[],
    )
    entry = {
        "timestamp": "2026-09-01T12:00:00Z", "accountId": ACCOUNT,
        "region": "us-east-1", "operation": "InvokeModel", "modelId": "test-model",
        "input": {"inputTokenCount": 3}, "output": {"outputTokenCount": 5},
    }

    def get_object(*, Bucket, Key):
        state.reads.append(Key)
        if Key == state.keys[0] and state.expire_after_first:
            # Time expires while this object's two records are being read.
            # Both must finish before the next object is considered.
            state.now = 10.0
        records = [entry, entry] if Key == state.keys[0] else [entry]
        return {"Body": io.BytesIO(b"\n".join(json.dumps(r).encode() for r in records))}

    async def write_rows(sql, rows):
        if "INSERT INTO ingestion_log_objects" in sql:
            if state.fail_ledger:
                raise RuntimeError("object ledger write failed")
            state.ledger.update(row[0] for row in rows)
        elif "INSERT INTO f_daily_tagged" in sql:
            state.tagged_rows.extend(rows)

    async def read_ledger(sql, keys):
        assert "FROM ingestion_log_objects" in sql
        return [{"s3_key": key} for key in keys if key in state.ledger]

    state.connection = SimpleNamespace(
        execute=AsyncMock(), executemany=AsyncMock(side_effect=write_rows),
        fetch=AsyncMock(side_effect=read_ledger), fetchval=AsyncMock(return_value=0),
        close=AsyncMock(),
    )
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=state.connection))
    monkeypatch.setattr(
        invocation_logs, "_s3_client",
        Mock(return_value=SimpleNamespace(get_object=get_object)),
    )
    monkeypatch.setattr(invocation_logs, "_list_log_keys", Mock(return_value=state.keys))
    monkeypatch.setattr(invocation_logs, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(sys, "argv", [
        "invocation_logs", "--bucket", "fixture-logs", "--accounts", ACCOUNT,
        "--regions", "us-east-1", "--days", "0", "--max-seconds", "10",
    ])
    return state


def test_budget_stop_records_complete_objects_and_next_run_resumes(monkeypatch, tmp_path, log_io):
    first = run_handler(monkeypatch, ("invocation_logs", invocation_logs.main))

    assert log_io.reads == ["first.json"]
    assert log_io.ledger == {"first.json"}
    assert sum(row[7] for row in log_io.tagged_rows if row[5] == "__all__") == 2
    log_io.connection.close.assert_awaited_once()
    assert first["status"] == "incomplete"
    assert first["failed_count"] == 0
    assert first["incomplete_modules"] == ["invocation_logs"]
    assert first["runs"][0]["rc"] == 2
    assert first["runs"][0]["incomplete_reason"] == "time_budget"
    completed, status = run_setup_gate(tmp_path, first)
    assert completed.returncode == 0, completed.stderr
    assert status == "incomplete"
    assert "data coverage is incomplete" in completed.stdout
    assert "resume" in completed.stdout

    log_io.expire_after_first = False
    second = run_handler(monkeypatch, ("invocation_logs", invocation_logs.main))

    assert log_io.reads == ["first.json", "second.json"]
    assert log_io.ledger == {"first.json", "second.json"}
    assert sum(row[7] for row in log_io.tagged_rows if row[5] == "__all__") == 3
    assert second["status"] == "ok"
    assert second["failed_count"] == 0
    assert second["incomplete_modules"] == []
    assert second["runs"][0]["rc"] == 0
    completed, status = run_setup_gate(tmp_path, second)
    assert completed.returncode == 0, completed.stderr
    assert status == "ok"

    # A further pass must not re-read or re-add either object's usage.
    third = run_handler(monkeypatch, ("invocation_logs", invocation_logs.main))
    assert third["status"] == "ok"
    assert log_io.reads == ["first.json", "second.json"]
    assert sum(row[7] for row in log_io.tagged_rows if row[5] == "__all__") == 3
    assert log_io.connection.close.await_count == 3


@pytest.mark.parametrize("failure", ["ledger", "close"])
def test_write_or_close_failure_cannot_be_reported_as_resumable(
    monkeypatch, tmp_path, log_io, failure,
):
    if failure == "ledger":
        log_io.fail_ledger = True
    else:
        log_io.connection.close.side_effect = RuntimeError("connection close failed")

    result = run_handler(monkeypatch, ("invocation_logs", invocation_logs.main))

    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    assert result["incomplete_modules"] == []
    assert result["runs"][0]["rc"] == "error"
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode != 0
    assert status is None


def test_failure_takes_precedence_over_a_real_budget_stop(monkeypatch, tmp_path, log_io):
    async def failed_module():
        return 1

    result = run_handler(
        monkeypatch,
        ("invocation_logs", invocation_logs.main),
        ("quotas", failed_module),
    )

    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    assert result["incomplete_modules"] == ["invocation_logs"]
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode != 0
    assert status is None


@pytest.mark.parametrize("outcome", [0, None])
def test_normal_completion_remains_successful(monkeypatch, tmp_path, outcome):
    async def completed_module():
        return outcome

    result = run_handler(monkeypatch, ("cw_metrics", completed_module))

    assert result["status"] == "ok"
    assert result["failed_count"] == 0
    assert result["incomplete_modules"] == []
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode == 0, completed.stderr
    assert status == "ok"


def test_system_exit_with_the_typed_code_is_still_a_failure(monkeypatch, tmp_path):
    async def exited_module():
        raise SystemExit(IngestOutcome.TIME_BUDGET_EXHAUSTED)

    result = run_handler(monkeypatch, ("invocation_logs", exited_module))

    assert result["status"] == "partial"
    assert result["failed_count"] == 1
    assert "incomplete_reason" not in result["runs"][0]
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode != 0
    assert status is None


def test_no_modules_cannot_report_success(monkeypatch, tmp_path):
    result = run_handler(monkeypatch)

    assert result["status"] == "partial"
    assert result["runs"] == []
    completed, status = run_setup_gate(tmp_path, result)
    assert completed.returncode != 0
    assert status is None


@pytest.mark.parametrize("metadata", [
    {"StatusCode": 200, "FunctionError": "Unhandled"}, {"StatusCode": 202}, {},
])
def test_lambda_invocation_errors_override_a_resumable_payload(
    monkeypatch, tmp_path, log_io, metadata,
):
    result = run_handler(monkeypatch, ("invocation_logs", invocation_logs.main))

    completed, status = run_setup_gate(tmp_path, result, metadata=metadata)

    assert completed.returncode != 0
    assert status is None
