"""POST /api/live-pull contracts with deterministic DB and function responses.

Run: python -m pytest tests/test_live_pull_api.py -q

No database or AWS access: db and the function invoke are mocked. The real SQL
runs against PostgreSQL in test_live_pull_db.py.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock
import sys

from fastapi import HTTPException
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from app import db, rate_catalog  # noqa: E402
from app.routers import live_pull  # noqa: E402

ROUTER = (ROOT / "backend/app/routers/live_pull.py").read_text()

ACCOUNT, REGION = "111111111111", "us-east-1"
MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/abcdef123456"
OK = {"ok": True, "status": "complete", "minutes": [], "peak": {}}


def body(**extra):
    return live_pull.LivePullRequest(**{"account_id": ACCOUNT, "region": REGION,
                                        "model_id": MODEL, "hours": 3, **extra})


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("LIVE_PULL_FUNCTION_NAME", "lens-live-pull")
    monkeypatch.setattr(live_pull, "_recent", {})
    monkeypatch.setattr(db, "fetchval", AsyncMock(return_value=1))
    monkeypatch.setattr(db, "fetch", AsyncMock(return_value=[
        {"profile_arn": ARN, "profile_id": "abcdef123456"}]))
    monkeypatch.setattr(rate_catalog, "snapshot",
                        AsyncMock(return_value=rate_catalog.Catalog()))
    invoke = Mock(return_value=dict(OK))
    monkeypatch.setattr(live_pull, "_invoke", invoke)
    return invoke


async def test_config_reports_whether_live_pull_is_deployed(monkeypatch):
    monkeypatch.delenv("LIVE_PULL_FUNCTION_NAME", raising=False)
    assert (await live_pull.live_pull_config())["enabled"] is False
    monkeypatch.setenv("LIVE_PULL_FUNCTION_NAME", "lens-live-pull")
    cfg = await live_pull.live_pull_config()
    assert cfg["enabled"] is True and cfg["hours"] == [1, 3, 6, 12, 24]


async def test_unconfigured_deployment_answers_501_without_touching_the_db(monkeypatch):
    monkeypatch.delenv("LIVE_PULL_FUNCTION_NAME", raising=False)
    fetchval = AsyncMock()
    monkeypatch.setattr(db, "fetchval", fetchval)
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(body())
    assert e.value.status_code == 501
    fetchval.assert_not_awaited()


@pytest.mark.parametrize("bad", [
    {"account_id": "1234"}, {"account_id": "\u0661" * 12}, {"region": "Mars"},
    {"model_id": "a b"},
    {"hours": 2}, {"endpoint": "mantle"},
])
async def test_invalid_requests_are_400_before_any_lookup(api, bad):
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(body(**bad))
    assert e.value.status_code == 400
    db.fetchval.assert_not_awaited()
    api.assert_not_called()


async def test_an_account_model_region_lens_does_not_report_is_refused(api, monkeypatch):
    """422, never 404: the CloudFront distribution serves index.html for 404s."""
    monkeypatch.setattr(db, "fetchval", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(body())
    assert e.value.status_code == 422
    api.assert_not_called()
    query, *params = db.fetchval.await_args.args
    assert "endpoint = 'runtime'" in query and params == [ACCOUNT, REGION, MODEL]


async def test_the_function_gets_the_model_its_profiles_and_the_catalog_rate(api):
    out = await live_pull.live_pull(body(hours=6))
    name, payload = api.call_args.args
    assert name == "lens-live-pull"
    assert payload == {"account_id": ACCOUNT, "region": REGION, "model_id": MODEL,
                       "identifiers": [MODEL, ARN, "abcdef123456"], "hours": 6,
                       "rate": 5, "rate_source": "bundled_default"}
    assert out["has_application_profile"] is True and out["cached"] is False


async def test_a_model_without_profiles_pulls_only_itself(api, monkeypatch):
    monkeypatch.setattr(db, "fetch", AsyncMock(return_value=[]))
    out = await live_pull.live_pull(body(model_id="amazon.nova-pro-v1:0"))
    payload = api.call_args.args[1]
    assert payload["identifiers"] == ["amazon.nova-pro-v1:0"] and payload["rate"] == 1
    assert out["has_application_profile"] is False


async def test_all_profile_forms_are_sent_including_the_twelfth_short_id(api, monkeypatch):
    profiles = [{"profile_id": f"p{i:011d}",
                 "profile_arn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/p{i:011d}"}
                for i in range(12)]
    monkeypatch.setattr(db, "fetch", AsyncMock(return_value=profiles))
    await live_pull.live_pull(body())
    sent = api.call_args.args[1]["identifiers"]
    assert len(sent) == 25 and sent[-1] == profiles[-1]["profile_id"]


async def test_an_oversized_model_group_is_refused_without_pulling_a_subset(api, monkeypatch):
    profiles = [{"profile_id": f"p{i:011d}"} for i in range(live_pull.MAX_IDENTIFIERS)]
    monkeypatch.setattr(db, "fetch", AsyncMock(return_value=profiles))
    with pytest.raises(HTTPException) as exc:
        await live_pull.live_pull(body())
    assert exc.value.status_code == 422
    api.assert_not_called()
    from ingestion.live_pull import MAX_IDENTIFIERS
    assert live_pull.MAX_IDENTIFIERS == MAX_IDENTIFIERS


async def test_a_repeat_within_the_cooldown_reuses_the_result(api):
    first = await live_pull.live_pull(body())
    again = await live_pull.live_pull(body())
    assert api.call_count == 1
    assert again["cached"] is True and first["cached"] is False
    await live_pull.live_pull(body(hours=1))  # a different window is a new pull
    assert api.call_count == 2


async def test_an_expired_memo_pulls_again(api, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(live_pull.time, "monotonic", lambda: clock[0])
    await live_pull.live_pull(body())
    clock[0] += live_pull.COOLDOWN_SECONDS
    await live_pull.live_pull(body())
    assert api.call_count == 2
    assert len(live_pull._recent) == 1


@pytest.mark.parametrize("error,status", [
    ("invalid_request", 400), ("no_access", 403), ("throttled", 429),
    ("failed", 502), (None, 502),
])
async def test_function_errors_map_to_http_statuses_and_are_not_memoised(api, error, status):
    api.return_value = {"ok": False, "error": error, "detail": "Safe message."}
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(body())
    assert e.value.status_code == status and e.value.detail == "Safe message."
    assert live_pull._recent == {}


async def test_an_unreachable_function_is_502_without_a_trace(api):
    api.side_effect = RuntimeError("endpoint URL and credentials")
    with pytest.raises(HTTPException) as e:
        await live_pull.live_pull(body())
    assert e.value.status_code == 502
    assert "credentials" not in e.value.detail


def test_invoke_reports_a_function_error_without_its_payload(monkeypatch):
    import boto3
    payload = Mock()
    payload.read.return_value = b'{"errorMessage": "Traceback ... secret", "errorType": "KeyError"}'
    client = Mock()
    client.invoke.return_value = {"FunctionError": "Unhandled", "Payload": payload}
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    out = live_pull._invoke("lens-live-pull", {"hours": 3})
    assert out == {"ok": False, "error": "failed", "detail": "The live-pull function failed."}
    assert client.invoke.call_args.kwargs["FunctionName"] == "lens-live-pull"


def test_no_response_from_this_router_can_be_a_404():
    """Every 404 reaching CloudFront is replaced by the SPA page with a 200."""
    assert "HTTPException(404" not in ROUTER
