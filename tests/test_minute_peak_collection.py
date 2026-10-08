"""Collector failure and completeness regressions; AWS and DB I/O are stubbed."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.rate_catalog import Catalog
from ingestion import cw_minute_peak as cp
from ingestion import inference_profiles as ip

ACCOUNT, REGION, MODEL = "111111111111", "us-east-1", "anthropic.claude-sonnet-5"
DAY = date(2026, 10, 1)
START = datetime.combine(DAY, datetime.min.time(), timezone.utc)


@pytest.fixture
def conn():
    c = SimpleNamespace(fetch=AsyncMock(return_value=[]), execute=AsyncMock(),
                        executemany=AsyncMock(), close=AsyncMock())

    @asynccontextmanager
    async def transaction():
        yield
    c.transaction = transaction
    return c


@pytest.fixture
def collection(monkeypatch, conn):
    monkeypatch.setattr(cp, "_cw", Mock())
    monkeypatch.setattr(cp, "list_model_ids", Mock(return_value=[MODEL]))
    monkeypatch.setattr(cp, "load_profile_map", AsyncMock(return_value={}))
    monkeypatch.setattr(cp, "minute_window", lambda days, now=None: [DAY])
    record = AsyncMock()
    upsert = AsyncMock(side_effect=lambda c, rows: len(rows))
    monkeypatch.setattr(cp, "_record_collection", record)
    monkeypatch.setattr(cp, "_upsert", upsert)
    return record, upsert


@pytest.mark.asyncio
async def test_partial_token_queries_never_replace_a_previous_peak(monkeypatch, conn, collection):
    record, upsert = collection
    monkeypatch.setattr(cp, "fetch_minutes", Mock(return_value=(
        {MODEL: {"Invocations": {START: 1}}},
        [f"{MODEL}/InputTokenCount:InternalError"])))
    result = await cp.collect_region(conn, ACCOUNT, REGION, 1, None, Catalog())
    assert result["status"] == "partial" and result["rows"] == 0
    upsert.assert_not_awaited()
    conn.execute.assert_not_awaited()  # no DELETE of last complete data
    saved = record.await_args.args[1]
    assert saved["last_success_at"] is None
    assert saved["series_complete"] == 4 and saved["status"] == "partial"


@pytest.mark.asyncio
async def test_all_failed_days_fail_the_region(monkeypatch, conn, collection):
    record, upsert = collection
    monkeypatch.setattr(cp, "fetch_minutes", Mock(side_effect=RuntimeError("unavailable")))
    result = await cp.collect_region(conn, ACCOUNT, REGION, 1, None, Catalog())
    assert result["status"] == "failed"
    assert record.await_args.args[1]["status"] == "failed"
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_failure_records_missing_coverage(monkeypatch, conn, collection):
    record, _ = collection
    monkeypatch.setattr(cp, "list_model_ids", Mock(side_effect=RuntimeError("denied")))
    result = await cp.collect_region(conn, ACCOUNT, REGION, 1, None, Catalog())
    assert result["status"] == "failed"
    saved = record.await_args.args[1]
    assert saved["event_date"] == DAY and saved["status"] == "failed"
    assert saved.get("last_success_at") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "failed"])
async def test_incomplete_region_produces_nonzero_job_exit(monkeypatch, conn, status):
    monkeypatch.setattr(cp, "discover_accounts",
                        Mock(return_value=[SimpleNamespace(accountId=ACCOUNT)]))
    monkeypatch.setattr(cp, "session_for", Mock())
    monkeypatch.setattr(cp.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(cp.rate_catalog, "snapshot_with_conn", AsyncMock(return_value=Catalog()))
    monkeypatch.setattr(cp, "collect_region", AsyncMock(return_value={"status": status, "rows": 0}))
    args = SimpleNamespace(regions=REGION, days=1, db_url="unused",
                           role_name="unused", external_id=None)
    assert await cp.run(args) == 1


def metric_page(status="Complete"):
    return {"MetricDataResults": [
        {"Id": f"m0_{i}", "StatusCode": status, "Timestamps": [], "Values": []}
        for i in range(len(cp.METRICS))
    ]}


def test_missing_result_is_incomplete_even_if_returned_results_are_complete():
    client = Mock()
    response = metric_page()
    response["MetricDataResults"].pop(1)
    client.get_metric_data.return_value = response
    _, incomplete = cp.fetch_minutes(client, [MODEL], START, START + timedelta(days=1))
    assert incomplete == [f"{MODEL}/InputTokenCount:missing result"]


def test_pagination_merges_samples_and_uses_final_complete_status():
    client = Mock()
    first, last = metric_page("PartialData"), metric_page()
    first["NextToken"] = "page2"
    first["MetricDataResults"][0].update(Timestamps=[START], Values=[2])
    last["MetricDataResults"][0].update(Timestamps=[START + timedelta(minutes=1)], Values=[3])
    client.get_metric_data.side_effect = [first, last]
    values, incomplete = cp.fetch_minutes(client, [MODEL], START, START + timedelta(days=1))
    assert incomplete == []
    assert list(values[MODEL]["Invocations"].values()) == [2, 3]


def test_repeated_pagination_token_cannot_hang_the_collector():
    client = Mock()
    client.get_metric_data.return_value = {**metric_page("PartialData"), "NextToken": "same"}
    with pytest.raises(ValueError, match="pagination token"):
        cp.fetch_minutes(client, [MODEL], START, START + timedelta(days=1))
    assert client.get_metric_data.call_count == 2


@pytest.mark.parametrize("value,offset", [(float("nan"), 0), (-1, 0), (10, 30), (10, 86400)])
def test_invalid_or_out_of_window_samples_are_not_peaks(value, offset):
    client = Mock()
    response = metric_page()
    response["MetricDataResults"][0].update(
        Timestamps=[START + timedelta(seconds=offset)], Values=[value])
    client.get_metric_data.return_value = response
    _, incomplete = cp.fetch_minutes(client, [MODEL], START, START + timedelta(days=1))
    assert incomplete == [f"{MODEL}/Invocations:invalid datapoints"]


def test_request_count_alone_cannot_establish_zero_token_usage():
    peak = cp.reduce_day([MODEL], {MODEL: {"Invocations": {START: 1}}}, 10)
    assert peak["peak_rpm"] == 1
    assert peak["peak_quota_tpm"] is None
    assert peak["quota_tpm_source"] == "unavailable"
    assert peak["peak_input_tpm"] is None and peak["peak_output_tpm"] is None


def test_unknown_token_usage_from_one_profile_invalidates_combined_tpm():
    peak = cp.reduce_day(["direct", "profile"], {
        "direct": {"EstimatedTPMQuotaUsage": {START: 100}},
        "profile": {"Invocations": {START: 1}},
    }, 10)
    assert peak["peak_quota_tpm"] is None, "100 would omit the active profile's tokens"
    assert peak["peak_rpm"] is None, "the direct source did not report its request count"


@pytest.mark.asyncio
async def test_collector_uses_configured_multiplier_on_each_effective_date(
        monkeypatch, conn, collection):
    _, upsert = collection
    monkeypatch.setattr(cp, "minute_window", lambda days, now=None: [DAY, DAY + timedelta(days=1)])
    monkeypatch.setattr(cp, "fetch_minutes", Mock(side_effect=lambda cw, ids, start, end: (
        {MODEL: {"OutputTokenCount": {start: 10}}}, [])))
    catalog = Catalog(entries=(
        {"all_of": ["sonnet5"], "rate": 10},
        {"all_of": ["sonnet5"], "rate": 15, "effective_from": "2026-10-02"},
    ))
    result = await cp.collect_region(conn, ACCOUNT, REGION, 2, None, catalog)
    assert result["status"] == "complete" and result["rows"] == 2
    rows = [call.args[1][0] for call in upsert.await_args_list]
    assert [(r[0], r[9], r[12], r[13]) for r in rows] == [
        (DAY, 100, 10, "catalog"), (DAY + timedelta(days=1), 150, 15, "catalog")]
    assert all("DELETE FROM public.f_minute_peak" in call.args[0]
               for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_late_mapping_invalidates_both_raw_and_direct_historical_peaks(conn):
    old_day = DAY - timedelta(days=30)  # raw minutes can no longer be reconstructed
    conn.fetch.return_value = [
        {"event_date": old_day, "modelid": "abcdef123456", "source_ids": ["abcdef123456"],
         "quota_tpm_source": "aws_estimate", "burndown_rate": 10},
        {"event_date": old_day, "modelid": MODEL, "source_ids": [MODEL],
         "quota_tpm_source": "aws_estimate", "burndown_rate": 10},
    ]
    await cp.invalidate_changed_peaks(conn, ACCOUNT, REGION, {"abcdef123456": MODEL}, Catalog())
    query, parameters = conn.executemany.await_args.args
    assert parameters == [(old_day, ACCOUNT, REGION)]
    assert "modelid=" not in query.lower(), "the direct model also needs recombination"


@pytest.mark.asyncio
async def test_rate_edits_invalidate_reconstructions_but_not_native_estimates(conn):
    conn.fetch.return_value = [
        {"event_date": DAY, "modelid": MODEL, "source_ids": [MODEL],
         "quota_tpm_source": "reconstructed", "burndown_rate": 5},
        {"event_date": DAY + timedelta(days=1), "modelid": MODEL, "source_ids": [MODEL],
         "quota_tpm_source": "aws_estimate", "burndown_rate": 5},
    ]
    await cp.invalidate_changed_peaks(conn, ACCOUNT, REGION, {}, Catalog())
    assert conn.executemany.await_args.args[1] == [(DAY, ACCOUNT, REGION)]


def test_descriptive_model_ids_do_not_trigger_profile_gets():
    client = Mock()
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    rows, warnings = ip.read_profiles(client, ACCOUNT, REGION,
                                      {"claude-sonnet-5", "gpt-oss-120b", "us." + MODEL})
    assert rows == [] and warnings == []
    client.get_inference_profile.assert_not_called()


@pytest.mark.asyncio
async def test_inactive_sources_are_requeried_before_replacing_a_day(monkeypatch, conn, collection):
    conn.fetch.side_effect = [[], [{"source_id": "abcdef123456"}]]
    monkeypatch.setattr(cp, "load_profile_map", AsyncMock(return_value={"abcdef123456": MODEL}))
    fetch = Mock(return_value=({}, []))
    monkeypatch.setattr(cp, "fetch_minutes", fetch)
    result = await cp.collect_region(conn, ACCOUNT, REGION, 1, None, Catalog())
    assert result["status"] == "complete"
    assert set(fetch.call_args.args[1]) == {MODEL, "abcdef123456"}


@pytest.mark.asyncio
async def test_system_profile_or_descriptive_model_is_not_counted_as_unresolved(conn):
    system_arn = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/us.{MODEL}"
    conn.fetch.side_effect = [
        [{"modelid": system_arn}, {"modelid": "claude-sonnet-5"}], []]
    client = Mock()
    client.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    client.get_inference_profile.return_value = {"type": "SYSTEM_DEFINED"}
    result = await ip.refresh_region(conn, client, ACCOUNT, REGION)
    assert result["unresolved_observed"] == 0
    assert result["warnings"] == []
    client.get_inference_profile.assert_called_once_with(inferenceProfileIdentifier=system_arn)
