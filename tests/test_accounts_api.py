"""Current configured membership joins account history without backend org access."""
from __future__ import annotations

from datetime import date
import io
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from app.routers import live_pull, utility  # noqa: E402
from ingestion import accounts, config  # noqa: E402

REAL_INVOKE = live_pull._invoke
ACTIVE, HISTORY, IDLE, STALE = (
    "111111111111", "222222222222", "333333333333", "444444444444",
)
OU = "ou-aaaa-11111111"


@pytest.fixture
def api(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(boto3, "client", forbidden)
    monkeypatch.setenv("LIVE_PULL_FUNCTION_NAME", "lens-live-pull")
    state = SimpleNamespace(
        traffic=[
            {"accountid": ACTIVE, "total_requests": 10, "model_count": 1,
             "last_seen": date(2026, 10, 25)},
            {"accountid": HISTORY, "total_requests": 20, "model_count": 2,
             "last_seen": date(2026, 10, 24)},
        ],
        names=[
            {"accountid": ACTIVE, "account_name": "Configured friendly name"},
            {"accountid": HISTORY, "account_name": "Historical account"},
            {"accountid": STALE, "account_name": "Old idle account"},
        ],
    )

    async def fetch(sql, *args):
        if "FROM f_daily" in sql:
            return state.traffic
        if "FROM dim_account" in sql:
            return state.names
        raise AssertionError(sql)

    monkeypatch.setattr(utility.db, "fetch", AsyncMock(side_effect=fetch))
    state.invoke = Mock(return_value={"ok": True, "accounts": [
        {"accountId": ACTIVE, "name": "Current active name"},
        {"accountId": IDLE, "name": "Current idle name"},
    ]})
    monkeypatch.setattr(live_pull, "_invoke", state.invoke)
    # A deployed backend must not rediscover scope from its stale image or role.
    state.config = Mock(side_effect=AssertionError("backend config is not scope authority"))
    monkeypatch.setattr(config, "load_config", state.config)
    state.discovery = Mock(side_effect=AssertionError("backend cannot discover Organizations"))
    monkeypatch.setattr(accounts, "discover_from_ous", state.discovery)
    monkeypatch.setattr(accounts, "discover_from_org", state.discovery)
    return state


async def test_idle_members_join_history_and_names_do_not_create_membership(api):
    result = await utility.list_accounts()
    assert [a["accountId"] for a in result] == [HISTORY, ACTIVE, IDLE]
    by_id = {a["accountId"]: a for a in result}
    assert by_id[IDLE] == {
        "accountId": IDLE, "name": "Current idle name", "total_requests": 0,
        "model_count": 0, "last_seen": None, "in_scope": True,
    }
    assert by_id[ACTIVE]["name"] == "Configured friendly name"
    assert by_id[ACTIVE]["total_requests"] == 10
    assert by_id[ACTIVE]["last_seen"] == "2026-10-25"
    assert by_id[HISTORY]["name"] == "Historical account"
    api.invoke.assert_called_once_with("lens-live-pull", {"action": "list-accounts"})
    api.config.assert_not_called()
    api.discovery.assert_not_called()


async def test_completely_idle_ou_is_visible_without_any_traffic_or_name_rows(api):
    api.traffic = []
    api.names = []
    out = await utility.list_accounts()
    assert [a["accountId"] for a in out] == [ACTIVE, IDLE]
    assert all(a["total_requests"] == 0 for a in out)
    assert out[1]["name"] == "Current idle name"


async def test_changed_membership_is_read_again_and_does_not_reuse_idle_members(api):
    first = await utility.list_accounts()
    assert IDLE in {a["accountId"] for a in first}
    api.invoke.return_value = {"ok": True, "accounts": []}
    second = await utility.list_accounts()
    assert {a["accountId"] for a in second} == {ACTIVE, HISTORY}
    assert api.invoke.call_count == 2
    api.discovery.assert_not_called()


@pytest.mark.parametrize("response", [
    {"ok": False, "error": "failed", "accounts": [{"accountId": STALE}]},
    {"ok": False, "error": "invalid_request"},  # an older live-pull function
    {"ok": True},
    {"ok": True, "accounts": "not a list"},
])
async def test_failed_discovery_keeps_history_without_stale_scope_fallback(api, response):
    api.invoke.return_value = response
    assert {a["accountId"] for a in await utility.list_accounts()} == {ACTIVE, HISTORY}
    api.config.assert_not_called()
    api.discovery.assert_not_called()


async def test_unreachable_function_preserves_history_and_does_not_leak_details(api, caplog):
    api.invoke.side_effect = RuntimeError("private endpoint and credentials")
    assert {a["accountId"] for a in await utility.list_accounts()} == {ACTIVE, HISTORY}
    assert "Configured account discovery failed" in caplog.text
    assert "credentials" not in caplog.text
    api.config.assert_not_called()
    api.discovery.assert_not_called()


async def test_account_action_uses_existing_lambda_transport(api, monkeypatch):
    # Exercise the real RequestResponse helper; the backend can only open Lambda.
    monkeypatch.setattr(live_pull, "_invoke", REAL_INVOKE)
    api.traffic = []
    api.names = []
    client = Mock()
    client.invoke.return_value = {
        "Payload": io.BytesIO(json.dumps({"ok": True, "accounts": [
            {"accountId": IDLE, "name": "Idle from Lambda"},
        ]}).encode()),
    }

    def aws_client(service, **kwargs):
        assert service == "lambda"
        return client

    monkeypatch.setattr(boto3, "client", aws_client)
    out = await utility.list_accounts()
    assert out[0]["accountId"] == IDLE and out[0]["name"] == "Idle from Lambda"
    kwargs = client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "lens-live-pull"
    assert json.loads(kwargs["Payload"]) == {"action": "list-accounts"}


@pytest.mark.parametrize("mode", ["discover-ou", "discover-org", "explicit", "single"])
async def test_local_development_uses_local_config_and_credentials(api, monkeypatch, mode):
    monkeypatch.delenv("LIVE_PULL_FUNCTION_NAME", raising=False)
    api.config.side_effect = None
    api.config.return_value = config.Config(monitored_accounts=config.MonitoredAccountsConfig(
        mode=mode, ou_ids=(OU,), ids=(IDLE,),
    ))
    api.discovery.side_effect = None
    api.discovery.return_value = [accounts.MonitoredAccount(IDLE, "Local member")]
    own = Mock(return_value=SimpleNamespace(self_account=IDLE))
    monkeypatch.setattr(accounts, "session_cache", own)
    out = await utility.list_accounts()
    assert {a["accountId"] for a in out} == {ACTIVE, HISTORY, IDLE}
    api.invoke.assert_not_called()
    if mode == "discover-ou":
        api.discovery.assert_called_once_with((OU,))
    elif mode == "discover-org":
        api.discovery.assert_called_once_with()
    else:
        api.discovery.assert_not_called()
    assert own.call_count == (1 if mode == "single" else 0)
