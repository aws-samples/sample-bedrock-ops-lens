"""Live pull's SQL against real PostgreSQL views in a disposable local cluster.

Requires PostgreSQL 15+ binaries on PATH (skipped otherwise). Never connects to
DATABASE_URL or a running database; the function invoke is mocked.
"""
from __future__ import annotations

from unittest.mock import Mock

from fastapi import HTTPException
import pytest

from test_inference_profiles_db import (  # noqa: F401 - fixtures by name
    ACCOUNT, MODEL, REGION, catalog, conn, daily, postgres, profile,
)

from app.routers import live_pull


@pytest.fixture
def invoke(monkeypatch):
    monkeypatch.setenv("LIVE_PULL_FUNCTION_NAME", "lens-live-pull")
    monkeypatch.setattr(live_pull, "_recent", {})
    mock = Mock(return_value={"ok": True, "status": "complete", "minutes": []})
    monkeypatch.setattr(live_pull, "_invoke", mock)
    return mock


def request(**extra):
    return live_pull.LivePullRequest(**{"account_id": ACCOUNT, "region": REGION,
                                        "model_id": MODEL, "hours": 1, **extra})


async def test_identifiers_are_the_model_and_only_its_own_profiles(conn):
    a, b = profile(), profile("bbbbbb123456")
    other_model = profile("cccccc123456")
    other_model["models"] = [{"modelArn": f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-pro-v1:0"}]
    await catalog(conn, a, b, other_model)
    elsewhere = profile("dddddd123456", account="222222222222")
    await conn.execute(
        """INSERT INTO public.dim_inference_profiles
             (accountid, region, profile_id, profile_arn, profile_name, model_id,
              model_arns, destination_regions)
           VALUES ($1, $2, $3, $4, 'Other account', $5, '{}', '{}')""",
        "222222222222", REGION, elsewhere["inferenceProfileId"],
        elsewhere["inferenceProfileArn"], MODEL)
    ids = await live_pull._identifiers(ACCOUNT, REGION, MODEL)
    assert ids == [MODEL, a["inferenceProfileArn"], a["inferenceProfileId"],
                   b["inferenceProfileArn"], b["inferenceProfileId"]]
    assert await live_pull._identifiers(ACCOUNT, "eu-west-1", MODEL) == [MODEL]


async def test_traffic_seen_only_through_a_profile_still_counts_as_known(conn, invoke):
    a = profile()
    await catalog(conn, a)
    await daily(conn, a["inferenceProfileArn"], 5)
    out = await live_pull.live_pull(request())
    assert out["has_application_profile"] is True
    assert invoke.call_args.args[1]["identifiers"][:2] == [MODEL, a["inferenceProfileArn"]]


async def test_mantle_only_usage_is_not_known_to_the_runtime_pull(conn, invoke):
    await daily(conn, MODEL, 5, endpoint="mantle")
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(request())
    assert e.value.status_code == 422
    invoke.assert_not_called()


async def test_another_accounts_usage_does_not_authorise_this_account(conn, invoke):
    await daily(conn, MODEL, 5, account="222222222222")
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(request())
    assert e.value.status_code == 422
    await daily(conn, MODEL, 5)
    assert (await live_pull.live_pull(request()))["ok"] is True
