"""The Overview "Account split" pie folds every account past the top six into
an "Other" slice, so /account-type-split has to return every account group.
An earlier LIMIT 8 made that slice silently omit accounts nine onwards."""
from __future__ import annotations

from test_inference_profiles_db import TODAY, conn, daily, postgres  # noqa: F401

from app.filters import FilterSet
from app.routers import extras

MODEL = "amazon.nova-pro-v1:0"


async def test_every_account_group_is_returned_largest_first(conn):
    accounts = [f"{100000000000 + i}" for i in range(12)]
    for i, account in enumerate(accounts):
        await daily(conn, MODEL, (i + 1) * 100, account=account)

    rows = await extras.account_type_split(FilterSet(start=TODAY, end=TODAY))

    assert len(rows) == 12
    assert [r["total_requests"] for r in rows] == [(12 - i) * 100 for i in range(12)]
    assert {r["account_type"] for r in rows} == set(accounts)
    assert all(r["unique_accounts"] == 1 for r in rows)
    assert sum(r["total_requests"] for r in rows) == sum((i + 1) * 100 for i in range(12))


async def test_account_filter_still_scopes_the_split(conn):
    for i in range(3):
        await daily(conn, MODEL, 50 * (i + 1), account=f"{200000000000 + i}")
    rows = await extras.account_type_split(
        FilterSet(start=TODAY, end=TODAY, accounts=("200000000001",)))
    assert [(r["account_type"], r["total_requests"]) for r in rows] == [("200000000001", 100)]
