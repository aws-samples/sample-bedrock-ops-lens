#!/usr/bin/env python3
"""
CloudWatch Metrics ingester for the bedrock-mantle endpoint.

Reads AWS/BedrockMantle metrics (launched 2026-06-01) and writes endpoint
= 'mantle' rows into the same volumetric tables that cw_metrics.py
populates with endpoint = 'runtime'. The dashboard then renders both
endpoints side-by-side per tab.

Important differences from cw_metrics.py (the AWS/Bedrock collector):

  Namespace      AWS/BedrockMantle       (vs AWS/Bedrock)
  Metric names   Inferences              (vs Invocations)
                 InferenceClientErrors   (vs InvocationClientErrors)
                 TotalInputTokens        (vs InputTokenCount)
                 TotalOutputTokens       (vs OutputTokenCount)
  Dimension      Model                   (vs ModelId)
                 (no ContextWindow dim)
  Latency        NOT PUBLISHED           (Mantle has no CW latency metric;
                                          fill from invocation logs only)
  5xx errors     NOT PUBLISHED           (only 4xx is broken out)
  Throttles      NOT PUBLISHED           (folded into 4xx count)
  Granularity    in-region only          (no CRIS aggregation)
  Project dim    Yes (`Project`)         (deferred to v1.1)

Reference: AWS docs — "Monitor the bedrock-mantle endpoint" and the
CloudWatch metrics for bedrock-mantle
(https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle.html).

Usage:
    python -m ingestion.cw_mantle_metrics --accounts ID --regions us-east-1 --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import asyncpg
import boto3
from botocore.config import Config

from .accounts import _add_common_args, discover_accounts, session_for

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# Per the public AWS doc (monitoring-mantle-metrics.html, verified 2026-06-02).
NAMESPACE = "AWS/BedrockMantle"

# Region allowlist for the Mantle namespace. Calling list-metrics in a region
# without Mantle is harmless (returns empty) but adds latency on every run;
# gate by this list to avoid 12-region pings on every ingest.
SUPPORTED_REGIONS = {
    "us-east-1", "us-east-2", "us-west-2",
    "ap-southeast-3", "ap-south-1", "ap-southeast-2", "ap-northeast-1",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-south-1", "eu-north-1",
    "sa-east-1",
}

# Map AWS/BedrockMantle metrics -> schema columns (Sum stat for all).
DAILY_METRICS = {
    "Inferences":              "total_requests",
    "InferenceClientErrors":   "client_errors_4xx",
    "TotalInputTokens":        "total_input_tokens",
    "TotalOutputTokens":       "total_output_tokens",
}


def _safe_id(prefix: str, n: int) -> str:
    """CW metric data query IDs must match `^[a-z][a-zA-Z0-9_]*`."""
    return f"{prefix}{n:04d}"


def _cw_client(region: str, session: boto3.Session | None = None):
    s = session or boto3._get_default_session()
    return s.client(
        "cloudwatch",
        region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_models(cw) -> list[str]:
    """List distinct Model values seen in this region's AWS/BedrockMantle.

    Uses the Model-only granularity (not Project+Model) since v1 ships
    Model-level aggregation.

    IMPORTANT: CloudWatch's ListMetrics only returns metrics that have
    reported data in roughly the last 2 weeks. So this MISSES Mantle traffic
    older than that window — get_metric_data would still return those points,
    but we'd never query them because the model wasn't "discovered" here.
    Callers must therefore treat an empty/short result as "supplement with
    candidate models" (see _candidate_models), not "no Mantle traffic".
    """
    paginator = cw.get_paginator("list_metrics")
    seen: set[str] = set()
    for page in paginator.paginate(Namespace=NAMESPACE):
        for m in page.get("Metrics", []):
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            # Model-level metrics have exactly one dimension: Model.
            if list(dims.keys()) == ["Model"]:
                seen.add(dims["Model"])
    return sorted(seen)


# Common Mantle model ids to probe when ListMetrics is empty/stale. Mantle
# uses BARE model ids (no CRIS prefix). Kept small + current; the DB-derived
# candidates (from f_daily) cover everything else this account actually uses.
_STATIC_MANTLE_CANDIDATES = [
    "anthropic.claude-opus-4-8", "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-6", "anthropic.claude-opus-4-5",
    "anthropic.claude-sonnet-4-5", "anthropic.claude-haiku-4-5",
    "openai.gpt-oss-120b", "openai.gpt-oss-20b",
]


async def _candidate_models(conn: asyncpg.Connection, discovered: list[str]) -> list[str]:
    """Model ids to query get_metric_data for.

    Union of: (1) models ListMetrics discovered (recent), (2) distinct base
    modelIds already in f_daily (strip CRIS prefixes → the bare ids Mantle
    uses; these are the models this account genuinely runs), and (3) a small
    static set of current Mantle models. Querying a model with no data is
    cheap and harmless (get_metric_data just returns no points), so a superset
    is safe — and it fixes the "older than ~2 weeks → invisible" gap.
    """
    cands: set[str] = set(discovered)
    cands.update(_STATIC_MANTLE_CANDIDATES)
    try:
        rows = await conn.fetch("SELECT DISTINCT modelId FROM f_daily")
        for r in rows:
            mid = (r["modelid"] if "modelid" in r else r.get("modelId")) or ""
            # Strip CRIS/geo prefixes (us. / eu. / apac. / global. ...) to the
            # bare id Mantle publishes under the Model dimension.
            for pfx in ("us.", "eu.", "apac.", "jp.", "au.", "ca.", "amer.", "global."):
                if mid.startswith(pfx):
                    mid = mid[len(pfx):]
                    break
            # Drop the version/date suffix after ':' if present (keep base id).
            base = mid.split(":")[0]
            if base:
                cands.add(base)
    except Exception:
        pass
    return sorted(c for c in cands if c)


def _build_daily_queries(models: list[str]) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Build a CW GetMetricData payload for every (Model, daily-rolled metric)."""
    queries: list[dict] = []
    idx_map: dict[str, tuple[str, str]] = {}
    counter = 0
    for mid in models:
        for metric_name in DAILY_METRICS:
            qid = _safe_id("d", counter)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": NAMESPACE,
                        "MetricName": metric_name,
                        "Dimensions": [{"Name": "Model", "Value": mid}],
                    },
                    "Period": 86400,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })
            idx_map[qid] = (metric_name, mid)
            counter += 1
    return queries, idx_map


