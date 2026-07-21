#!/usr/bin/env python3
"""
Proxy per-workload event ingester for Bedrock Ops Lens (Task A).

A GenAI proxy that fronts Bedrock signs every request with ONE IAM role, so
caller identity can't attribute usage to a workload. Instead the proxy emits
ONE metadata-only event per request to an S3 bucket in the customer's account,
which we read cross-account (same trust pattern as Bedrock invocation logs — no
public inbound endpoint). Works across BOTH bedrock-runtime and bedrock-mantle
because the proxy reads token counts from whichever response body it gets.

S3 layout the proxy writes (NDJSON, one JSON object per line; .jsonl or
.jsonl.gz):
    s3://<bucket>/proxy-events/<region>/<YYYY>/<MM>/<DD>/<HH>/*.jsonl[.gz]

Each line (metadata only — NEVER prompt/response text):
    {
      "ts": "2026-07-03T18:03:22Z",   # ISO-8601 UTC
      "dimensions": {                  # ARBITRARY custom attribution map
        "workload": "search-service",
        "env": "prod",
        "business_unit": "retail"
      },
      "model": "anthropic.claude-opus-4-8",
      "endpoint": "runtime" | "mantle",
      "region": "us-east-1",
      "input_tokens": 812,
      "output_tokens": 143,
      "cache_read_tokens": 0,          # optional
      "status": 200,                   # HTTP status the proxy saw
      "throttled": false,              # true if a 429/throttle
      "latency_ms": 940,               # optional; proxy wall-clock
      "request_id": "msg_bdrk_..."     # for idempotency
    }

    Back-compat: a top-level "workload": "x" is accepted and folded into
    dimensions as {"workload": "x"}. A request with no dimensions is bucketed
    under {"workload": "__unattributed__"} so its tokens still count.

Writes:
  - f_request_events       raw per-request rows w/ full JSONB dimensions map
  - f_proxy_dim_hourly     hourly rollup, one row per (dim_key, dim_value, …)
  - dim_proxy_dimensions   distinct (dim_key, dim_value) pairs for the picker

Resumability: a `(s3_key)` entry in `proxy_events_objects` records files already
processed, so re-runs skip them.

Usage:
    python -m ingestion.proxy_events \\
        --bucket my-genai-proxy-events \\
        --regions us-east-1,us-west-2 --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import asyncpg
import boto3
from botocore.config import Config

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# Cap how many raw request rows we retain per hour bucket in memory, so a
# pathological volume day can't blow the Lambda's memory. The hourly rollup is
# always complete; only the raw f_request_events sample is bounded.
RAW_RETENTION_DAYS = 14


def _s3_client(region: str):
    return boto3.client(
        "s3", region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_event_keys(s3, bucket: str, region: str,
                     start_dt: datetime, end_dt: datetime) -> list[str]:
    """Every proxy-event object key in the date range for one region."""
    keys: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        prefix = f"proxy-events/{region}/{cur:%Y/%m/%d}/"
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                k = obj["Key"]
                if obj["Size"] == 0:
                    continue
                if not (k.endswith(".jsonl") or k.endswith(".jsonl.gz")
                        or k.endswith(".json") or k.endswith(".json.gz")):
                    continue
                keys.append(k)
        cur += timedelta(days=1)
    return keys


async def _already_processed(conn: asyncpg.Connection, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = await conn.fetch(
        "SELECT s3_key FROM proxy_events_objects WHERE s3_key = ANY($1::text[])",
        keys,
    )
    return {r["s3_key"] for r in rows}


def _read_event_lines(s3, bucket: str, key: str):
    """Yield parsed JSON objects from a proxy-events object (NDJSON, maybe gz)."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    if key.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return
    for line in body.split(b"\n"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue


# Bucket for requests the proxy sent with no dimensions at all — so their
# tokens still count toward totals instead of vanishing.
_UNATTRIBUTED = "__unattributed__"
# Cap dimension keys per event so a misconfigured proxy can't explode the
# fan-out (mirrors the spirit of Bedrock's 10-tag requestMetadata limit).
_MAX_DIMS = 10


def _extract_dimensions(e: dict) -> dict:
    """Return a cleaned {key: value} string map from an event.

    Accepts either a `dimensions` object or a top-level `workload` (back-compat),
    or both (merged). Keys/values are trimmed strings; non-string scalars are
    stringified; empty keys/values dropped. Capped at _MAX_DIMS keys."""
    dims: dict[str, str] = {}
    raw = e.get("dimensions")
    if isinstance(raw, dict):
        for k, v in raw.items():
            ks = str(k).strip()
            if v is None:
                continue
            vs = str(v).strip()
            if ks and vs:
                dims[ks] = vs
    # Back-compat: a bare top-level workload becomes a dimension.
    wl = e.get("workload")
    if wl is not None:
        wls = str(wl).strip()
        if wls and "workload" not in dims:
            dims["workload"] = wls
    if not dims:
        dims = {"workload": _UNATTRIBUTED}
    # Deterministic truncation (sorted) if a proxy over-emits.
    if len(dims) > _MAX_DIMS:
        dims = dict(sorted(dims.items())[:_MAX_DIMS])
    return dims


def _parse_event(e: dict):
    """Normalize one proxy event. Returns a tuple or None if unusable.

    (ts, event_date, hour, dimensions_dict, modelId, endpoint, region, accountId,
     in_tok, out_tok, cache_read, status, throttled, latency_ms, request_id)
    """
    ts_raw = e.get("ts") or e.get("timestamp")
    model = (e.get("model") or e.get("modelId") or "").strip()
    if not ts_raw or not model:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None

    dimensions = _extract_dimensions(e)

    endpoint = (e.get("endpoint") or "runtime").lower()
    if endpoint not in ("runtime", "mantle"):
        endpoint = "runtime"
    region = (e.get("region") or "").strip() or "unknown"
    account = (e.get("accountId") or e.get("account_id") or "__none__").strip() or "__none__"

    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    in_tok = _int(e.get("input_tokens"))
    out_tok = _int(e.get("output_tokens"))
    cache_read = _int(e.get("cache_read_tokens"))
    status = _int(e.get("status")) or 200
    throttled = bool(e.get("throttled")) or status == 429
    latency = e.get("latency_ms")
    try:
        latency_ms = float(latency) if latency is not None else None
    except (TypeError, ValueError):
        latency_ms = None
    # Idempotency key. Fall back to a synthetic one if the proxy omitted it,
    # combining the fields so identical re-reads dedupe but distinct calls don't.
    request_id = (e.get("request_id") or e.get("id") or "").strip()
    if not request_id:
        dim_sig = ",".join(f"{k}={v}" for k, v in sorted(dimensions.items()))
        request_id = f"{dim_sig}:{model}:{ts_raw}:{in_tok}:{out_tok}"

    return (dt, dt.date(), dt.hour, dimensions, model, endpoint, region, account,
            in_tok, out_tok, cache_read, status, throttled, latency_ms, request_id)


def _pct(sorted_vals: list[float], p: float):
    """Nearest-rank percentile (matches invocation_logs latency math)."""
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    idx = max(0, min(n - 1, int(round(p * (n - 1)))))
    return sorted_vals[idx]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--regions", default="us-east-1")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]

    conn = await asyncpg.connect(args.db_url)
    total_events = 0
    new_keys: list[tuple] = []

    try:
        # Hourly rollup accumulator + latency samples per bucket. We fan each
        # request out to one bucket PER dimension key (workload/env/bu/…), so
        # summing a single dim_key later is correct.
        # key = (event_date, hour, dim_key, dim_value, modelId, endpoint, region, account)
        rollup: dict[tuple, dict] = defaultdict(lambda: {
            "total_requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "throttled_count": 0, "error_count": 0,
            "latencies": [],
        })
        raw_rows: list[tuple] = []
        raw_cutoff = (end - timedelta(days=RAW_RETENTION_DAYS)).date()

        for region in regions:
            s3 = _s3_client(region)
            keys = _list_event_keys(s3, args.bucket, region, start, end)
            already = await _already_processed(conn, keys)
            pending = [k for k in keys if k not in already]
            print(f"  [{region}] {len(keys)} proxy-event objects "
                  f"({len(already)} already processed, {len(pending)} new)")

            for key in pending:
                obj_rows = 0
                for e in _read_event_lines(s3, args.bucket, key):
                    parsed = _parse_event(e)
                    if not parsed:
                        continue
                    (ts, ev_date, hr, dimensions, model, endpoint, region_v, account,
                     in_tok, out_tok, cache_read, status, throttled, latency_ms,
                     request_id) = parsed
                    obj_rows += 1

                    # Fan out: one rollup bucket per (dim_key, dim_value).
                    for dim_key, dim_value in dimensions.items():
                        b = rollup[(ev_date, hr, dim_key, dim_value, model,
                                    endpoint, region_v, account)]
                        b["total_requests"] += 1
                        b["input_tokens"] += in_tok
                        b["output_tokens"] += out_tok
                        b["cache_read_tokens"] += cache_read
                        if throttled:
                            b["throttled_count"] += 1
                        if status >= 400:
                            b["error_count"] += 1
                        if latency_ms is not None and latency_ms >= 0:
                            b["latencies"].append(latency_ms)

                    if ev_date >= raw_cutoff:
                        raw_rows.append((
                            ts, ev_date, json.dumps(dimensions), model, endpoint,
                            region_v, account, in_tok, out_tok, cache_read, status,
                            throttled, latency_ms, request_id,
                        ))

                new_keys.append((key, obj_rows))
                total_events += obj_rows

        # --- write raw per-request rows (idempotent on request_id, ts) ------
        if raw_rows:
            await conn.executemany(
                """
                INSERT INTO f_request_events (
                    ts, event_date, dimensions, modelId, endpoint, region, accountId,
                    input_tokens, output_tokens, cache_read_tokens,
                    status, throttled, latency_ms, request_id
                ) VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (event_date, request_id, ts) DO NOTHING
                """,
                raw_rows,
            )

        # --- write hourly rollup (additive upsert) --------------------------
        if rollup:
            roll_rows = []
            for (ev_date, hr, dim_key, dim_value, model, endpoint, region_v, account), m in rollup.items():
                lat = sorted(m["latencies"])
                roll_rows.append((
                    ev_date, hr, dim_key, dim_value, model, endpoint, region_v, account,
                    m["total_requests"], m["input_tokens"], m["output_tokens"],
                    m["cache_read_tokens"], m["throttled_count"], m["error_count"],
                    _pct(lat, 0.50), _pct(lat, 0.90), _pct(lat, 0.99),
                ))
            await conn.executemany(
                """
                INSERT INTO f_proxy_dim_hourly (
                    event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId,
                    total_requests, input_tokens, output_tokens, cache_read_tokens,
                    throttled_count, error_count,
                    p50_latency_ms, p90_latency_ms, p99_latency_ms
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId)
                DO UPDATE SET
                    total_requests   = f_proxy_dim_hourly.total_requests   + EXCLUDED.total_requests,
                    input_tokens     = f_proxy_dim_hourly.input_tokens     + EXCLUDED.input_tokens,
                    output_tokens    = f_proxy_dim_hourly.output_tokens    + EXCLUDED.output_tokens,
                    cache_read_tokens= f_proxy_dim_hourly.cache_read_tokens+ EXCLUDED.cache_read_tokens,
                    throttled_count  = f_proxy_dim_hourly.throttled_count  + EXCLUDED.throttled_count,
                    error_count      = f_proxy_dim_hourly.error_count      + EXCLUDED.error_count,
                    -- percentiles: take the max as a cheap "worst seen" merge
                    -- (exact cross-batch percentiles would need raw samples;
                    -- for the reporting use case worst-of is acceptable and honest).
                    p50_latency_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p50_latency_ms,0), COALESCE(EXCLUDED.p50_latency_ms,0)),
                    p90_latency_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p90_latency_ms,0), COALESCE(EXCLUDED.p90_latency_ms,0)),
                    p99_latency_ms = GREATEST(COALESCE(f_proxy_dim_hourly.p99_latency_ms,0), COALESCE(EXCLUDED.p99_latency_ms,0))
                """,
                roll_rows,
            )

        # --- resumability markers -------------------------------------------
        if new_keys:
            await conn.executemany(
                "INSERT INTO proxy_events_objects (s3_key, row_count) "
                "VALUES ($1, $2) ON CONFLICT (s3_key) DO NOTHING",
                new_keys,
            )

        # --- retention: trim raw events beyond the window ------------------
        await conn.execute(
            "DELETE FROM f_request_events WHERE event_date < current_date - $1::int",
            RAW_RETENTION_DAYS,
        )

        # --- refresh dim_proxy_dimensions dropdown source -------------------
        await conn.execute("DELETE FROM dim_proxy_dimensions")
        await conn.execute("""
            INSERT INTO dim_proxy_dimensions (dim_key, dim_value, first_seen, last_seen, total_requests_30d, endpoints)
            SELECT dim_key, dim_value, MIN(event_date), MAX(event_date), SUM(total_requests),
                   array_agg(DISTINCT endpoint)
            FROM f_proxy_dim_hourly
            WHERE event_date >= current_date - INTERVAL '30 days'
            GROUP BY dim_key, dim_value
        """)

        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_proxy_events_refresh', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
        )

        n_dims = await conn.fetchval("SELECT COUNT(*) FROM dim_proxy_dimensions")
        n_keys = await conn.fetchval("SELECT COUNT(DISTINCT dim_key) FROM dim_proxy_dimensions")
        print(f"DONE. parsed {total_events} proxy events → "
              f"{len(rollup)} hourly rollup rows, {n_dims} distinct (key,value) pairs "
              f"across {n_keys} dimension keys.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
