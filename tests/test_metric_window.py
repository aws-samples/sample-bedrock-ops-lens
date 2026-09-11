"""CloudWatch bucket alignment — found during real-data integration testing.

Every CloudWatch ingester computed its window as `start = now() - days`, which is
not aligned to anything. CloudWatch aligns GetMetricData buckets to the requested
StartTime, so the bucket boundaries moved with the MINUTE the ingester ran, and
the loader then stored each bucket under `ts.hour` / `ts.date()`.

Measured directly against AWS/Bedrock (us-east-1, Claude Haiku 4.5, real
traffic), same metric, same two-day span, only the request start minute changed:

    request start 03:00  ->  bucket 2026-09-09T16:00 =   236,840
    request start 03:37  ->  bucket 2026-09-09T16:37 =   496,302
    Period=86400, 03:00  ->  4,556,604
    Period=86400, 03:37  ->  4,752,045          (4.3% apart)

Consequences, all confirmed on the deployed stack: an hourly peak was attributed
to the wrong hour, the stored value for "hour 16" depended on scrape time,
re-running the ingester rewrote history, and a reconciliation of stored values
against CloudWatch disagreed on 140 of 163 overlapping hours (the dashboard
reported a peak of 11,011 TPM where CloudWatch's hour-aligned peak was 10,396).

Flooring START to UTC midnight fixes both grains at once, because midnight is
also an hour boundary. END stays live so the current partial hour is not thrown
away — alignment depends only on StartTime.

Run: .venv/bin/python -m pytest tests/test_metric_window.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.accounts import metric_window  # noqa: E402

INGESTERS = ["ingestion/cw_metrics.py", "ingestion/cw_mantle_metrics.py",
             "ingestion/cw_agentcore.py", "ingestion/cw_guardrails.py"]


@pytest.mark.parametrize("minute,second", [(0, 0), (37, 55), (59, 59), (1, 3)])
def test_start_is_always_utc_midnight(minute, second):
    """Whatever minute the job runs at, the window starts at a day boundary."""
    now = datetime(2026, 9, 11, 3, minute, second, 123456, tzinfo=timezone.utc)
    start, _ = metric_window(30, now)
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)


def test_start_is_also_an_hour_boundary():
    """Midnight satisfies the 3600s grain too — that is why one floor is enough."""
    start, _ = metric_window(7, datetime(2026, 9, 11, 3, 37, 55, tzinfo=timezone.utc))
    assert start.minute == 0 and start.second == 0


def test_the_window_is_scrape_time_independent():
    """The core defect: two runs in the same hour must ask for the same buckets,
    or the stored numbers change without the traffic changing."""
    day = datetime(2026, 9, 11, tzinfo=timezone.utc)
    starts = {metric_window(30, day + timedelta(hours=h, minutes=m))[0]
              for h in (0, 3, 13, 23) for m in (0, 17, 37, 59)}
    assert len(starts) == 1, f"window start still moves with scrape time: {starts}"


def test_end_stays_live_so_the_current_partial_bucket_is_not_discarded():
    now = datetime(2026, 9, 11, 3, 37, 55, tzinfo=timezone.utc)
    _, end = metric_window(30, now)
    assert end == now, (
        "flooring END would drop the freshest hour; alignment only depends on "
        "StartTime, so there is no reason to pay that cost")


def test_the_requested_span_still_covers_the_asked_for_days():
    now = datetime(2026, 9, 11, 3, 37, 55, tzinfo=timezone.utc)
    start, end = metric_window(30, now)
    span_days = (end - start).total_seconds() / 86400
    # Flooring start can only widen the window, never narrow it below `days`.
    assert 30 <= span_days < 31


@pytest.mark.parametrize("rel", INGESTERS)
def test_every_cloudwatch_ingester_uses_the_shared_window(rel):
    src = (ROOT / rel).read_text()
    assert "metric_window(" in src, f"{rel} must use the shared aligned window"


@pytest.mark.parametrize("rel", INGESTERS)
def test_no_ingester_recomputes_an_unaligned_window(rel):
    """The old two-liner is easy to reintroduce by copy-paste."""
    src = (ROOT / rel).read_text()
    assert "start = end - timedelta(days=args.days)" not in src, (
        f"{rel} reintroduced the unaligned window")


def test_the_reason_is_recorded_where_someone_will_change_it():
    """A helper that just floors a datetime looks pointlessly defensive."""
    src = (ROOT / "ingestion/accounts.py").read_text()
    block = src.split("def metric_window")[1][:2600]
    assert "StartTime" in block
    assert "4,556,604" in block or "4556604" in block, (
        "keep the measured counterexample; it is what justifies the helper")
