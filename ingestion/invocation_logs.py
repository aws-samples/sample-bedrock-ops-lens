#!/usr/bin/env python3
"""
Invocation-log ingester for Bedrock Ops Lens.

Reads Bedrock model invocation logs from S3, parses the requestMetadata field,
and aggregates into f_daily_tagged. Each log line is one invocation. A log row
with N requestMetadata keys fans out to N rows in f_daily_tagged (one per
tag_key) so per-tag GROUP BY math works correctly.

Reads s3://<bucket>/AWSLogs/<accountId>/BedrockModelInvocationLogs/<region>/<YYYY>/<MM>/<DD>/<HH>/...
The .json.gz files contain one JSON document per line.

Resumability: a `(s3_key)` entry in `ingestion_log_objects` records files
already processed, so re-runs skip them.

Usage:
    python -m ingestion.invocation_logs \\
        --bucket bedrock-lens-invocation-logs-YOUR_ACCOUNT_ID-us-east-1 \\
        --accounts YOUR_ACCOUNT_ID --regions us-east-1 --days 7
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
from typing import Any

import asyncpg
import boto3
from botocore.config import Config

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)


def _s3_client(region: str):
    return boto3.client(
        "s3", region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_log_keys(s3, bucket: str, account: str, region: str,
                    start_dt: datetime, end_dt: datetime) -> list[str]:
    """Yield every log object key in the given UTC date range."""
    keys: list[str] = []
    cur = start_dt
    while cur <= end_dt:
        prefix = (
            f"AWSLogs/{account}/BedrockModelInvocationLogs/{region}/"
            f"{cur:%Y/%m/%d}/"
        )
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                k = obj["Key"]
                if k.endswith("amazon-bedrock-logs-permission-check"):
                    continue
                if obj["Size"] == 0:
                    continue
                # Skip large-payload sidecars (the message bodies dropped to S3
                # when the inline log line exceeds 100 KB). They live under
                # `<prefix>/<HH>/data/...` and contain just the messages array,
                # not a structured log record. We get all the fields we need
                # from the top-level `<prefix>/<HH>/<ts>_<id>.json.gz` records.
                if "/data/" in k:
                    continue
                keys.append(k)
        cur += timedelta(days=1)
    return keys


async def _already_processed(conn: asyncpg.Connection, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    rows = await conn.fetch(
        "SELECT s3_key FROM ingestion_log_objects WHERE s3_key = ANY($1::text[])",
        keys,
    )
    return {r["s3_key"] for r in rows}


def _read_log_lines(s3, bucket: str, key: str):
    """Read a Bedrock invocation log object (gzipped json-lines)."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    try:
        if key.endswith(".gz") or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        for line in body.split(b"\n"):
            if not line.strip():
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    except Exception:
        return


# Real per-code statuses we track. Mirrors f_hourly_status columns.
STATUS_CODES = (200, 400, 403, 404, 408, 424, 429, 500, 503)


def _http_status_from_log(entry: dict) -> int:
    """Map a Bedrock invocation-log record to a true HTTP status code.

    Unlike CloudWatch (which only exposes all-4xx and all-5xx aggregates),
    the raw invocation log carries a genuine per-request `errorCode` — the
    Bedrock exception name. We translate each exception to its HTTP status so
    the dashboard can show a real per-code breakdown.

    Preference order:
      1. An explicit numeric status if the log carries one (future-proofing —
         some log schemas include httpStatusCode).
      2. The exception name in `errorCode` / `error`.
      3. No error → 200.
    """
    http = entry.get("httpStatusCode") or (entry.get("output") or {}).get("httpStatusCode")
    if http:
        try:
            code = int(http)
            return code if code in STATUS_CODES else (
                400 if 400 <= code < 500 else 500 if 500 <= code < 600 else 200)
        except (TypeError, ValueError):
            pass

    err = entry.get("errorCode") or entry.get("error")
    if not err:
        return 200
    e = str(err)
    # Bedrock exception names → HTTP status. Order matters (most specific first).
    return (
        429 if "Throttl" in e else
        403 if "AccessDenied" in e or "Forbidden" in e or "Unauthorized" in e else
        404 if "NotFound" in e else                              # ResourceNotFoundException
        408 if "Timeout" in e else                               # ModelTimeoutException / RequestTimeout
        424 if "ModelError" in e or "FailedDependency" in e
              or "DependencyFailed" in e or "ModelNotReady" in e else
        400 if "Validation" in e or "BadRequest" in e
              or "Malformed" in e or "Serialization" in e else
        503 if "ServiceUnavailable" in e or "Unavailable" in e else
        500  # InternalServerException / ServiceException / default
    )


