"""Minute API contracts with deterministic DB responses (no database required)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from app import db, rate_catalog
from app.filters import FilterSet
from app.routers import ops_insights

DAY = date(2026, 10, 1)
STAMP = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
ACCOUNT, REGION, MODEL = "111111111111", "us-east-1", "anthropic.claude-sonnet-5"
KEY = dict(accountid=ACCOUNT, modelid=MODEL, region=REGION, endpoint="runtime")


def hourly(**extra):
    return {**KEY, "event_date": DAY, "has_application_profile": False,
            "total_requests": 60, "input_quota_tokens": 600,
            "total_output_tokens": 60, "estimated_tpm_quota_usage": 6000, **extra}


def minute(**extra):
    return {**KEY, "peak_rpm": 12, "peak_input_tpm": 1000,
            "peak_output_tpm": 100, "peak_quota_tpm": 2000,
            "active_minutes": 10, "resolution_stale": False, "days_with_minute_data": 1,
            "has_application_profile": False, "measured_dates": [DAY],
            "rpm_measurement_complete": True, "input_measurement_complete": True,
            "output_measurement_complete": True, "quota_measurement_complete": True,
            "quota_tpm_source": "aws_estimate", "peak_quota_tpm_at": STAMP, **extra}


def coverage(**extra):
    return {**KEY, "days_attempted": 1, "days_complete": 1,
            "days_incomplete": 0, "last_success_at": STAMP,
            "collected_through": STAMP, "includes_open_day": False, **extra}


@pytest.fixture
def setup(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(rate_catalog, "snapshot", AsyncMock(return_value=rate_catalog.Catalog()))
    return fetch


@pytest.mark.asyncio
async def test_collection_query_is_scoped_to_selected_dates_account_region_and_endpoint(setup):
    setup.side_effect = [[hourly()], [minute()], [coverage()]]
    f = FilterSet(start=DAY, end=DAY, accounts={ACCOUNT}, region=REGION, endpoint="runtime")
    [row] = await ops_insights.ops_peak_rpm(f)
    query, *params = setup.await_args_list[2].args
    assert "WHERE event_date BETWEEN $1::date AND $2::date" in query
    assert params[:2] == [DAY, DAY]
    assert REGION in params and "runtime" in params and [ACCOUNT] in params
    assert row["minute_collection"]["days_expected"] == 1
    assert row["minute_coverage_complete"] is True
    assert row["peak_minute_estimated_quota_tpm"] == 2000


@pytest.mark.asyncio
async def test_partial_collection_exposes_observation_without_claiming_complete_coverage(setup):
    setup.side_effect = [[hourly()], [minute()],
                         [coverage(days_complete=0, days_incomplete=1)]]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=DAY))
    assert row["peak_minute_estimated_quota_tpm"] == 2000
    assert row["minute_coverage_status"] == "partial"
    assert row["minute_coverage_complete"] is False


@pytest.mark.asyncio
async def test_mapping_staleness_withholds_a_peak_even_with_complete_collection(setup):
    setup.side_effect = [[hourly()], [minute(resolution_stale=True)], [coverage()]]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=DAY))
    assert row["minute_resolution_stale"] is True
    assert row["measured_minute_available"] is False
    assert row["peak_minute_estimated_quota_tpm"] is None
    assert row["peak_minute_rpm"] is None
    assert row["minute_coverage_status"] == "stale"
    assert row["busiest_hour_avg_quota_tpm"] == 100


@pytest.mark.asyncio
async def test_no_collection_record_does_not_claim_full_window_coverage(setup):
    setup.side_effect = [[hourly()], [minute()], []]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=DAY))
    assert row["minute_coverage_complete"] is False
    assert row["minute_collection"]["days_missing"] == 1


@pytest.mark.asyncio
async def test_runtime_minute_data_never_populates_a_mantle_row(setup):
    setup.side_effect = [
        [hourly(), hourly(endpoint="mantle", estimated_tpm_quota_usage=None)],
        [minute()], [coverage()]]
    rows = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=DAY, endpoint="all"))
    assert len(rows) == 2
    runtime = next(r for r in rows if r["endpoint"] == "runtime")
    mantle = next(r for r in rows if r["endpoint"] == "mantle")
    assert runtime["peak_minute_estimated_quota_tpm"] == 2000
    assert mantle["peak_minute_estimated_quota_tpm"] is None
    assert mantle["measured_minute_available"] is False
    assert mantle["burndown_rate"] == 1


@pytest.mark.asyncio
async def test_hourly_reconstruction_also_uses_the_rate_for_each_observation_date(setup, monkeypatch):
    monkeypatch.setattr(rate_catalog, "snapshot", AsyncMock(return_value=rate_catalog.Catalog(
        entries=({"all_of": ["sonnet5"], "rate": 10},
                 {"all_of": ["sonnet5"], "rate": 15, "effective_from": "2026-10-02"}))))
    setup.side_effect = [
        [hourly(estimated_tpm_quota_usage=None, total_output_tokens=600, input_quota_tokens=0),
         hourly(event_date=date(2026, 10, 2), estimated_tpm_quota_usage=None,
                total_output_tokens=600, input_quota_tokens=0)],
        [], []]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=date(2026, 10, 2)))
    assert row["busiest_hour_avg_quota_tpm"] == 150  # 600 * 15 / 60
    assert row["burndown_rate"] == 15


async def test_successful_queries_do_not_hide_an_active_days_unknown_token_measurement(setup):
    setup.side_effect = [
        [hourly(estimated_tpm_quota_usage=60000)],
        [minute(peak_quota_tpm=100, quota_measurement_complete=False,
                measured_dates=[DAY, date(2026, 10, 2)], days_with_minute_data=2)],
        [coverage(days_attempted=2, days_complete=2)],
    ]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=date(2026, 10, 2)))
    assert row["peak_minute_estimated_quota_tpm"] == 100
    assert row["busiest_hour_avg_quota_tpm"] == 1000
    assert row["minute_quota_complete"] is False
    assert row["minute_rpm_complete"] is True
    assert row["minute_coverage_complete"] is False


@pytest.mark.parametrize("activity", [
    {},
    {"total_requests": 0, "estimated_tpm_quota_usage": None},
    {"total_requests": 0, "input_quota_tokens": 0, "total_output_tokens": 0},
])
async def test_an_active_hourly_day_missing_from_minute_rows_is_not_exact(setup, activity):
    setup.side_effect = [
        [hourly(), hourly(event_date=date(2026, 10, 2), **activity)],
        [minute()], [coverage(days_attempted=2, days_complete=2)],
    ]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=date(2026, 10, 2)))
    assert row["minute_rpm_complete"] is False
    assert row["minute_quota_complete"] is False


async def test_new_minute_profile_usage_also_makes_quota_routing_unknown(setup):
    setup.side_effect = [[hourly()], [minute(has_application_profile=True)], [coverage()]]
    [row] = await ops_insights.ops_peak_rpm(FilterSet(start=DAY, end=DAY))
    assert row["has_application_profile"] is True


@pytest.mark.parametrize("model,family,expected", [
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", "On-demand", 200000),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "Cross-region", 400000),
    ("global.anthropic.claude-sonnet-4-5-20250929-v1:0", None, None),
    ("anthropic.claude-sonnet-4-6", None, None),
])
async def test_peak_quota_matching_uses_exact_model_and_family(setup, model, family, expected):
    quotas = [
        dict(accountid=ACCOUNT, region=REGION, model_name="Anthropic Claude Haiku 4.5",
             metric="TPM", traffic_type="On-demand", applied_value=1000000),
        dict(accountid=ACCOUNT, region=REGION, model_name="Anthropic Claude Sonnet 4.5",
             metric="TPM", traffic_type="Cross-region", applied_value=400000),
        dict(accountid=ACCOUNT, region=REGION, model_name="Anthropic Claude Sonnet 4.5",
             metric="TPM", traffic_type="On-demand", applied_value=None, default_value=200000),
    ]
    setup.return_value = quotas
    rows = [dict(accountId=ACCOUNT, modelId=model, region=REGION,
                 endpoint="runtime", has_application_profile=False)]
    await ops_insights._attach_peak_quotas(rows)
    assert rows[0]["quota_tpm"]["limit_per_minute"] == expected
    if expected is not None:
        assert rows[0]["quota_tpm"]["quota_family"] == family


async def test_mantle_and_profile_rows_cannot_borrow_a_runtime_model_limit(setup):
    setup.return_value = [
        dict(accountid=ACCOUNT, region=REGION, model_name="Amazon Nova Pro",
             metric="TPM", traffic_type="On-demand", applied_value=100000),
    ]
    rows = [
        dict(accountId=ACCOUNT, modelId="amazon.nova-pro-v1:0", region=REGION,
             endpoint=ep, has_application_profile=profile)
        for ep, profile in [("runtime", True), ("mantle", False)]
    ]
    await ops_insights._attach_peak_quotas(rows)
    assert all(r["quota_tpm"]["limit_per_minute"] is None for r in rows)
    assert rows[0]["quota_tpm"]["quota_routing_unknown"] is True
