"""discover-ou: collection scope equals rollout scope.

`setup-pipeline.sh --scope ou` rolls reader roles out to one or more OUs with a
service-managed StackSet, and now sets the ingester to `discover-ou`. These
tests pin the resolver: child OUs are included, inactive accounts are skipped,
duplicates collapse, and a lookup failure RAISES instead of quietly widening to
the whole organization or narrowing to the running account.

Run: .venv/bin/python -m pytest tests/test_discover_ou.py -q
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ingestion import accounts, config


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return list(self._pages(kwargs["ParentId"]))


class _FakeOrg:
    """ou-root -> [acct A (active), acct B (suspended)] + child ou-child -> [acct C, acct A again]."""
    tree = {
        "ou-aaaa-11111111": {"accounts": [("111111111111", "ACTIVE", "alpha"), ("222222222222", "SUSPENDED", "beta")],
                             "children": ["ou-aaaa-22222222"]},
        "ou-aaaa-22222222": {"accounts": [("333333333333", "ACTIVE", "gamma"), ("111111111111", "ACTIVE", "alpha")],
                             "children": []},
        "ou-bbbb-33333333": {"accounts": [("444444444444", "ACTIVE", "delta")], "children": []},
    }

    def __init__(self):
        self.calls = []

    def get_paginator(self, name):
        def pages(parent):
            self.calls.append((name, parent))
            if parent not in self.tree:
                raise ClientError({"Error": {"Code": "ParentNotFoundException", "Message": parent}}, name)
            node = self.tree[parent]
            if name == "list_accounts_for_parent":
                yield {"Accounts": [{"Id": i, "State": st, "Name": n} for i, st, n in node["accounts"]]}
            else:
                yield {"OrganizationalUnits": [{"Id": c} for c in node["children"]]}
        return _Paginator(pages)


@pytest.fixture
def fake_org(monkeypatch):
    org = _FakeOrg()
    monkeypatch.setattr(accounts.boto3, "client", lambda service, **kw: org)
    return org


def test_resolves_members_including_child_ous_without_duplicates(fake_org):
    got = accounts.discover_from_ous(["ou-aaaa-11111111"])
    assert sorted(a.accountId for a in got) == ["111111111111", "333333333333"]
    assert {a.accountId: a.name for a in got}["333333333333"] == "gamma"


def test_multiple_ous_union(fake_org):
    got = accounts.discover_from_ous(["ou-aaaa-22222222", "ou-bbbb-33333333"])
    assert sorted(a.accountId for a in got) == ["111111111111", "333333333333", "444444444444"]


def test_lookup_failure_raises_instead_of_changing_scope(fake_org):
    with pytest.raises(ClientError):
        accounts.discover_from_ous(["ou-zzzz-99999999"])


def test_empty_ou_list_is_an_error(fake_org):
    with pytest.raises(ValueError):
        accounts.discover_from_ous([])


def test_config_reads_ou_ids_from_env_and_rejects_garbage(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("monitored_accounts:\n  mode: discover-ou\n  ou_ids: [ou-aaaa-11111111, nonsense]\n")
    monkeypatch.delenv("MONITORED_ACCOUNTS_MODE", raising=False)
    monkeypatch.delenv("MONITORED_OU_IDS", raising=False)
    with pytest.raises(ValueError, match="valid OU/root IDs"):
        config.load_config(cfg_file)
    monkeypatch.setenv("MONITORED_OU_IDS", "ou-bbbb-33333333,ou-aaaa-22222222")
    cfg = config.load_config(cfg_file)
    assert cfg.monitored_accounts.mode == "discover-ou"
    assert cfg.monitored_accounts.ou_ids == ("ou-bbbb-33333333", "ou-aaaa-22222222")


def test_discover_accounts_in_ou_mode_does_not_fall_back_to_single(monkeypatch, fake_org):
    """The legacy discover-org path swallows errors and narrows to the running
    account. discover-ou must not: a broken lookup is a failed run. Env
    overrides take precedence over config.yaml, so drive the mode through them."""
    monkeypatch.setenv("MONITORED_ACCOUNTS_MODE", "discover-ou")
    monkeypatch.setenv("MONITORED_OU_IDS", "ou-zzzz-99999999")

    class Args:
        accounts = None
        accounts_config = None
        discover_org = False
        discover_ou = None

    with pytest.raises(ClientError):
        accounts.discover_accounts(Args())


def test_cli_flag_discover_ou(fake_org):
    class Args:
        accounts = None
        accounts_config = None
        discover_org = False
        discover_ou = "ou-bbbb-33333333"
    got = accounts.discover_accounts(Args())
    assert [a.accountId for a in got] == ["444444444444"]


@pytest.mark.parametrize("discovery", ["org", "ou"])
@pytest.mark.parametrize(("state", "included"), [
    ({"State": "ACTIVE"}, True),
    ({"Status": "ACTIVE"}, True),
    ({"State": "SUSPENDED", "Status": "ACTIVE"}, False),
    ({"State": "ACTIVE", "Status": "SUSPENDED"}, True),
    ({"State": "PENDING_ACTIVATION"}, False),
])
def test_state_precedes_retired_status(monkeypatch, discovery, state, included):
    account = {"Id": "111111111111", "Name": "alpha", **state}

    def paginator(name):
        pages = ([{"Accounts": [account]}] if name != "list_organizational_units_for_parent"
                 else [{"OrganizationalUnits": []}])
        return SimpleNamespace(paginate=lambda **kw: pages)

    monkeypatch.setattr(accounts.boto3, "client", lambda *a, **kw: SimpleNamespace(
        get_paginator=paginator,
    ))
    got = (accounts.discover_from_org() if discovery == "org"
           else accounts.discover_from_ous(["ou-aaaa-11111111"]))
    assert [a.accountId for a in got] == (["111111111111"] if included else [])


def test_empty_pages_do_not_end_account_or_child_ou_pagination(monkeypatch):
    root, child = "ou-aaaa-11111111", "ou-aaaa-22222222"
    visited = []

    def paginator(name):
        def paginate(**kw):
            parent = kw["ParentId"]
            visited.append((name, parent))
            yield {}
            if name == "list_accounts_for_parent":
                yield {"Accounts": [{"Id": "111111111111" if parent == root else "222222222222",
                                     "State": "ACTIVE"}]}
            else:
                yield {"OrganizationalUnits": [{"Id": child}] if parent == root else []}
        return SimpleNamespace(paginate=paginate)

    monkeypatch.setattr(accounts.boto3, "client", lambda *a, **kw: SimpleNamespace(
        get_paginator=paginator,
    ))
    got = accounts.discover_from_ous([root, child, root])
    assert sorted(a.accountId for a in got) == ["111111111111", "222222222222"]
    assert len(visited) == len(set(visited)) == 4


def test_ou_config_failure_does_not_fall_back_to_running_account(monkeypatch):
    monkeypatch.setenv("MONITORED_ACCOUNTS_MODE", "discover-ou")
    monkeypatch.setattr(config, "load_config", Mock(side_effect=ValueError("invalid config")))
    fallback = Mock(side_effect=AssertionError("must not inspect running credentials"))
    monkeypatch.setattr(accounts, "session_cache", fallback)
    with pytest.raises(ValueError, match="invalid config"):
        accounts.discover_accounts(SimpleNamespace())
    fallback.assert_not_called()
