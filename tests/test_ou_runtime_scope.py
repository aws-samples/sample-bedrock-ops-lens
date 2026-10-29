"""OU runtime boundaries; all AWS, database, and socket I/O is faked."""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import io
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import boto3
from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters
import pytest
import yaml

from ingestion import accounts, config, cost, lambda_handler, live_pull, proxy_events


CENTRAL, MEMBER, OTHER = "999999999999", "111111111111", "222222222222"
OU, CHILD, OTHER_OU = "ou-aaaa-11111111", "ou-aaaa-22222222", "ou-bbbb-33333333"
START, END = date(2026, 10, 1), date(2026, 10, 2)
SERVICES = ["Amazon Bedrock"]
REAL_LOAD_CONFIG = config.load_config
COLLECTORS = [
    "model_lifecycle", "cw_metrics", "cw_mantle_metrics", "cost", "cw_agentcore",
    "cw_guardrails", "invocation_logs", "proxy_events",
    "inference_profiles", "cw_minute_peak", "quotas",
]


def denied():
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                       "ListAccountsForParent")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected external I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    clients = Mock(side_effect=forbidden)
    connection = AsyncMock(side_effect=forbidden)
    monkeypatch.setattr(boto3, "client", clients)
    monkeypatch.setattr(asyncpg, "connect", connection)
    monkeypatch.setattr(accounts, "session_cache", Mock(side_effect=forbidden))
    monkeypatch.setattr(cost, "session_cache", Mock(side_effect=forbidden))
    monkeypatch.setattr(live_pull, "new_session_cache", Mock(side_effect=forbidden))
    monkeypatch.setattr(sys, "argv", ["cost"])
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture.invalid/lens")
    monkeypatch.setenv("BEDROCK_LOGS_BUCKET", "fixture-logs")
    monkeypatch.setenv("PROXY_EVENTS_BUCKET", "fixture-proxy")
    monkeypatch.setenv("INGESTER_FUNCTION_NAME", "fixture-ingester")
    cfg = SimpleNamespace(
        monitored_accounts=config.MonitoredAccountsConfig(mode="discover-ou", ou_ids=(OU,)),
        account_names={}, resolved_regions=lambda: ["us-east-1"],
    )
    for module in (config, cost, live_pull):
        monkeypatch.setattr(module, "load_config", lambda: cfg)
    return SimpleNamespace(config=cfg, clients=clients, connection=connection)


@pytest.mark.parametrize("helper", [
    cost._list_bedrock_services, cost._fetch_cost, cost._fetch_cost_by_usage_type,
])
def test_cost_helpers_explicit_empty_scope_makes_no_request(helper):
    ce = Mock()
    args = (ce, START, END) if helper is cost._list_bedrock_services else (
        ce, START, END, SERVICES,
    )
    assert helper(*args, only_accounts=[]) == []
    assert ce.mock_calls == []


@pytest.mark.parametrize("helper", [cost._fetch_cost, cost._fetch_cost_by_usage_type])
@pytest.mark.parametrize("selected", [None, [MEMBER, OTHER]])
def test_cost_filter_survives_every_page(helper, selected):
    ce = Mock()
    ce.get_cost_and_usage.side_effect = [
        {"ResultsByTime": [], "NextPageToken": "next"}, {"ResultsByTime": []},
    ]
    assert helper(ce, START, END, SERVICES, only_accounts=selected) == []
    calls = ce.get_cost_and_usage.call_args_list
    assert len(calls) == 2 and calls[1].kwargs["NextPageToken"] == "next"
    for call in calls:
        flt = call.kwargs["Filter"]
        if selected is None:
            assert flt["Dimensions"]["Key"] == "SERVICE"
        else:
            assert flt["And"][1]["Dimensions"] == {
                "Key": "LINKED_ACCOUNT", "Values": selected, "MatchOptions": ["EQUALS"],
            }


@pytest.mark.parametrize("membership", [[], "denied"])
def test_cost_resolves_scope_before_clients_or_database(monkeypatch, isolated, membership):
    discovery = Mock(return_value=membership, side_effect=denied() if membership == "denied" else None)
    ce = Mock(side_effect=AssertionError("must not open CE"))
    monkeypatch.setattr(accounts, "discover_from_ous", discovery)
    monkeypatch.setattr(cost, "_ce_client", ce)
    if membership == "denied":
        with pytest.raises(ClientError):
            asyncio.run(cost.main())
    else:
        assert asyncio.run(cost.main()) == 0
    ce.assert_not_called()
    isolated.connection.assert_not_called()
    discovery.assert_called_once_with((OU,))