def _build_hourly_queries(models: list[str]) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Hourly Inferences + tokens + 4xx for f_hourly_peak and f_hourly_errors."""
    queries: list[dict] = []
    idx_map: dict[str, tuple[str, str]] = {}
    counter = 0
    for mid in models:
        for metric_name in DAILY_METRICS:
            qid = _safe_id("h", counter)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": NAMESPACE,
                        "MetricName": metric_name,
                        "Dimensions": [{"Name": "Model", "Value": mid}],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            })
            idx_map[qid] = (metric_name, mid)
            counter += 1
    return queries, idx_map


def _get_metric_data(cw, queries: list[dict], start: datetime, end: datetime) -> dict[str, dict]:
    """Run a batch of MetricDataQueries; return id -> {timestamps, values}."""
    out: dict[str, dict] = {}
    CHUNK = 500
    for i in range(0, len(queries), CHUNK):
        chunk = queries[i:i + CHUNK]
        next_token = None
        while True:
            kwargs = dict(MetricDataQueries=chunk, StartTime=start, EndTime=end,
                          ScanBy="TimestampAscending")
            if next_token:
                kwargs["NextToken"] = next_token
            resp = cw.get_metric_data(**kwargs)
            for r in resp.get("MetricDataResults", []):
                qid = r["Id"]
                out.setdefault(qid, {"timestamps": [], "values": []})
                out[qid]["timestamps"].extend(r.get("Timestamps", []))
                out[qid]["values"].extend(r.get("Values", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break
    return out


async def _ingest_region(conn: asyncpg.Connection, account: str, region: str,
                         start: datetime, end: datetime,
                         session: boto3.Session | None = None) -> dict[str, int]:
    if region not in SUPPORTED_REGIONS:
        # Skip silently — Mantle isn't in this region. Not an error.
        return {"f_daily": 0, "f_hourly_peak": 0, "f_hourly_errors": 0}

    cw = _cw_client(region, session=session)
    discovered = _list_models(cw)
    # Supplement ListMetrics (which only sees ~last 2 weeks) with candidate
    # models from the DB + a static set, so Mantle traffic older than that
    # window is still ingested. Querying a model with no data is a harmless
    # no-op, so a superset is safe.
    models = await _candidate_models(conn, discovered)
    if not models:
        return {"f_daily": 0, "f_hourly_peak": 0, "f_hourly_errors": 0}
    print(f"  [{account}/{region}] mantle: {len(discovered)} discovered + "
          f"{len(models)} candidates to probe")

    counts = {"f_daily": 0, "f_hourly_peak": 0, "f_hourly_errors": 0}

    # ---- f_daily ----
    queries, idx_map = _build_daily_queries(models)
    print(f"  [{account}/{region}] mantle f_daily: {len(queries)} queries...")
    raw = _get_metric_data(cw, queries, start, end)

    daily_buckets: dict[tuple[date, str], dict[str, float]] = defaultdict(dict)
    for qid, (metric, mid) in idx_map.items():
        col = DAILY_METRICS[metric]
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            d = ts.astimezone(timezone.utc).date()
            daily_buckets[(d, mid)][col] = daily_buckets[(d, mid)].get(col, 0.0) + v

    daily_rows: list[tuple] = []
    for (d, mid), m in daily_buckets.items():
        total = int(m.get("total_requests", 0) or 0)
        c4xx = int(m.get("client_errors_4xx", 0) or 0)
        if total <= 0:
            continue
        # Mantle CW exposes no 5xx — the "successful" count is overcounted
        # by exactly the (invisible) 5xx count. The UI labels this honestly.
        # 429 throttles are folded into the 4xx total per AWS docs; we put
        # the whole 4xx into status_429_count so the existing throttle-aware
        # severity coloring still lights up when something's wrong.
        daily_rows.append((
            d, account, mid, region,
            "__none__", "__none__", "__none__", "__none__",
            "mantle",                   # endpoint
            total,
            max(0, total - c4xx),       # successful (excl. unreported 5xx)
            c4xx,                       # failed
            int(m.get("total_input_tokens", 0) or 0),
            int(m.get("total_output_tokens", 0) or 0),
            0,                          # cache_read_input_tokens — Mantle has no equivalent
            0,                          # cache_write_input_tokens
            0, 0, c4xx,                 # 400, 403, 429 (4xx folded into 429 column)
            0, 0,                       # 500, 503 (not published)
        ))

    if daily_rows:
        await conn.executemany(
            """
            INSERT INTO f_daily (
                event_date, accountId, modelId, region, operation, traffic_type,
                service_tier, inference_profile_prefix, endpoint,
                total_requests, successful_requests, failed_requests,
                total_input_tokens, total_output_tokens,
                total_cache_read_input_tokens, total_cache_write_input_tokens,
                status_400_count, status_403_count, status_429_count,
                status_500_count, status_503_count
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
            ON CONFLICT (event_date, accountId, modelId, region, operation,
                         traffic_type, service_tier, inference_profile_prefix, endpoint)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                successful_requests = EXCLUDED.successful_requests,
                failed_requests = EXCLUDED.failed_requests,
                total_input_tokens = EXCLUDED.total_input_tokens,
                total_output_tokens = EXCLUDED.total_output_tokens,
                status_400_count = EXCLUDED.status_400_count,
                status_403_count = EXCLUDED.status_403_count,
                status_429_count = EXCLUDED.status_429_count
            """,
            daily_rows,
        )
        counts["f_daily"] = len(daily_rows)

    # ---- f_hourly_peak + f_hourly_errors ----
    queries, idx_map = _build_hourly_queries(models)
    print(f"  [{account}/{region}] mantle f_hourly_*: {len(queries)} queries...")
    raw = _get_metric_data(cw, queries, start, end)
    hourly_buckets: dict[tuple[date, int, str], dict[str, float]] = defaultdict(dict)
    for qid, (metric, mid) in idx_map.items():
        col = DAILY_METRICS[metric]
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            ts_utc = ts.astimezone(timezone.utc)
            hourly_buckets[(ts_utc.date(), ts_utc.hour, mid)][col] = v

    hourly_rows = []
    err_rows = []
    today_utc = datetime.now(timezone.utc).date()
    err_window_start = today_utc - timedelta(days=6)

    for (d, hr, mid), m in hourly_buckets.items():
        total = int(m.get("total_requests", 0) or 0)
        c4xx = int(m.get("client_errors_4xx", 0) or 0)
        if total > 0:
            hourly_rows.append((
                d, hr, account, mid, region, "mantle",
                total,
                int(m.get("total_input_tokens", 0) or 0),
                int(m.get("total_output_tokens", 0) or 0),
                0,      # total_cache_read_input_tokens: Mantle has no cache
                0,      # total_cache_write_input_tokens: ditto. Write 0 (not
                        # NULL) so the Peak-TPM quota calc counts Mantle tokens.
                c4xx,   # throttle proxy
            ))
        if c4xx > 0 and d >= err_window_start:
            err_rows.append((
                d, hr, account, mid, region, "mantle",
                total, c4xx,
                0, 0, c4xx,    # 400/403/429 (we don't break out 4xx further on Mantle)
                0, 0,          # 500/503 (not published)
            ))

    if hourly_rows:
        await conn.executemany(
            """
            INSERT INTO f_hourly_peak (
                event_date, hour, accountId, modelId, region, endpoint,
                total_requests, total_input_tokens, total_output_tokens,
                total_cache_read_input_tokens, total_cache_write_input_tokens,
                status_429_count
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (event_date, hour, accountId, modelId, region, endpoint)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                total_input_tokens = EXCLUDED.total_input_tokens,
                total_output_tokens = EXCLUDED.total_output_tokens,
                total_cache_read_input_tokens = EXCLUDED.total_cache_read_input_tokens,
                total_cache_write_input_tokens = EXCLUDED.total_cache_write_input_tokens,
                status_429_count = EXCLUDED.status_429_count
            """,
            hourly_rows,
        )
        counts["f_hourly_peak"] = len(hourly_rows)

    # f_hourly_errors: rolling-7d wipe + reload, scoped to the mantle slice.
    await conn.execute(
        """
        DELETE FROM f_hourly_errors
        WHERE accountId = $1 AND region = $2 AND event_date >= $3
          AND endpoint = 'mantle'
        """,
        account, region, err_window_start,
    )
    if err_rows:
        await conn.executemany(
            """
            INSERT INTO f_hourly_errors (
                event_date, hour, accountId, modelId, region, endpoint,
                total_requests, failed_requests,
                status_400_count, status_403_count, status_429_count,
                status_500_count, status_503_count
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (event_date, hour, accountId, modelId, region, endpoint)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                failed_requests = EXCLUDED.failed_requests,
                status_400_count = EXCLUDED.status_400_count,
                status_403_count = EXCLUDED.status_403_count,
                status_429_count = EXCLUDED.status_429_count
            """,
            err_rows,
        )
        counts["f_hourly_errors"] = len(err_rows)

    # NOTE: no f_latency_daily writes here. Mantle does not publish a CW
    # latency metric. Latency for Mantle is derived from Model Invocation
    # Logs (parsed by ingestion/invocation_logs.py).

    # ---- f_mantle_token_pctl (gap A) ----
    # Per-inference InputTokens/OutputTokens percentiles (p50/p90/p99). These
    # are published at the Project+Model level; we query per Model (percentiles
    # can't be re-aggregated across dimensions, so we take the Model-level
    # distribution). Daily period. Mantle's only per-request distribution
    # signal since it publishes no latency.
    counts["f_mantle_token_pctl"] = await _ingest_token_pctl(
        conn, cw, account, region, models, start, end)

    # ---- f_mantle_project (gap B) ----
    # Native Project-dimension rollup for chargeback. Mantle publishes
    # Inferences + tokens at Project and Project+Model levels; we discover the
    # Project values from ListMetrics and roll up per (project, model) per day.
    counts["f_mantle_project"] = await _ingest_projects(
        conn, cw, account, region, start, end)

    return counts


def _list_projects(cw) -> list[tuple[str, str]]:
    """Distinct (Project, Model) pairs seen in AWS/BedrockMantle (Project+Model
    granularity). ListMetrics only sees ~14 days; Project values can't be
    guessed, so anything older than that simply won't back-fill (acceptable —
    Project chargeback is forward-looking)."""
    paginator = cw.get_paginator("list_metrics")
    seen: set[tuple[str, str]] = set()
    for page in paginator.paginate(Namespace=NAMESPACE, MetricName="Inferences"):
        for m in page.get("Metrics", []):
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            if "Project" in dims:
                seen.add((dims["Project"], dims.get("Model", "__all__")))
    return sorted(seen)


async def _ingest_projects(conn, cw, account, region, start, end) -> int:
    pairs = _list_projects(cw)
    if not pairs:
        return 0
    # Build daily queries per (project, model, metric).
    proj_metrics = {"Inferences": "total_requests", "InferenceClientErrors": "client_errors_4xx",
                    "TotalInputTokens": "total_input_tokens", "TotalOutputTokens": "total_output_tokens"}
    queries: list[dict] = []
    idx: dict[str, tuple[str, str, str]] = {}
    n = 0
    for proj, model in pairs:
        dims = [{"Name": "Project", "Value": proj}]
        if model != "__all__":
            dims.append({"Name": "Model", "Value": model})
        for metric in proj_metrics:
            qid = _safe_id("j", n); n += 1
            queries.append({
                "Id": qid,
                "MetricStat": {"Metric": {"Namespace": NAMESPACE, "MetricName": metric,
                                          "Dimensions": dims}, "Period": 86400, "Stat": "Sum"},
                "ReturnData": True,
            })
            idx[qid] = (metric, proj, model)
    raw = _get_metric_data(cw, queries, start, end)
    buckets: dict[tuple[date, str, str], dict[str, float]] = defaultdict(dict)
    for qid, (metric, proj, model) in idx.items():
        col = proj_metrics[metric]
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            d = ts.astimezone(timezone.utc).date()
            buckets[(d, proj, model)][col] = buckets[(d, proj, model)].get(col, 0.0) + v
    rows = []
    for (d, proj, model), m in buckets.items():
        total = int(m.get("total_requests", 0) or 0)
        if total <= 0:
            continue
        rows.append((d, account, region, proj, model, total,
                     int(m.get("client_errors_4xx", 0) or 0),
                     int(m.get("total_input_tokens", 0) or 0),
                     int(m.get("total_output_tokens", 0) or 0)))
    if rows:
        await conn.executemany(
            """
            INSERT INTO f_mantle_project (
                event_date, accountId, region, project, modelId,
                total_requests, client_errors_4xx, total_input_tokens, total_output_tokens
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (event_date, accountId, region, project, modelId)
            DO UPDATE SET
                total_requests = EXCLUDED.total_requests,
                client_errors_4xx = EXCLUDED.client_errors_4xx,
                total_input_tokens = EXCLUDED.total_input_tokens,
                total_output_tokens = EXCLUDED.total_output_tokens
            """,
            rows,
        )
    return len(rows)


_PCTL_STATS = ["p50", "p90", "p99"]


async def _ingest_token_pctl(conn, cw, account, region, models, start, end) -> int:
    """Query per-inference InputTokens/OutputTokens percentiles per model/day."""
    # Build GetMetricData queries: one per (model, metric, percentile), daily.
    queries: list[dict] = []
    idx: dict[str, tuple[str, str, str]] = {}  # qid -> (metric, pctl, model)
    n = 0
    for mid in models:
        for metric in ("InputTokens", "OutputTokens"):
            for pc in _PCTL_STATS:
                qid = _safe_id("p", n); n += 1
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {"Namespace": NAMESPACE, "MetricName": metric,
                                   "Dimensions": [{"Name": "Model", "Value": mid}]},
                        "Period": 86400, "Stat": pc,
                    },
                    "ReturnData": True,
                })
                idx[qid] = (metric, pc, mid)
    if not queries:
        return 0
    raw = _get_metric_data(cw, queries, start, end)
    # bucket by (date, model) -> {metric_pctl: value}
    buckets: dict[tuple[date, str], dict[str, float]] = defaultdict(dict)
    for qid, (metric, pc, mid) in idx.items():
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            d = ts.astimezone(timezone.utc).date()
            buckets[(d, mid)][f"{metric}_{pc}"] = v
    rows = []
    for (d, mid), m in buckets.items():
        # only write rows that actually have at least one percentile value
        if not m:
            continue
        rows.append((
            d, account, mid, region, "__none__",
            None,  # sample_count unknown from percentile stat alone
            m.get("InputTokens_p50"), m.get("InputTokens_p90"), m.get("InputTokens_p99"),
            m.get("OutputTokens_p50"), m.get("OutputTokens_p90"), m.get("OutputTokens_p99"),
        ))
    if rows:
        await conn.executemany(
            """
            INSERT INTO f_mantle_token_pctl (
                event_date, accountId, modelId, region, project, sample_count,
                p50_input_tokens, p90_input_tokens, p99_input_tokens,
                p50_output_tokens, p90_output_tokens, p99_output_tokens
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (event_date, accountId, modelId, region, project)
            DO UPDATE SET
                p50_input_tokens = EXCLUDED.p50_input_tokens,
                p90_input_tokens = EXCLUDED.p90_input_tokens,
                p99_input_tokens = EXCLUDED.p99_input_tokens,
                p50_output_tokens = EXCLUDED.p50_output_tokens,
                p90_output_tokens = EXCLUDED.p90_output_tokens,
                p99_output_tokens = EXCLUDED.p99_output_tokens
            """,
            rows,
        )
    return len(rows)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest CloudWatch AWS/BedrockMantle metrics.",
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

    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        try:
            from .config import load_config
            regions = load_config().resolved_regions()
        except Exception:
            regions = ["us-east-1"]

    print(f"Ingesting Mantle CW metrics: {len(accts)} account(s), regions={regions} "
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
                print(f"  [{acct}] AssumeRole failed: {e}", file=sys.stderr)
                failures.append((acct, "*", f"AssumeRole: {e}"))
                continue
            for region in regions:
                try:
                    counts = await _ingest_region(conn, acct, region, start, end, session=session)
                    print(f"  [{acct}/{region}] {counts}")
                except Exception as e:
                    print(f"  [{acct}/{region}] FAILED: {e}", file=sys.stderr)
                    failures.append((acct, region, str(e)))
    finally:
        await conn.close()

    if failures:
        print(f"Mantle ingest: {len(failures)} (account, region) failure(s):", file=sys.stderr)
        for a, r, e in failures:
            print(f"  {a}/{r}: {e}", file=sys.stderr)
        return 1
    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
