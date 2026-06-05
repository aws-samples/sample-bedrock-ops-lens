#!/usr/bin/env python3
"""
CloudWatch Metrics ingester for Bedrock Ops Lens.

Reads AWS/Bedrock metrics from one or more (account, region) targets and
populates the volumetric tables: f_daily, f_hourly_peak, f_hourly_errors,
f_latency_daily.

The CloudWatch Bedrock namespace only exposes (ModelId, ContextWindow) as
dimensions, so the higher-cardinality columns in f_daily (operation,
traffic_type, service_tier, inference_profile_prefix) are populated only
when invocation logs are also ingested. CW-Metrics-only rows use the
schema's '__none__' sentinel for those columns.

Usage:
    python -m ingestion.cw_metrics --accounts YOUR_ACCOUNT_ID --regions us-east-1 --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg
import boto3
from botocore.config import Config

from .accounts import (
    DEFAULT_EXTERNAL_ID,
    DEFAULT_ROLE_NAME,
    _add_common_args,
    discover_accounts,
    session_for,
)

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# Map CloudWatch metric names → schema columns. Stat per metric.
DAILY_METRICS = {
    "Invocations":              ("total_requests",                 "Sum"),
    "InvocationClientErrors":   ("failed_requests_4xx",            "Sum"),
    "InvocationServerErrors":   ("failed_requests_5xx",            "Sum"),
    "InputTokenCount":          ("total_input_tokens",             "Sum"),
    "OutputTokenCount":         ("total_output_tokens",            "Sum"),
    "CacheReadInputTokenCount": ("total_cache_read_input_tokens",  "Sum"),
    "CacheWriteInputTokenCount":("total_cache_write_input_tokens", "Sum"),
}

LATENCY_METRICS = [
    ("InvocationLatency", "p50_e2e",  "p50"),
    ("InvocationLatency", "p90_e2e",  "p90"),
    ("InvocationLatency", "p99_e2e",  "p99"),
    ("InvocationLatency", "avg_e2e",  "Average"),
    ("TimeToFirstToken",  "p50_ttft", "p50"),
    ("TimeToFirstToken",  "p90_ttft", "p90"),
    ("TimeToFirstToken",  "p99_ttft", "p99"),
    ("TimeToFirstToken",  "avg_ttft", "Average"),
    ("InvocationLatency", "sample_count", "SampleCount"),
]


def _cw_client(region: str, session: boto3.Session | None = None):
    """boto3 CloudWatch client built from a per-account session, with retry config.

    Pass `session=None` to use the running credentials (single-account mode).
    Pass an assumed-role session to make calls AS that account."""
    s = session or boto3._get_default_session()
    return s.client(
        "cloudwatch",
        region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_models(cw, namespace="AWS/Bedrock") -> list[tuple[str, str | None]]:
    """List distinct (ModelId, ContextWindow) seen in this region."""
    paginator = cw.get_paginator("list_metrics")
    seen: set[tuple[str, str | None]] = set()
    for page in paginator.paginate(Namespace=namespace):
        for m in page["Metrics"]:
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            mid = dims.get("ModelId")
            cw_window = dims.get("ContextWindow")
            if mid:
                seen.add((mid, cw_window))
    return sorted(seen, key=lambda t: (t[0], t[1] or ""))


def _get_metric_data(cw, queries: list[dict], start: datetime, end: datetime) -> dict[str, dict]:
    """Run a batch of MetricDataQueries; return id → {timestamps, values}."""
    out: dict[str, dict] = {}
    # CloudWatch API caps queries per call. Chunk to be safe.
    CHUNK = 500
    for i in range(0, len(queries), CHUNK):
        batch = queries[i:i + CHUNK]
        next_token = None
        while True:
            kwargs: dict[str, Any] = dict(
                StartTime=start,
                EndTime=end,
                MetricDataQueries=batch,
                ScanBy="TimestampAscending",
            )
            if next_token:
                kwargs["NextToken"] = next_token
            resp = cw.get_metric_data(**kwargs)
            for r in resp["MetricDataResults"]:
                rid = r["Id"]
                cur = out.setdefault(rid, {"timestamps": [], "values": []})
                cur["timestamps"].extend(r["Timestamps"])
                cur["values"].extend(r["Values"])
            next_token = resp.get("NextToken")
            if not next_token:
                break
    return out


def _safe_id(prefix: str, idx: int) -> str:
    """CloudWatch MetricDataQuery Ids must match ^[a-z][a-zA-Z0-9_]*$."""
    return f"{prefix}{idx}"


def _build_daily_queries(models: list[tuple[str, str | None]]) -> tuple[list[dict], dict[str, tuple[str, str, str | None]]]:
    """Return (queries, idx → (metric_name, modelId, context_window))."""
    queries: list[dict] = []
    idx_map: dict[str, tuple[str, str, str | None]] = {}
    counter = 0
    for mid, ctx in models:
        for metric_name, _ in [(m, s) for m, (_, s) in DAILY_METRICS.items()]:
            stat = DAILY_METRICS[metric_name][1]
            qid = _safe_id("d", counter)
            dims = [{"Name": "ModelId", "Value": mid}]
            if ctx and metric_name in ("InputTokenCount", "OutputTokenCount",
                                        "CacheReadInputTokenCount",
                                        "CacheWriteInputTokenCount"):
                dims.append({"Name": "ContextWindow", "Value": ctx})
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Bedrock",
                        "MetricName": metric_name,
                        "Dimensions": dims,
                    },
                    "Period": 86400,
                    "Stat": stat,
                },
                "ReturnData": True,
            })
            idx_map[qid] = (metric_name, mid, ctx)
            counter += 1
    return queries, idx_map


def _build_hourly_queries(models: list[tuple[str, str | None]]) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Hourly queries for f_hourly_peak + f_hourly_errors. Pulls Invocations,
    tokens, and BOTH client+server error counters so the same data feeds both
    tables (peak: Invocations / 429 proxy; errors: full 4xx/5xx breakdown)."""
    queries: list[dict] = []
    idx_map: dict[str, tuple[str, str]] = {}
    counter = 0
    seen_models: set[str] = set()
    for mid, _ in models:
        if mid in seen_models:
            continue
        seen_models.add(mid)
        for metric_name in ("Invocations", "InputTokenCount", "OutputTokenCount",
                            "InvocationClientErrors", "InvocationServerErrors"):
            qid = _safe_id("h", counter)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Bedrock",
                        "MetricName": metric_name,
                        "Dimensions": [{"Name": "ModelId", "Value": mid}],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })
            idx_map[qid] = (metric_name, mid)
            counter += 1
    return queries, idx_map