@pytest.mark.parametrize("csv", ["", " , ", "not-an-account"])
def test_empty_explicit_cost_cli_scope_never_becomes_unfiltered(monkeypatch, isolated, csv):
    monkeypatch.setattr(sys, "argv", ["cost", "--accounts", csv])
    ce = Mock(side_effect=AssertionError("must not open CE"))
    monkeypatch.setattr(cost, "_ce_client", ce)
    assert asyncio.run(cost.main()) == 0
    ce.assert_not_called()
    isolated.connection.assert_not_called()


def test_empty_explicit_cost_config_never_becomes_unfiltered(monkeypatch, isolated):
    isolated.config.monitored_accounts = config.MonitoredAccountsConfig(mode="explicit")
    ce = Mock(side_effect=AssertionError("must not open CE"))
    monkeypatch.setattr(cost, "_ce_client", ce)
    assert asyncio.run(cost.main()) == 0
    ce.assert_not_called()
    isolated.connection.assert_not_called()


def test_invalid_cost_config_fails_before_ce(monkeypatch, isolated):
    monkeypatch.setattr(cost, "load_config", Mock(side_effect=ValueError("invalid config")))
    ce = Mock(side_effect=AssertionError("must not open CE"))
    monkeypatch.setattr(cost, "_ce_client", ce)
    with pytest.raises(ValueError, match="invalid config"):
        asyncio.run(cost.main())
    ce.assert_not_called()
    isolated.connection.assert_not_called()


def test_cost_persists_only_selected_accounts_and_usage_totals(monkeypatch, isolated):
    """Exercise both real CE parsers and durable upserts with a recording DB."""
    selected = [accounts.MonitoredAccount(MEMBER), accounts.MonitoredAccount(OTHER)]
    monkeypatch.setattr(accounts, "discover_from_ous", Mock(return_value=selected))
    billed = {CENTRAL: 1000, MEMBER: 2, OTHER: 3}
    requests = []
    ce_model = get_session().get_service_model("ce")

    def dimensions(**kw):
        validate_parameters(kw, ce_model.operation_model("GetDimensionValues").input_shape)
        requests.append(kw)
        assert kw["Filter"]["Dimensions"]["Values"] == [MEMBER, OTHER]
        if "NextPageToken" not in kw:
            return {"DimensionValues": [], "NextPageToken": "services-page-2"}
        return {"DimensionValues": [{"Value": SERVICES[0]}]}

    def fetch(**kw):
        validate_parameters(kw, ce_model.operation_model("GetCostAndUsage").input_shape)
        requests.append(kw)
        ids = kw["Filter"]["And"][1]["Dimensions"]["Values"]
        assert ids == [MEMBER, OTHER]
        if kw["GroupBy"][0]["Key"] == "LINKED_ACCOUNT":
            groups = [{"Keys": [a, SERVICES[0]], "Metrics": {
                "UnblendedCost": {"Amount": str(billed[a]), "Unit": "USD"},
            }} for a in ids]
        else:
            groups = [{"Keys": [SERVICES[0], "InputTokens"], "Metrics": {
                "UnblendedCost": {"Amount": str(sum(billed[a] for a in ids))},
                "UsageQuantity": {"Amount": "10"},
            }}]
        return {"ResultsByTime": [{"TimePeriod": {"Start": START.isoformat()}, "Groups": groups}]}

    ce = SimpleNamespace(get_dimension_values=dimensions, get_cost_and_usage=fetch)
    monkeypatch.setattr(cost, "_ce_client", lambda: ce)
    conn = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(), close=AsyncMock())
    isolated.connection.side_effect = None
    isolated.connection.return_value = conn
    assert asyncio.run(cost.main()) == 0
    usage_call, daily_call = conn.executemany.call_args_list
    assert "INSERT INTO f_daily_cost_usage_type" in usage_call.args[0]
    assert usage_call.args[1] == [(START, SERVICES[0], "InputTokens", 5.0, 10.0)]
    assert {r[1] for r in daily_call.args[1]} == {MEMBER, OTHER}
    assert sum(r[3] for r in daily_call.args[1]) == 5
    assert len(requests) == 4
    conn.close.assert_awaited_once()

    # An OU that becomes empty must leave storage untouched, not overwrite it
    # with the billing family's 1005 dollars.
    monkeypatch.setattr(accounts, "discover_from_ous", Mock(return_value=[]))
    assert asyncio.run(cost.main()) == 0
    assert len(requests) == 4
    assert isolated.connection.await_count == 1
    assert conn.executemany.await_count == 2