def _parse_log_entry(entry: dict) -> tuple[date, int, str, str, str, str, dict, int, int, int, float | None]:
    """Returns (date, hour, accountId, modelId, region, operation,
              requestMetadata, total_in_tokens, total_out_tokens,
              status_code, latency_ms) or None.

    latency_ms is read from output.outputBodyJson.metrics.latencyMs when
    present (Bedrock invocation logs), else None. The dashboard's Latency
    tab uses this for the bedrock-mantle path because Mantle does not
    publish latency to CloudWatch.
    """
    ts = entry.get("timestamp")
    acct = entry.get("accountId", "")
    region = entry.get("region", "")
    op = entry.get("operation", "")
    model_id = entry.get("modelId", "")
    if not (ts and acct and region and op and model_id):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None
    d, hr = dt.date(), dt.hour
    metadata = entry.get("requestMetadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    in_t = int((entry.get("input") or {}).get("inputTokenCount") or 0)
    out_t = int((entry.get("output") or {}).get("outputTokenCount") or 0)
    status = _http_status_from_log(entry)
    # Latency: nested under output.outputBodyJson.metrics.latencyMs in
    # Bedrock invocation logs. Defensive deep-get to handle older logs
    # that don't include the metrics block.
    latency_ms: float | None = None
    output_block = entry.get("output") or {}
    body_json = output_block.get("outputBodyJson") or {}
    metrics_block = body_json.get("metrics") or {}
    raw_lat = metrics_block.get("latencyMs")
    if raw_lat is not None:
        try:
            latency_ms = float(raw_lat)
        except (TypeError, ValueError):
            latency_ms = None
    return d, hr, acct, model_id, region, op, metadata, in_t, out_t, status, latency_ms


async def _ensure_log_objects_table(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_log_objects (
            s3_key       TEXT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            row_count    BIGINT NOT NULL,
            tag_count    BIGINT NOT NULL DEFAULT 0
        )
    """)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--accounts", default="YOUR_ACCOUNT_ID")
    ap.add_argument("--regions", default="us-east-1")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()]
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]

    conn = await asyncpg.connect(args.db_url)
    await _ensure_log_objects_table(conn)

    total_logs = 0
    total_tagged_rows = 0
    new_keys: list[tuple] = []

    try:
        for acct in accounts:
            for region in regions:
                s3 = _s3_client(region)
                keys = _list_log_keys(s3, args.bucket, acct, region, start, end)
                already = await _already_processed(conn, keys)
                pending = [k for k in keys if k not in already]
                print(f"  [{acct}/{region}] {len(keys)} log objects ({len(already)} already processed, {len(pending)} new)")

                # Aggregate everything in memory, fan out to per-tag rows, upsert in one go.
                buckets: dict[tuple, dict] = defaultdict(lambda: {
                    "total_requests": 0, "failed_requests": 0,
                    "total_input_tokens": 0, "total_output_tokens": 0,
                    "cache_read": 0, "cache_write": 0,
                })
                # Per (date, hour, account, model, region) → real per-status-code
                # counts. This is the genuine per-code data CloudWatch can't give;
                # it feeds f_hourly_status and the dashboard's "Status Codes" chart.
                status_buckets: dict[tuple, dict[int, int]] = defaultdict(
                    lambda: {c: 0 for c in STATUS_CODES})

                # Latency samples per (date, model, region, endpoint). The
                # bedrock-mantle endpoint does not publish latency to CW —
                # f_latency_daily for endpoint='mantle' comes from here.
                latency_samples: dict[tuple, list[float]] = defaultdict(list)

                # Per-principal (identity.arn) usage → f_identity_usage (gap G):
                # "who is calling Bedrock" at the IAM-principal level. Keyed by
                # (date, account, region, arn, model, endpoint).
                identity_buckets: dict[tuple, dict[str, int]] = defaultdict(
                    lambda: {"total_requests": 0, "total_input_tokens": 0,
                             "total_output_tokens": 0, "failed_requests": 0})

                for key in pending:
                    obj_rows = 0
                    obj_tag_rows = 0
                    for entry in _read_log_lines(s3, args.bucket, key):
                        parsed = _parse_log_entry(entry)
                        if not parsed:
                            continue
                        d, hr, a, mid, r, op, metadata, in_t, out_t, status, latency_ms = parsed
                        # Endpoint detection: invocation logs from the
                        # bedrock-mantle endpoint surface either an explicit
                        # endpoint field or an operation name carrying the
                        # Mantle API hint (Responses / ChatCompletions /
                        # Messages). Default to 'runtime' for everything else.
                        ep_hint = (entry.get("endpoint") or "").lower()
                        if "mantle" in ep_hint:
                            endpoint = "mantle"
                        elif any(s in op for s in ("Responses", "ChatCompletions", "Messages")):
                            endpoint = "mantle"
                        else:
                            endpoint = "runtime"
                        obj_rows += 1
                        # Real per-code hourly tally (one bump per request),
                        # keyed by endpoint too so the Status Codes chart can
                        # slice runtime vs mantle (both may appear in the same
                        # invocation-log stream).
                        sb = status_buckets[(d, hr, a, mid, r, endpoint)]
                        sb[status if status in sb else (
                            400 if 400 <= status < 500 else
                            500 if 500 <= status < 600 else 200)] += 1
                        # Fan out to one row per tag_key. If no tags, write a single
                        # row with sentinel '__none__'.
                        if not metadata:
                            tag_pairs = [("__none__", "__none__")]
                        else:
                            tag_pairs = [(str(k)[:256], str(v)[:256]) for k, v in metadata.items()]
                        for tk, tv in tag_pairs:
                            key_tuple = (d, a, mid, r, op, tk, tv)
                            b = buckets[key_tuple]
                            b["total_requests"] += 1
                            if status >= 400:
                                b["failed_requests"] += 1
                            b["total_input_tokens"] += in_t
                            b["total_output_tokens"] += out_t
                            obj_tag_rows += 1
                        # Per-principal attribution (gap G). identity.arn is the
                        # IAM principal that made the call; truncate to keep the
                        # key bounded. '__unknown__' when the log omits it.
                        arn = ((entry.get("identity") or {}).get("arn") or "__unknown__")[:512]
                        ib = identity_buckets[(d, a, r, arn, mid, endpoint)]
                        ib["total_requests"] += 1
                        ib["total_input_tokens"] += in_t
                        ib["total_output_tokens"] += out_t
                        if status >= 400:
                            ib["failed_requests"] += 1
                        # Record per-request latency for the endpoint slice.
                        if latency_ms is not None and latency_ms >= 0:
                            latency_samples[(d, mid, r, endpoint)].append(latency_ms)
                    new_keys.append((key, obj_rows, obj_tag_rows))
                    total_logs += obj_rows

                if buckets:
                    rows = []
                    for (d, a, mid, r, op, tk, tv), m in buckets.items():
                        rows.append((
                            d, a, mid, r, op, tk, tv,
                            m["total_requests"], m["failed_requests"],
                            m["total_input_tokens"], m["total_output_tokens"],
                            m["cache_read"], m["cache_write"],
                        ))
                    await conn.executemany(
                        """
                        INSERT INTO f_daily_tagged (
                            event_date, accountId, modelId, region, operation,
                            tag_key, tag_value,
                            total_requests, failed_requests,
                            total_input_tokens, total_output_tokens,
                            total_cache_read_input_tokens,
                            total_cache_write_input_tokens
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        ON CONFLICT (event_date, accountId, modelId, region,
                                     operation, tag_key, tag_value) DO UPDATE SET
                            total_requests = f_daily_tagged.total_requests + EXCLUDED.total_requests,
                            failed_requests = COALESCE(f_daily_tagged.failed_requests,0) + COALESCE(EXCLUDED.failed_requests,0),
                            total_input_tokens = COALESCE(f_daily_tagged.total_input_tokens,0) + COALESCE(EXCLUDED.total_input_tokens,0),
                            total_output_tokens = COALESCE(f_daily_tagged.total_output_tokens,0) + COALESCE(EXCLUDED.total_output_tokens,0)
                        """,
                        rows,
                    )
                    total_tagged_rows += len(rows)

                # Real per-status-code hourly rows → f_hourly_status. Additive
                # upsert so re-runs over overlapping windows accumulate correctly
                # (matches f_daily_tagged semantics). Only emit hours that had at
                # least one request.
                if status_buckets:
                    status_rows = []
                    for (d, hr, a, mid, r, endpoint), sc in status_buckets.items():
                        total = sum(sc.values())
                        if total <= 0:
                            continue
                        status_rows.append((
                            d, hr, a, mid, r, endpoint, total,
                            sc[200], sc[400], sc[403], sc[404], sc[408],
                            sc[424], sc[429], sc[500], sc[503],
                        ))
                    if status_rows:
                        await conn.executemany(
                            """
                            INSERT INTO f_hourly_status (
                                event_date, hour, accountId, modelId, region, endpoint,
                                total_requests,
                                status_200_count, status_400_count, status_403_count,
                                status_404_count, status_408_count, status_424_count,
                                status_429_count, status_500_count, status_503_count
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                            ON CONFLICT (event_date, hour, accountId, modelId, region, endpoint)
                            DO UPDATE SET
                                total_requests   = f_hourly_status.total_requests   + EXCLUDED.total_requests,
                                status_200_count = COALESCE(f_hourly_status.status_200_count,0) + EXCLUDED.status_200_count,
                                status_400_count = COALESCE(f_hourly_status.status_400_count,0) + EXCLUDED.status_400_count,
                                status_403_count = COALESCE(f_hourly_status.status_403_count,0) + EXCLUDED.status_403_count,
                                status_404_count = COALESCE(f_hourly_status.status_404_count,0) + EXCLUDED.status_404_count,
                                status_408_count = COALESCE(f_hourly_status.status_408_count,0) + EXCLUDED.status_408_count,
                                status_424_count = COALESCE(f_hourly_status.status_424_count,0) + EXCLUDED.status_424_count,
                                status_429_count = COALESCE(f_hourly_status.status_429_count,0) + EXCLUDED.status_429_count,
                                status_500_count = COALESCE(f_hourly_status.status_500_count,0) + EXCLUDED.status_500_count,
                                status_503_count = COALESCE(f_hourly_status.status_503_count,0) + EXCLUDED.status_503_count
                            """,
                            status_rows,
                        )

                # Per-principal usage → f_identity_usage (gap G). Additive
                # upsert like the other invocation-log tables.
                if identity_buckets:
                    id_rows = [
                        (d, a, r, arn, mid, ep,
                         v["total_requests"], v["total_input_tokens"],
                         v["total_output_tokens"], v["failed_requests"])
                        for (d, a, r, arn, mid, ep), v in identity_buckets.items()
                        if v["total_requests"] > 0
                    ]
                    if id_rows:
                        await conn.executemany(
                            """
                            INSERT INTO f_identity_usage (
                                event_date, accountId, region, identity_arn, modelId, endpoint,
                                total_requests, total_input_tokens, total_output_tokens, failed_requests
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                            ON CONFLICT (event_date, accountId, region, identity_arn, modelId, endpoint)
                            DO UPDATE SET
                                total_requests = COALESCE(f_identity_usage.total_requests,0) + EXCLUDED.total_requests,
                                total_input_tokens = COALESCE(f_identity_usage.total_input_tokens,0) + EXCLUDED.total_input_tokens,
                                total_output_tokens = COALESCE(f_identity_usage.total_output_tokens,0) + EXCLUDED.total_output_tokens,
                                failed_requests = COALESCE(f_identity_usage.failed_requests,0) + EXCLUDED.failed_requests
                            """,
                            id_rows,
                        )

        if new_keys:
            await conn.executemany(
                "INSERT INTO ingestion_log_objects (s3_key, row_count, tag_count) "
                "VALUES ($1, $2, $3) ON CONFLICT (s3_key) DO NOTHING",
                new_keys,
            )

        # Retain f_hourly_status for 90 days — the dashboard's date-range picker
        # allows windows up to 90 days, so a shorter retention would silently cap
        # the Status Codes chart (a 7-day cap made every wider filter show the
        # same last-7-days regardless of selection). Keep in sync with the
        # picker's max range in frontend/src/App.jsx (isValidRange: 90 days).
        await conn.execute(
            "DELETE FROM f_hourly_status WHERE event_date < current_date - INTERVAL '90 days'")
        # Same 90-day retention for per-principal usage (gap G).
        await conn.execute(
            "DELETE FROM f_identity_usage WHERE event_date < current_date - INTERVAL '90 days'")

        # f_latency_daily: derive percentiles per (date, model, region, endpoint).
        # Source for the bedrock-mantle endpoint (CW publishes no latency
        # for Mantle); merges with the runtime endpoint where CW already
        # populates this table. We DELETE the slice we own (mantle only,
        # within the window) before inserting so re-runs don't double-count.
        if latency_samples:
            import statistics
            await conn.execute(
                """
                DELETE FROM f_latency_daily
                WHERE endpoint = 'mantle' AND event_date >= $1
                """,
                start.date(),
            )
            lat_rows = []
            for (d, mid, r, ep), samples in latency_samples.items():
                if not samples or ep != "mantle":
                    continue
                samples.sort()
                n = len(samples)
                avg = sum(samples) / n
                # Nearest-rank percentile — fine for the dashboard's needs.
                def _pct(p):
                    idx = max(0, min(n - 1, int(round(p * (n - 1)))))
                    return samples[idx]
                lat_rows.append((
                    d, mid, "__none__", r, ep,
                    n,
                    avg, _pct(0.50), _pct(0.90), _pct(0.99),
                    None, None, None, None,   # ttft not in invocation logs
                ))
            if lat_rows:
                await conn.executemany(
                    """
                    INSERT INTO f_latency_daily (
                        event_date, modelId, traffic_type, region, endpoint, sample_count,
                        avg_e2e, p50_e2e, p90_e2e, p99_e2e,
                        avg_ttft, p50_ttft, p90_ttft, p99_ttft
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT (event_date, modelId, traffic_type, region, endpoint)
                    DO UPDATE SET
                        sample_count = EXCLUDED.sample_count,
                        avg_e2e = EXCLUDED.avg_e2e, p50_e2e = EXCLUDED.p50_e2e,
                        p90_e2e = EXCLUDED.p90_e2e, p99_e2e = EXCLUDED.p99_e2e
                    """,
                    lat_rows,
                )
                print(f"  wrote {len(lat_rows)} latency rows from invocation logs (endpoint='mantle')")

        # Refresh dim_tags from f_daily_tagged
        await conn.execute("DELETE FROM dim_tags")
        await conn.execute("""
            INSERT INTO dim_tags (tag_key, tag_value, first_seen, last_seen, total_requests_30d)
            SELECT tag_key, tag_value, MIN(event_date), MAX(event_date), SUM(total_requests)
            FROM f_daily_tagged
            WHERE event_date >= current_date - INTERVAL '30 days'
            GROUP BY tag_key, tag_value
        """)

        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_invocation_logs_refresh', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
        )

        n_status = await conn.fetchval("SELECT COUNT(*) FROM f_hourly_status")
        print(f"DONE. parsed {total_logs} log lines → {total_tagged_rows} f_daily_tagged rows, "
              f"f_hourly_status now has {n_status} rows.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