def _build_latency_queries(models: list[tuple[str, str | None]]) -> tuple[list[dict], dict[str, tuple[str, str, str]]]:
    """Latency queries: per-day percentiles per model."""
    queries: list[dict] = []
    idx_map: dict[str, tuple[str, str, str]] = {}
    counter = 0
    seen_models: set[str] = set()
    for mid, _ in models:
        if mid in seen_models:
            continue
        seen_models.add(mid)
        for metric_name, col_name, stat in LATENCY_METRICS:
            qid = _safe_id("l", counter)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Bedrock",
                        "MetricName": metric_name,
                        "Dimensions": [{"Name": "ModelId", "Value": mid}],
                    },
                    "Period": 86400,
                    "Stat": stat,
                },
                "ReturnData": True,
            })
            idx_map[qid] = (col_name, mid, stat)
            counter += 1
    return queries, idx_map


async def _ingest_region(conn: asyncpg.Connection, account: str, region: str,
                         start: datetime, end: datetime,
                         session: boto3.Session | None = None) -> dict[str, int]:
    cw = _cw_client(region, session=session)
    models = _list_models(cw)
    if not models:
        return {"f_daily": 0, "f_hourly_peak": 0, "f_latency_daily": 0}
    print(f"  [{account}/{region}] found {len(models)} ModelId series")

    counts = {"f_daily": 0, "f_hourly_peak": 0, "f_latency_daily": 0, "f_hourly_errors": 0}

    # ---- f_daily ----
    queries, idx_map = _build_daily_queries(models)
    print(f"  [{account}/{region}] f_daily: {len(queries)} queries...")
    raw = _get_metric_data(cw, queries, start, end)
    # Bucket: (date, modelId, context_window) → {column → value}
    daily_buckets: dict[tuple[date, str, str | None], dict[str, float]] = defaultdict(dict)
    for qid, (metric, mid, ctx) in idx_map.items():
        col, _ = DAILY_METRICS[metric]
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            d = ts.astimezone(timezone.utc).date()
            key = (d, mid, ctx)
            # Sum status codes into 4xx/5xx; fold into existing schema columns.
            daily_buckets[key][col] = daily_buckets[key].get(col, 0.0) + v

    daily_rows: list[tuple] = []
    for (d, mid, ctx), m in daily_buckets.items():
        total = int(m.get("total_requests", 0) or 0)
        c4xx = int(m.get("failed_requests_4xx", 0) or 0)
        c5xx = int(m.get("failed_requests_5xx", 0) or 0)
        if total <= 0:
            continue
        # Approximation: throttles (429) is a subset of client errors. CW also
        # exposes InvocationThrottles separately on some accounts; we treat
        # ClientErrors as the conservative throttled-count proxy and put server
        # errors into status_500.
        daily_rows.append((
            d, account, mid, region,
            "__none__", "__none__", "__none__", "__none__",  # operation/traffic/tier/profile
            total,                          # total_requests
            max(0, total - c4xx - c5xx),    # successful
            c4xx + c5xx,                    # failed
            int(m.get("total_input_tokens", 0) or 0),
            int(m.get("total_output_tokens", 0) or 0),
            int(m.get("total_cache_read_input_tokens", 0) or 0),
            int(m.get("total_cache_write_input_tokens", 0) or 0),
            0, 0, c4xx,                     # status_400, status_403, status_429 (proxy)
            c5xx, 0,                        # status_500, status_503
        ))

    if daily_rows:
        await conn.executemany(
            """
            INSERT INTO f_daily (
                event_date, accountId, modelId, region, operation, traffic_type,
                service_tier, inference_profile_prefix,
                total_requests, successful_requests, failed_requests,
                total_input_tokens, total_output_tokens,
                total_cache_read_input_tokens, total_cache_write_input_tokens,
                status_400_count, status_403_count, status_429_count,
                status_500_count, status_503_count
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
            ON CONFLICT (event_date, accountId, modelId, region, operation,
                         traffic_type, service_tier, inference_profile_prefix)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                successful_requests = EXCLUDED.successful_requests,
                failed_requests = EXCLUDED.failed_requests,
                total_input_tokens = EXCLUDED.total_input_tokens,
                total_output_tokens = EXCLUDED.total_output_tokens,
                total_cache_read_input_tokens = EXCLUDED.total_cache_read_input_tokens,
                total_cache_write_input_tokens = EXCLUDED.total_cache_write_input_tokens,
                status_400_count = EXCLUDED.status_400_count,
                status_403_count = EXCLUDED.status_403_count,
                status_429_count = EXCLUDED.status_429_count,
                status_500_count = EXCLUDED.status_500_count,
                status_503_count = EXCLUDED.status_503_count
            """,
            daily_rows,
        )
        counts["f_daily"] = len(daily_rows)

    # ---- f_hourly_peak + f_hourly_errors ----
    # The same hourly query batch feeds both tables to halve the CW-API cost.
    # peak gets the headline counters; errors gets the per-status-code breakdown
    # (4xx and 5xx) for the rolling 7-day window only — wipe and reload.
    queries, idx_map = _build_hourly_queries(models)
    print(f"  [{account}/{region}] f_hourly_peak + f_hourly_errors: {len(queries)} queries...")
    raw = _get_metric_data(cw, queries, start, end)
    hourly_buckets: dict[tuple[date, int, str], dict[str, float]] = defaultdict(dict)
    for qid, (metric, mid) in idx_map.items():
        col_map = {
            "Invocations":            "total_requests",
            "InputTokenCount":        "total_input_tokens",
            "OutputTokenCount":       "total_output_tokens",
            "InvocationClientErrors": "client_errors_4xx",
            "InvocationServerErrors": "server_errors_5xx",
        }
        col = col_map[metric]
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            ts_utc = ts.astimezone(timezone.utc)
            hourly_buckets[(ts_utc.date(), ts_utc.hour, mid)][col] = v

    hourly_rows = []
    err_rows = []
    today_utc = datetime.now(timezone.utc).date()
    err_window_start = today_utc - timedelta(days=6)  # rolling 7 days incl. today

    for (d, hr, mid), m in hourly_buckets.items():
        total = int(m.get("total_requests", 0) or 0)
        c4xx = int(m.get("client_errors_4xx", 0) or 0)
        c5xx = int(m.get("server_errors_5xx", 0) or 0)
        if total > 0:
            hourly_rows.append((
                d, hr, account, mid, region,
                total,
                int(m.get("total_input_tokens", 0) or 0),
                int(m.get("total_output_tokens", 0) or 0),
                c4xx,  # use client-errors as throttle proxy in peak table
            ))
        # Error rows: only within the rolling 7-day window AND only when
        # there's at least one failure to report.
        if (c4xx > 0 or c5xx > 0) and d >= err_window_start:
            failed = c4xx + c5xx
            # Same approximation as f_daily: split 5xx 60/40 between 500/503;
            # treat all 4xx as 429 (throttle proxy) until logs are wired in.
            s500 = int(c5xx * 0.6)
            s503 = c5xx - s500
            err_rows.append((
                d, hr, account, mid, region,
                total, failed,
                0, 0, c4xx, s500, s503,
            ))

    if hourly_rows:
        await conn.executemany(
            """
            INSERT INTO f_hourly_peak (
                event_date, hour, accountId, modelId, region,
                total_requests, total_input_tokens, total_output_tokens, status_429_count
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (event_date, hour, accountId, modelId, region)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                total_input_tokens = EXCLUDED.total_input_tokens,
                total_output_tokens = EXCLUDED.total_output_tokens,
                status_429_count = EXCLUDED.status_429_count
            """,
            hourly_rows,
        )
        counts["f_hourly_peak"] = len(hourly_rows)

    # f_hourly_errors: rolling 7-day window, full wipe + reload (small table,
    # ~85K rows max in the reference, much smaller for single-account customers).
    await conn.execute(
        """
        DELETE FROM f_hourly_errors
        WHERE accountId = $1 AND region = $2 AND event_date >= $3
        """,
        account, region, err_window_start,
    )
    if err_rows:
        await conn.executemany(
            """
            INSERT INTO f_hourly_errors (
                event_date, hour, accountId, modelId, region,
                total_requests, failed_requests,
                status_400_count, status_403_count, status_429_count,
                status_500_count, status_503_count
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (event_date, hour, accountId, modelId, region)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                failed_requests = EXCLUDED.failed_requests,
                status_400_count = EXCLUDED.status_400_count,
                status_403_count = EXCLUDED.status_403_count,
                status_429_count = EXCLUDED.status_429_count,
                status_500_count = EXCLUDED.status_500_count,
                status_503_count = EXCLUDED.status_503_count
            """,
            err_rows,
        )
        counts["f_hourly_errors"] = len(err_rows)

    # ---- f_latency_daily ----
    queries, idx_map = _build_latency_queries(models)
    print(f"  [{account}/{region}] f_latency_daily: {len(queries)} queries...")
    raw = _get_metric_data(cw, queries, start, end)
    lat_buckets: dict[tuple[date, str], dict[str, float]] = defaultdict(dict)
    for qid, (col, mid, _stat) in idx_map.items():
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            d = ts.astimezone(timezone.utc).date()
            lat_buckets[(d, mid)][col] = v

    lat_rows = []
    for (d, mid), m in lat_buckets.items():
        if not m.get("sample_count"):
            continue
        lat_rows.append((
            d, mid, "__none__", region,
            int(m.get("sample_count", 0)),
            m.get("avg_e2e"),  m.get("p50_e2e"),  m.get("p90_e2e"),  m.get("p99_e2e"),
            m.get("avg_ttft"), m.get("p50_ttft"), m.get("p90_ttft"), m.get("p99_ttft"),
        ))

    if lat_rows:
        await conn.executemany(
            """
            INSERT INTO f_latency_daily (
                event_date, modelId, traffic_type, region, sample_count,
                avg_e2e, p50_e2e, p90_e2e, p99_e2e,
                avg_ttft, p50_ttft, p90_ttft, p99_ttft
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (event_date, modelId, traffic_type, region)
            DO UPDATE SET
                sample_count = EXCLUDED.sample_count,
                avg_e2e = EXCLUDED.avg_e2e, p50_e2e = EXCLUDED.p50_e2e,
                p90_e2e = EXCLUDED.p90_e2e, p99_e2e = EXCLUDED.p99_e2e,
                avg_ttft = EXCLUDED.avg_ttft, p50_ttft = EXCLUDED.p50_ttft,
                p90_ttft = EXCLUDED.p90_ttft, p99_ttft = EXCLUDED.p99_ttft
            """,
            lat_rows,
        )
        counts["f_latency_daily"] = len(lat_rows)

    return counts


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest CloudWatch AWS/Bedrock metrics into the Ops Lens schema.",
    )
    _add_common_args(ap)
    ap.add_argument("--regions", default="",
                    help="comma-separated AWS regions; defaults to config.yaml monitored_regions")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    accts = discover_accounts(args)
    if not accts:
        print("ERROR: no monitored accounts resolved", file=sys.stderr)
        return 2

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    # Region resolution precedence: CLI flag wins; otherwise pull from config.
    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        try:
            from .config import load_config
            regions = load_config().resolved_regions()
        except Exception:
            regions = ["us-east-1"]

    print(f"Ingesting CW metrics: {len(accts)} account(s), regions={regions} "
          f"start={start.date()} end={end.date()}")

    conn = await asyncpg.connect(args.db_url)
    failures: list[tuple[str, str, str]] = []
    try:
        for monitored in accts:
            acct = monitored.accountId
            try:
                session = session_for(acct, role_name=args.role_name,
                                      external_id=args.external_id)
            except Exception as e:
                msg = f"sts:AssumeRole failed: {type(e).__name__}: {e}"
                print(f"  [{acct}] SKIP — {msg}", flush=True)
                failures.append((acct, "*", msg))
                continue

            for region in regions:
                try:
                    counts = await _ingest_region(conn, acct, region, start, end, session=session)
                    print(f"  [{acct}/{region}] {counts}")
                except Exception as e:  # one region's failure shouldn't kill the run
                    msg = f"{type(e).__name__}: {e}"
                    print(f"  [{acct}/{region}] ERROR — {msg}", flush=True)
                    failures.append((acct, region, msg))

        # Stamp metadata.
        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_cw_metrics_refresh', $1, now()),
                   ('last_cw_metrics_accounts', $2, now()),
                   ('last_cw_metrics_regions', $3, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
            ",".join(a.accountId for a in accts),
            ",".join(regions),
        )
    finally:
        await conn.close()

    if failures:
        print(f"\nDONE with {len(failures)} failure(s):")
        for acct, region, msg in failures:
            print(f"  [{acct}/{region}] {msg}")
        return 1
    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