@pytest.fixture
def scheduler(monkeypatch):
    observed = []
    for name in COLLECTORS:
        async def main(module=name):
            observed.append((module, list(sys.argv)))
            if module != "model_lifecycle":
                parser = argparse.ArgumentParser()
                accounts._add_common_args(parser)
                args, _ = parser.parse_known_args()
                discovered = accounts.discover_accounts(args)
                assert [a.accountId for a in discovered] == [MEMBER, OTHER]
            return 0
        monkeypatch.setitem(sys.modules, f"ingestion.{name}", SimpleNamespace(main=main))
    refresh, findings, bump = AsyncMock(), AsyncMock(return_value={}), Mock()
    monkeypatch.setattr(lambda_handler, "_refresh_account_names", refresh)
    monkeypatch.setattr(lambda_handler, "_evaluate_findings", findings)
    monkeypatch.setattr(lambda_handler, "_bump_cache_generation", bump)
    return SimpleNamespace(observed=observed, refresh=refresh, findings=findings, bump=bump)


@pytest.mark.parametrize("membership", [[], "denied"])
def test_orchestration_checks_ou_before_any_collector_or_side_effect(
    monkeypatch, isolated, scheduler, membership,
):
    monkeypatch.delenv("DATABASE_URL")
    discovery = Mock(return_value=membership, side_effect=denied() if membership == "denied" else None)
    monkeypatch.setattr(accounts, "discover_from_ous", discovery)
    if membership == "denied":
        with pytest.raises(ClientError):
            lambda_handler.handler({}, None)
    else:
        result = lambda_handler.handler({}, None)
        assert result["status"] == "ok"
        assert result["runs"] == []
        assert result["skipped_reason"] == "empty_ou_scope"
        assert result["failed_count"] == 0
    assert scheduler.observed == []
    scheduler.refresh.assert_not_called()
    scheduler.findings.assert_not_called()
    scheduler.bump.assert_not_called()
    isolated.clients.assert_not_called()
    isolated.connection.assert_not_called()


def test_every_collector_reuses_one_ou_snapshot_including_aip_and_minute_peaks(
    monkeypatch, scheduler,
):
    monkeypatch.setenv("MINUTE_PEAK_DAYS", "8")
    selected = [accounts.MonitoredAccount(MEMBER, "one"), accounts.MonitoredAccount(OTHER, "two")]
    discovery = Mock(side_effect=[selected, AssertionError("second lookup could change scope")])
    monkeypatch.setattr(accounts, "discover_from_ous", discovery)
    result = asyncio.run(lambda_handler._orchestrate(None, 7, logs_budget_s=123))
    assert [name for name, _ in scheduler.observed] == COLLECTORS
    assert all(r["rc"] == 0 for r in result["runs"])
    discovery.assert_called_once_with((OU,))
    scheduler.refresh.assert_awaited_once_with(selected)
    commands = dict(scheduler.observed)
    logs = commands["invocation_logs"]
    assert logs[logs.index("--max-seconds") + 1] == "123"
    minute = commands["cw_minute_peak"]
    assert minute[minute.index("--days") + 1] == "8"
    assert commands["cost"][commands["cost"].index("--days") + 1] == "30"


def test_ou_proxy_collection_receives_the_same_scope_as_other_collectors(monkeypatch, scheduler):
    monkeypatch.setattr(accounts, "discover_from_ous", Mock(return_value=[
        accounts.MonitoredAccount(MEMBER), accounts.MonitoredAccount(OTHER),
    ]))
    result = lambda_handler.handler({"only": ["proxy_events", "inference_profiles"]}, None)
    assert result["status"] == "ok" and result["failed_count"] == 0
    assert [name for name, _ in scheduler.observed] == ["proxy_events", "inference_profiles"]


def test_warm_invocation_does_not_reuse_previous_ou_membership(monkeypatch, scheduler):
    monkeypatch.delenv("PROXY_EVENTS_BUCKET")
    selected = [accounts.MonitoredAccount(MEMBER), accounts.MonitoredAccount(OTHER)]
    discovery = Mock(side_effect=[selected, [], denied()])
    monkeypatch.setattr(accounts, "discover_from_ous", discovery)
    assert lambda_handler.handler({"only": ["inference_profiles"]}, None)["status"] == "ok"
    assert lambda_handler.handler({}, None)["skipped_reason"] == "empty_ou_scope"
    with pytest.raises(ClientError):
        lambda_handler.handler({}, None)
    assert len(scheduler.observed) == 1
    assert scheduler.refresh.await_count == 1
    assert discovery.call_count == 3


def test_empty_account_name_refresh_never_opens_clients_or_database(isolated):
    assert accounts.resolve_account_names([]) == []
    asyncio.run(lambda_handler._refresh_account_names([]))
    isolated.clients.assert_not_called()
    isolated.connection.assert_not_called()


