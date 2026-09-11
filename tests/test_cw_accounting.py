"""Regression tests for CloudWatch request/error/throttle accounting.

the audit audit finding 01. The authoritative AWS definitions
(https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html):

  Invocations             "Number of SUCCESSFUL requests to Converse/
                           ConverseStream/InvokeModel/InvokeModelWithResponseStream"
  InvocationClientErrors  "Number of invocations that result in client-side errors"
  InvocationServerErrors  "Number of invocations that result in AWS server-side errors"
  InvocationThrottles     "Number of invocations that the system throttled.
                           THROTTLED REQUESTS AND OTHER INVOCATION ERRORS DON'T
                           COUNT AS EITHER Invocations OR Errors."

Therefore the four counters are mutually exclusive populations and:

    attempts  = Invocations + ClientErrors + ServerErrors + Throttles
    successes = Invocations                      (never derived by subtraction)
    failures  = ClientErrors + ServerErrors      (throttles tracked separately)

These tests drive the REAL producer (`ingestion.cw_metrics`) with controlled
CloudWatch responses and a recording connection stand-in, and assert against
independently hand-computed expectations (not by re-running producer helpers).

Run: .venv/bin/python -m pytest tests/test_cw_accounting.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from ingestion import cw_metrics  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class RecordingConn:
    """asyncpg-connection stand-in that records executemany payloads by table."""

    def __init__(self):
        self.writes: dict[str, list[tuple]] = {}
        self.executed: list[str] = []

    async def executemany(self, sql: str, rows):
        table = _table_of(sql)
        self.writes.setdefault(table, []).extend(rows)

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "OK"

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        return None


def _table_of(sql: str) -> str:
    upper = sql.upper()
    i = upper.find("INSERT INTO ")
    if i < 0:
        return "other"
    return sql[i + len("INSERT INTO "):].split()[0].strip()


class FakeCW:
    """Minimal get_metric_data stub. `series` maps metric name -> value, applied
    to every query id whose metric matches, at one fixed timestamp."""

    def __init__(self, series: dict[str, float], ts: datetime):
        self.series = series
        self.ts = ts
        self.calls = 0

    def get_metric_data(self, **kwargs):
        self.calls += 1
        results = []
        for q in kwargs["MetricDataQueries"]:
            metric = q["MetricStat"]["Metric"]["MetricName"]
            if metric in self.series:
                results.append({
                    "Id": q["Id"],
                    "Timestamps": [self.ts],
                    "Values": [float(self.series[metric])],
                })
            else:
                results.append({"Id": q["Id"], "Timestamps": [], "Values": []})
        return {"MetricDataResults": results}


TS = datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc)
MODELS = [("anthropic.claude-sonnet-4-5-20250929-v1:0", None)]
START = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)


async def _run(series: dict[str, float], monkeypatch=None) -> RecordingConn:
    """Drive the real producer, patching only its two external seams:
    the CloudWatch client factory and model discovery."""
    conn = RecordingConn()
    cw = FakeCW(series, TS)
    orig_client, orig_models = cw_metrics._cw_client, cw_metrics._list_models
    cw_metrics._cw_client = lambda region, session=None: cw
    cw_metrics._list_models = lambda _cw: MODELS
    try:
        await cw_metrics._ingest_region(
            conn, "111111111111", "us-east-1", START, END,
        )
    finally:
        cw_metrics._cw_client, cw_metrics._list_models = orig_client, orig_models
    return conn


def _daily(conn: RecordingConn) -> tuple | None:
    rows = conn.writes.get("f_daily", [])
    return rows[0] if rows else None


# Positional indices into the f_daily INSERT tuple (see cw_metrics daily_rows).
D_TOTAL, D_SUCCESS, D_FAILED = 9, 10, 11
D_400, D_403, D_429, D_500 = 16, 17, 18, 19


# --------------------------------------------------------------------------- #
# Finding 01 — the three cases from the audit, with independent expectations
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_successes_and_throttles_are_distinct_populations():
    """100 successful invocations + 20 throttles, no other errors.

    Hand-computed: successes=100, failures=0, throttles=20, attempts=120.
    The bug reported total=100/throttles=0 (throttles clamped to 4xx=0)."""
    conn = await _run({"Invocations": 100, "InvocationThrottles": 20})
    row = _daily(conn)
    assert row is not None, "a day with successes must be stored"
    assert row[D_SUCCESS] == 100
    assert row[D_FAILED] == 0
    assert row[D_429] == 20, "throttles are NOT a subset of InvocationClientErrors"
    assert row[D_TOTAL] == 120, "attempts = successes + errors + throttles"


@pytest.mark.asyncio
async def test_errors_are_not_subtracted_from_successes():
    """100 successes + 5 client errors + 10 server errors.

    Hand-computed: successes=100 (Invocations is already success-only),
    failures=15, attempts=115 → error rate 15/115 = 13.04%.
    The bug reported success=85 (double-subtracting) and 15/100 = 15%."""
    conn = await _run({
        "Invocations": 100,
        "InvocationClientErrors": 5,
        "InvocationServerErrors": 10,
    })
    row = _daily(conn)
    assert row is not None
    assert row[D_SUCCESS] == 100
    assert row[D_FAILED] == 15
    assert row[D_TOTAL] == 115
    assert row[D_400] == 5, "non-throttle 4xx == ClientErrors when throttles are 0"
    assert row[D_500] == 10


@pytest.mark.asyncio
async def test_fully_throttled_interval_is_still_recorded():
    """0 successes + 20 throttles must remain visible.

    The bug skipped the row entirely (`if total <= 0: continue`), hiding a
    completely throttled interval — the exact case an operator most needs."""
    conn = await _run({"Invocations": 0, "InvocationThrottles": 20})
    row = _daily(conn)
    assert row is not None, "a throttle-only interval must not disappear"
    assert row[D_SUCCESS] == 0
    assert row[D_429] == 20
    assert row[D_TOTAL] == 20


@pytest.mark.asyncio
async def test_error_only_interval_is_still_recorded():
    """0 successes + 7 server errors must remain visible."""
    conn = await _run({"Invocations": 0, "InvocationServerErrors": 7})
    row = _daily(conn)
    assert row is not None
    assert row[D_SUCCESS] == 0
    assert row[D_FAILED] == 7
    assert row[D_500] == 7
    assert row[D_TOTAL] == 7


@pytest.mark.asyncio
async def test_throttles_exceeding_client_errors_are_preserved():
    """Throttles > ClientErrors is legal (disjoint counters) and must not clamp.

    50 successes, 3 client errors, 40 throttles:
    non-throttle 4xx = 3 (NOT max(0, 3-40) = 0), throttles = 40."""
    conn = await _run({
        "Invocations": 50,
        "InvocationClientErrors": 3,
        "InvocationThrottles": 40,
    })
    row = _daily(conn)
    assert row is not None
    assert row[D_400] == 3, "ClientErrors already excludes throttles; do not subtract"
    assert row[D_429] == 40
    assert row[D_SUCCESS] == 50
    assert row[D_FAILED] == 3
    assert row[D_TOTAL] == 93


@pytest.mark.asyncio
async def test_empty_interval_writes_nothing():
    """All counters zero/absent → no row (avoids materializing empty days)."""
    conn = await _run({})
    assert _daily(conn) is None


# --------------------------------------------------------------------------- #
# Invariants that must hold for every stored row
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("series", [
    {"Invocations": 100, "InvocationThrottles": 20},
    {"Invocations": 100, "InvocationClientErrors": 5, "InvocationServerErrors": 10},
    {"Invocations": 0, "InvocationThrottles": 20},
    {"Invocations": 50, "InvocationClientErrors": 3, "InvocationThrottles": 40},
    {"Invocations": 7},
])
async def test_daily_row_invariants(series):
    conn = await _run(series)
    row = _daily(conn)
    if row is None:
        return
    total, success, failed = row[D_TOTAL], row[D_SUCCESS], row[D_FAILED]
    throttles = row[D_429]
    assert success >= 0 and failed >= 0 and throttles >= 0
    assert 0 <= failed <= total
    assert success + failed + throttles == total, (
        "attempts must decompose exactly into successes + failures + throttles")


# --------------------------------------------------------------------------- #
# Finding 01 (hourly path) — f_hourly_peak / f_hourly_errors must use the same
# disjoint-counter semantics as f_daily.
# --------------------------------------------------------------------------- #
def _hourly_peak(conn: RecordingConn) -> tuple | None:
    rows = conn.writes.get("f_hourly_peak", [])
    return rows[0] if rows else None


def _hourly_err(conn: RecordingConn) -> tuple | None:
    rows = conn.writes.get("f_hourly_errors", [])
    return rows[0] if rows else None


# f_hourly_peak tuple: (date, hour, acct, model, region, endpoint, total, ...,
#                       est_tpm, status_429) → total at 6, throttles last.
HP_TOTAL, HP_429 = 6, 12
# f_hourly_errors tuple: (date, hour, acct, model, region, endpoint, total,
#                         failed, 400, 403, 429, 500, 503)
HE_TOTAL, HE_FAILED, HE_400, HE_429, HE_500 = 6, 7, 8, 10, 11


@pytest.mark.asyncio
async def test_hourly_peak_counts_attempts_and_keeps_throttles():
    """100 successes + 20 throttles in one hour → attempts 120, throttles 20."""
    conn = await _run({"Invocations": 100, "InvocationThrottles": 20})
    row = _hourly_peak(conn)
    assert row is not None
    assert row[HP_TOTAL] == 120
    assert row[HP_429] == 20


@pytest.mark.asyncio
async def test_hourly_throttle_only_hour_is_visible_in_both_tables():
    """A 100%-throttled hour must appear in peak AND errors tables."""
    conn = await _run({"Invocations": 0, "InvocationThrottles": 20})
    peak = _hourly_peak(conn)
    err = _hourly_err(conn)
    assert peak is not None, "throttle-only hour must reach f_hourly_peak"
    assert peak[HP_TOTAL] == 20 and peak[HP_429] == 20
    assert err is not None, "throttle-only hour must reach f_hourly_errors"
    assert err[HE_429] == 20
    assert err[HE_TOTAL] == 20


@pytest.mark.asyncio
async def test_hourly_throttles_not_clamped_to_client_errors():
    """50 successes, 3 4xx, 40 throttles → 4xx stays 3, throttles stay 40."""
    conn = await _run({
        "Invocations": 50,
        "InvocationClientErrors": 3,
        "InvocationServerErrors": 0,
        "InvocationThrottles": 40,
    })
    peak = _hourly_peak(conn)
    err = _hourly_err(conn)
    assert peak[HP_429] == 40
    assert peak[HP_TOTAL] == 93
    assert err[HE_400] == 3
    assert err[HE_429] == 40
    assert err[HE_FAILED] == 3, "failures exclude throttles"


@pytest.mark.asyncio
async def test_daily_and_hourly_totals_agree():
    """Same controlled series must yield the same attempt count in both grains
    (one hourly datapoint in the fixture, so they must match exactly)."""
    series = {"Invocations": 100, "InvocationClientErrors": 5,
              "InvocationServerErrors": 10, "InvocationThrottles": 20}
    conn = await _run(series)
    daily, peak = _daily(conn), _hourly_peak(conn)
    assert daily[D_TOTAL] == peak[HP_TOTAL] == 135
    assert daily[D_429] == peak[HP_429] == 20


# --------------------------------------------------------------------------- #
# Finding 15 — context-window variants must not collide with the aggregate row.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_context_variants_do_not_collide_with_aggregate():
    """CloudWatch reports (ModelId, ContextWindow) pairs. f_daily's key has no
    context column, so emitting one row per variant let the last writer replace
    the aggregate. Exactly one canonical row per (date, model) must be written,
    carrying the aggregate value."""
    model = "anthropic.claude-sonnet-4-5-20250929-v1:0"
    discovered = [(model, None), (model, "18k"), (model, "51k"), (model, "200k")]

    conn = RecordingConn()
    cw = FakeCW({"Invocations": 500, "InputTokenCount": 500}, TS)
    orig_client, orig_models = cw_metrics._cw_client, cw_metrics._list_models
    cw_metrics._cw_client = lambda region, session=None: cw
    cw_metrics._list_models = lambda _cw: discovered
    try:
        await cw_metrics._ingest_region(
            conn, "111111111111", "us-east-1", START, END)
    finally:
        cw_metrics._cw_client, cw_metrics._list_models = orig_client, orig_models

    daily = conn.writes.get("f_daily", [])
    assert len(daily) == 1, (
        f"expected ONE canonical row per (date, model); got {len(daily)} "
        "colliding rows")
    assert daily[0][D_SUCCESS] == 500, "the aggregate must survive, not a variant"
    # And the same for the hourly grain.
    peak = conn.writes.get("f_hourly_peak", [])
    assert len(peak) == 1, f"expected ONE hourly row; got {len(peak)}"


# --------------------------------------------------------------------------- #
# Finding 04 — latency must keep account identity.
# --------------------------------------------------------------------------- #
# f_latency_daily tuple: (date, acct, model, traffic, region, endpoint,
#                         sample_count, ttft_sample_count, avg_e2e, ...)
L_ACCT, L_SAMPLES, L_TTFT_N, L_AVG_E2E = 1, 6, 7, 8


def _latency(conn: RecordingConn) -> list[tuple]:
    return conn.writes.get("f_latency_daily", [])


@pytest.mark.asyncio
async def test_latency_rows_carry_the_account_id():
    conn = await _run({"Invocations": 10, "InvocationLatency": 250})
    rows = _latency(conn)
    assert rows, "latency row expected"
    assert rows[0][L_ACCT] == "111111111111", (
        "the account must be stored, or per-account upserts overwrite each other")


@pytest.mark.asyncio
async def test_two_accounts_produce_distinct_latency_keys():
    """The audit's case: account A (100 samples @10ms) and account B
    (900 samples @1,000ms) previously collided on an account-free key, leaving
    only B — while the true combined mean is 901ms. Both rows must now exist
    with their own conflict keys so the reader can aggregate them correctly."""
    async def run_for(acct, samples, avg_ms):
        conn = RecordingConn()
        cw = FakeCW({"Invocations": samples, "InvocationLatency": avg_ms}, TS)
        orig_c, orig_m = cw_metrics._cw_client, cw_metrics._list_models
        cw_metrics._cw_client = lambda region, session=None: cw
        cw_metrics._list_models = lambda _cw: MODELS
        try:
            await cw_metrics._ingest_region(conn, acct, "us-east-1", START, END)
        finally:
            cw_metrics._cw_client, cw_metrics._list_models = orig_c, orig_m
        return _latency(conn)[0]

    a = await run_for("111111111111", 100, 10)
    b = await run_for("222222222222", 900, 1000)
    key = lambda r: (r[0], r[1], r[2], r[3], r[4], r[5])   # incl. account
    assert key(a) != key(b), "distinct accounts must not share a conflict key"
    # Independent oracle for the combined mean the reader should produce:
    combined = (100 * 10 + 900 * 1000) / (100 + 900)
    assert combined == 901.0


@pytest.mark.asyncio
async def test_ttft_has_its_own_sample_count():
    """TTFT is streaming-only, so its population differs from E2E's. 1,000 E2E
    samples with only 100 TTFT samples must store 100, not 1,000."""
    conn = RecordingConn()
    cw = FakeCW({}, TS)
    # Distinct SampleCount per metric: InvocationLatency=1000, TimeToFirstToken=100
    def per_metric(**kwargs):
        results = []
        for q in kwargs["MetricDataQueries"]:
            m = q["MetricStat"]["Metric"]["MetricName"]
            stat = q["MetricStat"]["Stat"]
            val = None
            if m == "Invocations":
                val = 1000
            elif m == "InvocationLatency":
                val = 1000 if stat == "SampleCount" else 500
            elif m == "TimeToFirstToken":
                val = 100 if stat == "SampleCount" else 200
            if val is None:
                results.append({"Id": q["Id"], "Timestamps": [], "Values": []})
            else:
                results.append({"Id": q["Id"], "Timestamps": [TS], "Values": [float(val)]})
        return {"MetricDataResults": results}
    cw.get_metric_data = per_metric

    orig_c, orig_m = cw_metrics._cw_client, cw_metrics._list_models
    cw_metrics._cw_client = lambda region, session=None: cw
    cw_metrics._list_models = lambda _cw: MODELS
    try:
        await cw_metrics._ingest_region(conn, "111111111111", "us-east-1", START, END)
    finally:
        cw_metrics._cw_client, cw_metrics._list_models = orig_c, orig_m

    row = _latency(conn)[0]
    assert row[L_SAMPLES] == 1000
    assert row[L_TTFT_N] == 100, "TTFT population must be its own count"