def test_account_name_refresh_uses_supplied_snapshot_without_rediscovery(monkeypatch, isolated):
    selected = [accounts.MonitoredAccount(MEMBER, "member")]
    discovery = Mock(side_effect=AssertionError("must use the supplied snapshot"))
    monkeypatch.setattr(accounts, "discover_accounts", discovery)
    resolved = [(MEMBER, "member", "org")]
    resolver = Mock(return_value=resolved)
    monkeypatch.setattr(accounts, "resolve_account_names", resolver)
    conn = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(), close=AsyncMock())
    isolated.connection.side_effect = None
    isolated.connection.return_value = conn
    asyncio.run(lambda_handler._refresh_account_names(selected))
    discovery.assert_not_called()
    resolver.assert_called_once_with(selected, {})
    assert conn.executemany.call_args.args[1] == resolved
    conn.close.assert_awaited_once()


@pytest.fixture
def live_scope(monkeypatch):
    state = SimpleNamespace(
        env={"MONITORED_ACCOUNTS_MODE": "discover-ou", "MONITORED_OU_IDS": OU,
             "BEDROCK_OPS_LENS_ROLE_NAME": "team/Reader", "BEDROCK_OPS_LENS_EXTERNAL_ID": "ext"},
        tree={OU: [], CHILD: [MEMBER], OTHER_OU: [OTHER]},
        children={OU: [CHILD]}, calls=[], settings_reads=0, denied_parent=None,
    )

    def paginator(name):
        def paginate(**kw):
            parent = kw["ParentId"]
            state.calls.append((name, parent))
            if parent == state.denied_parent:
                raise denied()
            if name == "list_accounts_for_parent":
                yield {"Accounts": [
                    {"Id": a, "Name": f"Account {a}", "State": "ACTIVE"}
                    for a in state.tree[parent]
                ]}
            elif name == "list_organizational_units_for_parent":
                yield {"OrganizationalUnits": [{"Id": ou} for ou in state.children.get(parent, [])]}
            else:
                raise AssertionError("must not discover the entire organization")
        return SimpleNamespace(paginate=paginate)

    def settings(**kw):
        assert kw == {"FunctionName": "fixture-ingester"}
        state.settings_reads += 1
        return {"Environment": {"Variables": dict(state.env)}}

    def client(service, **kw):
        if service == "organizations":
            return SimpleNamespace(get_paginator=paginator)
        assert service == "lambda"
        return SimpleNamespace(get_function_configuration=settings)

    monkeypatch.setattr(boto3, "client", client)
    state.sessions = SimpleNamespace(self_account=CENTRAL, session_for=Mock(return_value=object()))
    state.new_sessions = Mock(return_value=state.sessions)
    monkeypatch.setattr(live_pull, "new_session_cache", state.new_sessions)
    # A warm discover-org cache must never authorize an OU request.
    monkeypatch.setattr(live_pull, "_org_cache", (float("inf"), frozenset([CENTRAL, MEMBER, OTHER])))
    return state


def test_live_pull_and_ingester_discover_the_same_nested_members(live_scope):
    discovered = accounts.discover_accounts(SimpleNamespace())
    assert [a.accountId for a in discovered] == [MEMBER]
    live_pull.open_session(MEMBER)
    live_scope.sessions.session_for.assert_called_once_with(MEMBER, "team/Reader", "ext")
    for account in [OTHER, CENTRAL]:
        with pytest.raises(live_pull.NotMonitored):
            live_pull.open_session(account)
    assert live_scope.new_sessions.call_count == 1
    assert live_scope.settings_reads == 3


def test_live_pull_reads_changed_ou_settings_without_baked_config_fallback(live_scope):
    live_pull.open_session(MEMBER)
    live_scope.env["MONITORED_OU_IDS"] = OTHER_OU
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)
    live_pull.open_session(OTHER)
    assert [c.args[0] for c in live_scope.sessions.session_for.call_args_list] == [MEMBER, OTHER]
    assert live_scope.settings_reads == 3


def test_live_pull_rechecks_membership_on_each_request(live_scope):
    live_pull.open_session(MEMBER)
    live_scope.tree[CHILD] = [OTHER]
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)
    live_pull.open_session(OTHER)
    assert live_scope.calls.count(("list_accounts_for_parent", CHILD)) == 3
    assert live_scope.new_sessions.call_count == 2


@pytest.mark.parametrize("ou_ids", [None, "", " , "])
def test_missing_live_ou_ids_cannot_reuse_baked_scope(live_scope, ou_ids):
    if ou_ids is None:
        live_scope.env.pop("MONITORED_OU_IDS")
    else:
        live_scope.env["MONITORED_OU_IDS"] = ou_ids
    with pytest.raises(live_pull.ScopeUnavailable):
        live_pull.open_session(MEMBER)
    assert live_scope.calls == []
    live_scope.new_sessions.assert_not_called()


@pytest.mark.parametrize("unavailable", [False, True])
@pytest.mark.parametrize("account", [CENTRAL, MEMBER])
def test_live_empty_or_denied_ou_never_opens_target_session(live_scope, unavailable, account):
    live_scope.tree[CHILD] = []
    if unavailable:
        # Even seeing the requested account in the root must not authorize it
        # before the rest of the membership lookup has succeeded.
        live_scope.tree[OU] = [account]
        live_scope.denied_parent = CHILD
    expected = live_pull.ScopeUnavailable if unavailable else live_pull.NotMonitored
    with pytest.raises(expected):
        live_pull.open_session(account)
    live_scope.new_sessions.assert_not_called()
    live_scope.sessions.session_for.assert_not_called()


def test_live_scope_denial_is_reported_as_failure_without_metrics(live_scope):
    live_scope.denied_parent = CHILD
    result = live_pull.handler({
        "account_id": CENTRAL, "region": "us-east-1", "model_id": "test-model",
    }, None)
    assert result["ok"] is False and result["error"] == "failed"
    live_scope.new_sessions.assert_not_called()


def test_account_list_uses_current_nested_ou_members_and_names(live_scope, monkeypatch):
    # A live OU selection must work even when the image's baked scope is invalid.
    monkeypatch.setattr(live_pull, "load_config", Mock(side_effect=ValueError("stale config")))
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [{"accountId": MEMBER, "name": f"Account {MEMBER}"}],
    }
    live_scope.tree[CHILD] = [OTHER]
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [{"accountId": OTHER, "name": f"Account {OTHER}"}],
    }
    live_scope.env["MONITORED_OU_IDS"] = OTHER_OU
    live_scope.tree[OTHER_OU] = [CENTRAL]
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [{"accountId": CENTRAL, "name": f"Account {CENTRAL}"}],
    }
    assert live_scope.settings_reads == 3
    live_scope.new_sessions.assert_not_called()
    live_scope.sessions.session_for.assert_not_called()


def test_account_list_empty_ou_is_success_with_no_fallback(live_scope):
    live_scope.tree[CHILD] = []
    assert live_pull.handler({"action": "list-accounts"}, None) == {"ok": True, "accounts": []}
    live_scope.new_sessions.assert_not_called()


@pytest.mark.parametrize("unavailable", ["missing", "empty", "denied", "environment"])
def test_account_list_unavailable_scope_never_returns_partial_or_baked_members(
    live_scope, monkeypatch, unavailable,
):
    if unavailable == "missing":
        live_scope.env.pop("MONITORED_OU_IDS")
    elif unavailable == "empty":
        live_scope.env["MONITORED_OU_IDS"] = ""
    elif unavailable == "denied":
        live_scope.tree[OU] = [CENTRAL]  # a partial result must not escape
        live_scope.denied_parent = CHILD
    else:
        monkeypatch.setattr(live_pull, "ingester_settings", Mock(side_effect=denied()))
    out = live_pull.handler({"action": "list-accounts"}, None)
    assert out["ok"] is False and out["error"] == "failed"
    assert "accounts" not in out
    live_scope.new_sessions.assert_not_called()
    live_scope.sessions.session_for.assert_not_called()


@pytest.mark.parametrize("event", [
    {"action": "purge_proxy"},
    {"action": "list-accounts", "MONITORED_OU_IDS": OTHER_OU},
    {"action": "list-accounts", "account_id": OTHER},
    {"action": "list-accounts", "admin": "migrate"},
])
def test_account_list_does_not_accept_scope_overrides_or_other_actions(live_scope, event):
    out = live_pull.handler(event, None)
    assert out["ok"] is False and out["error"] == "invalid_request"
    assert live_scope.settings_reads == 0 and live_scope.calls == []
    live_scope.new_sessions.assert_not_called()


def test_account_list_explicit_scope_is_live_and_does_not_assume_roles(live_scope):
    live_scope.env.update(MONITORED_ACCOUNTS_MODE="explicit",
                          MONITORED_ACCOUNTS_IDS=f"{OTHER},{MEMBER},{OTHER}")
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [
            {"accountId": OTHER, "name": ""}, {"accountId": MEMBER, "name": ""},
        ],
    }
    assert live_scope.calls == []  # no Organizations lookup for explicit scope
    live_scope.new_sessions.assert_not_called()


@pytest.mark.parametrize("ids", [None, ""])
def test_blank_live_explicit_ids_cannot_restore_stale_baked_members(
    live_scope, isolated, ids,
):
    isolated.config.monitored_accounts = config.MonitoredAccountsConfig(
        mode="explicit", ids=(MEMBER,),
    )
    live_scope.env["MONITORED_ACCOUNTS_MODE"] = "explicit"
    if ids is not None:
        live_scope.env["MONITORED_ACCOUNTS_IDS"] = ids
    # Match the ingester's existing empty-explicit fallback to its own account.
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [{"accountId": CENTRAL, "name": ""}],
    }
    with pytest.raises(live_pull.NotMonitored):
        live_pull.open_session(MEMBER)
    live_scope.sessions.session_for.assert_not_called()


@pytest.mark.parametrize("mode", ["single", "explicit"])
def test_account_list_running_account_fallback_never_assumes_a_role(live_scope, mode):
    live_scope.env.update(MONITORED_ACCOUNTS_MODE=mode, MONITORED_ACCOUNTS_IDS="")
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [{"accountId": CENTRAL, "name": ""}],
    }
    assert live_scope.calls == []
    live_scope.sessions.session_for.assert_not_called()


def test_account_list_org_scope_bypasses_metric_cache_and_keeps_names(live_scope, monkeypatch):
    live_scope.env["MONITORED_ACCOUNTS_MODE"] = "discover-org"
    discovery = Mock(side_effect=[
        [accounts.MonitoredAccount(MEMBER, "First member")],
        [accounts.MonitoredAccount(OTHER, "New member")],
        [],
    ])
    monkeypatch.setattr(live_pull, "discover_from_org", discovery)
    assert live_pull.handler({"action": "list-accounts"}, None)["accounts"] == [
        {"accountId": MEMBER, "name": "First member"},
    ]
    assert live_pull.handler({"action": "list-accounts"}, None)["accounts"] == [
        {"accountId": OTHER, "name": "New member"},
    ]
    assert live_pull.handler({"action": "list-accounts"}, None)["accounts"] == []
    assert discovery.call_count == 3
    live_scope.new_sessions.assert_not_called()


def test_account_list_org_failure_matches_legacy_ingester_fallback(live_scope, monkeypatch):
    live_scope.env["MONITORED_ACCOUNTS_MODE"] = "discover-org"
    monkeypatch.setattr(live_pull, "discover_from_org", Mock(side_effect=denied()))
    assert live_pull.handler({"action": "list-accounts"}, None) == {
        "ok": True, "accounts": [{"accountId": CENTRAL, "name": ""}],
    }
    live_scope.sessions.session_for.assert_not_called()


def test_unreadable_live_environment_does_not_become_single_account(monkeypatch, isolated):
    client = SimpleNamespace(get_function_configuration=Mock(return_value={
        "Environment": {"Error": {"ErrorCode": "KMSAccessDeniedException"}},
    }))
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: client)
    with pytest.raises(live_pull.ScopeUnavailable):
        live_pull.open_session(CENTRAL)
    live_pull.new_session_cache.assert_not_called()


@pytest.mark.parametrize("contents", [
    "monitored_accounts:\n  mode: discover-ou\n  ou_ids: [not-an-ou]\n",
    "monitored_accounts:\n  mode: discover-ou\n  ou_ids: [\n",
])
def test_invalid_yaml_only_scope_propagates_without_running_account_fallback(
    monkeypatch, tmp_path, isolated, contents,
):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(contents)
    for key in ("MONITORED_ACCOUNTS_MODE", "MONITORED_OU_IDS", "MONITORED_ACCOUNTS_IDS"):
        monkeypatch.delenv(key, raising=False)
    for module in (config, live_pull):
        monkeypatch.setattr(module, "load_config", lambda: REAL_LOAD_CONFIG(cfg_file))
    with pytest.raises((ValueError, yaml.YAMLError)):
        accounts.discover_accounts(SimpleNamespace())
    accounts.session_cache.assert_not_called()
    isolated.clients.assert_not_called()

    # Live pulls without an environment override have the same YAML scope.
    monkeypatch.setattr(live_pull, "ingester_settings", lambda: {})
    with pytest.raises(live_pull.ScopeUnavailable):
        live_pull.open_session(CENTRAL)
    live_pull.new_session_cache.assert_not_called()


def test_legacy_org_lookup_failure_still_uses_running_account(monkeypatch, isolated):
    isolated.config.monitored_accounts = config.MonitoredAccountsConfig(mode="discover-org")
    monkeypatch.setattr(accounts, "discover_from_org", Mock(side_effect=denied()))
    monkeypatch.setattr(accounts, "session_cache", Mock(return_value=SimpleNamespace(
        self_account=CENTRAL,
    )))
    assert [a.accountId for a in accounts.discover_accounts(SimpleNamespace())] == [CENTRAL]


@pytest.fixture
def proxy_io(monkeypatch, isolated):
    """Exercise the real parser and writer against a mixed-account S3 object."""
    now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    key = "proxy-events/us-east-1/2026/10/20/12/mixed.jsonl"
    state = SimpleNamespace(
        ledger={}, raw={}, rollups=defaultdict(int), raw_batches=[], rollup_batches=[],
        reads=[], writes=[], objects={}, dimensions=set(),
    )

    def event(account, request_id, *, old=False):
        ev = {
            "ts": (now - timedelta(days=20 if old else 1)).isoformat(),
            "model": "test-model", "region": "us-east-1", "endpoint": "runtime",
            "dimensions": {"workload": str(account or "unknown")},
            "input_tokens": 11, "output_tokens": 7, "cache_write_tokens": 3,
            "status": 200, "request_id": request_id,
        }
        if account is not None:
            ev["accountId"] = account
        return ev

    # Excluded events deliberately collide with the allowed request's identity.
    # Filtering after dedup would lose the allowed event.
    records = [
        event(CENTRAL, "same-id"), event(None, "same-id"),
        event(MEMBER, "same-id"), event(MEMBER, "same-id"), event(OTHER, "other-id"),
    ]
    state.event = event
    state.key = key
    state.now = now
    state.objects[key] = b"\n".join(json.dumps(e).encode() for e in records)

    def read(*, Bucket, Key):
        assert Bucket == "fixture-proxy"
        state.reads.append(Key)
        return {"Body": io.BytesIO(state.objects[Key])}

    async def fetch(sql, *args):
        if "SELECT s3_key" in sql:
            keys = args[0]
            prefixes = [p[:-1] for p in args[1]] if len(args) > 1 else []
            return [{"s3_key": k} for k in state.ledger
                    if k in keys or any(k.startswith(prefix) for prefix in prefixes)]
        assert "INSERT INTO f_request_events" in sql
        rows = list(zip(*args))
        state.raw_batches.append(rows)
        state.writes.append(("raw", rows))
        returned = []
        for row in rows:
            ident = (row[1], row[17], row[0])
            if ident not in state.raw:
                state.raw[ident] = row
                returned.append({"event_date": row[1], "request_id": row[17], "ts": row[0]})
        return returned

    async def executemany(sql, rows):
        rows = list(rows)
        if "INSERT INTO proxy_events_objects" in sql:
            state.writes.append(("ledger", rows))
            state.ledger.update(rows)
        else:
            assert "INSERT INTO f_proxy_dim_hourly" in sql
            state.rollup_batches.append(rows)
            state.writes.append(("rollup", rows))
            for row in rows:
                state.rollups[(row[0], row[2], row[3], row[7])] += row[8]

    async def execute(sql, *args):
        state.writes.append((sql, args))
        if sql.startswith("DELETE FROM f_request_events"):
            assert "accountId = ANY($2::text[])" in sql
            retention, selected = args
            for ident, row in list(state.raw.items()):
                if row[1] < (now - timedelta(days=retention)).date() and (
                    selected is None or row[6] in selected
                ):
                    del state.raw[ident]
        elif "INSERT INTO dim_proxy_dimensions" in sql:
            assert "accountId = ANY($1::text[])" in sql
            [selected] = args
            state.dimensions = {
                value for (_, dim, value, account) in state.rollups
                if dim != "__all__" and (selected is None or account in selected)
            }
        else:
            assert sql == "DELETE FROM dim_proxy_dimensions" or "INSERT INTO ingestion_meta" in sql

    connection = SimpleNamespace(
        fetch=AsyncMock(side_effect=fetch), executemany=AsyncMock(side_effect=executemany),
        execute=AsyncMock(side_effect=execute), close=AsyncMock(), fetchval=AsyncMock(return_value=0),
    )
    state.connection = connection
    isolated.connection.side_effect = None
    isolated.connection.return_value = connection
    monkeypatch.setattr(proxy_events, "_s3_client", Mock(return_value=SimpleNamespace(get_object=read)))
    monkeypatch.setattr(proxy_events, "_list_event_keys", lambda *a: list(state.objects))
    monkeypatch.setattr(proxy_events, "_OBJ_ERRORS", {})
    monkeypatch.setattr(proxy_events, "datetime", SimpleNamespace(
        now=lambda tz: now, fromisoformat=datetime.fromisoformat,
    ))

    def run(selected):
        argv = ["proxy_events", "--bucket", "fixture-proxy", "--regions", "us-east-1",
                "--days", "30"]
        if selected is not None:
            argv += ["--accounts", selected]
        monkeypatch.setattr(sys, "argv", argv)
        return asyncio.run(proxy_events.main())
    state.run = run
    return state


@pytest.mark.parametrize("selected", [MEMBER, f"{MEMBER},{OTHER}"])
def test_proxy_filters_mixed_objects_before_raw_writes_and_dedup(proxy_io, selected):
    allowed = set(selected.split(","))
    assert proxy_io.run(selected) == 0
    assert proxy_io.reads == [proxy_io.key]
    assert {r[6] for batch in proxy_io.raw_batches for r in batch} == allowed
    assert sum(len(batch) for batch in proxy_io.raw_batches) == len(allowed)
    assert {r[7] for batch in proxy_io.rollup_batches for r in batch} == allowed
    totals = {a: n for (_, dim, _, a), n in proxy_io.rollups.items() if dim == "__all__"}
    assert totals == dict.fromkeys(allowed, 1)
    assert proxy_io.dimensions == allowed
    assert len(proxy_io.ledger) == len(allowed)
    assert proxy_io.key not in proxy_io.ledger  # other accounts remain eligible
    assert all(marker.rsplit(":", 1)[1] in allowed for marker in proxy_io.ledger)


@pytest.mark.parametrize("selected", ["", " , ", "not-an-account"])
def test_empty_proxy_scope_makes_no_reads_or_writes(proxy_io, isolated, selected):
    assert proxy_io.run(selected) == 0
    assert proxy_io.reads == [] and proxy_io.writes == []
    proxy_events._s3_client.assert_not_called()
    isolated.connection.assert_not_called()


def test_proxy_ou_expansion_replays_only_new_accounts_even_beyond_raw_retention(proxy_io):
    records = [
        proxy_io.event(MEMBER, "old-member", old=True),
        proxy_io.event(OTHER, "old-other", old=True),
    ]
    proxy_io.objects[proxy_io.key] = b"\n".join(json.dumps(e).encode() for e in records)
    assert proxy_io.run(MEMBER) == 0
    assert proxy_io.raw_batches == []  # too old for raw-row dedup to help
    assert proxy_io.run(f"{MEMBER},{OTHER}") == 0
    assert proxy_io.reads == [proxy_io.key, proxy_io.key]
    totals = {a: n for (_, dim, _, a), n in proxy_io.rollups.items() if dim == "__all__"}
    assert totals == {MEMBER: 1, OTHER: 1}
    assert proxy_io.run(f"{OTHER},{MEMBER}") == 0
    assert proxy_io.run(MEMBER) == 0
    assert len(proxy_io.reads) == 2

    # Removing the filter later must also honor previously scoped progress.
    assert proxy_io.run(None) == 0
    assert proxy_io.key in proxy_io.ledger
    assert proxy_io.run(MEMBER) == 0
    assert len(proxy_io.reads) == 3
    assert {a: n for (_, dim, _, a), n in proxy_io.rollups.items()
            if dim == "__all__"} == {MEMBER: 1, OTHER: 1}


def test_proxy_honors_existing_whole_object_markers(proxy_io):
    proxy_io.ledger[proxy_io.key] = 5
    assert proxy_io.run(MEMBER) == 0
    assert proxy_io.reads == []
    assert proxy_io.raw_batches == [] and proxy_io.rollup_batches == []


def test_proxy_scoped_retention_and_dimensions_do_not_touch_outside_accounts(proxy_io):
    old = proxy_io.now - timedelta(days=20)
    for account in (MEMBER, CENTRAL):
        proxy_io.raw[(old.date(), account, old)] = (
            old, old.date(), "{}", "test-model", "runtime", "us-east-1", account,
        )
        proxy_io.rollups[(old.date(), "workload", account, account)] = 10
    assert proxy_io.run(MEMBER) == 0
    assert (old.date(), MEMBER, old) not in proxy_io.raw
    assert (old.date(), CENTRAL, old) in proxy_io.raw
    assert proxy_io.dimensions == {MEMBER}
    assert proxy_io.rollups[(old.date(), "workload", CENTRAL, CENTRAL)] == 10


def test_proxy_without_scope_preserves_unknown_account_telemetry(proxy_io):
    proxy_io.objects[proxy_io.key] = json.dumps(proxy_io.event(None, "unknown-account")).encode()
    assert proxy_io.run(None) == 0
    assert [r[6] for r in proxy_io.raw_batches[0]] == ["__none__"]
    assert proxy_io.key in proxy_io.ledger


def test_real_proxy_writer_runs_under_the_orchestrators_ou_snapshot(
    monkeypatch, scheduler, proxy_io,
):
    monkeypatch.setitem(sys.modules, "ingestion.proxy_events", proxy_events)
    selected = [accounts.MonitoredAccount(MEMBER), accounts.MonitoredAccount(OTHER)]
    discovery = Mock(return_value=selected)
    monkeypatch.setattr(accounts, "discover_from_ous", discovery)
    result = lambda_handler.handler({"only": ["proxy_events"]}, None)
    assert result["status"] == "ok"
    assert {r[6] for batch in proxy_io.raw_batches for r in batch} == {MEMBER, OTHER}
    discovery.assert_called_once_with((OU,))
    scheduler.refresh.assert_awaited_once_with(selected)
