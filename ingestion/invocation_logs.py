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


def _principal_label(arn: str) -> str:
    """Human-readable label for an IAM principal ARN.

    assumed-role/<role>/<session> → "<role>/<session>"  (session carries the
    human login for SSO principals; role carries the workload/team).
    user/<name>                   → "<name>"
    anything else                 → last ARN segment.
    """
    if not arn:
        return "unknown"
    try:
        tail = arn.split(":", 5)[5]          # e.g. assumed-role/X/Y or user/X
    except IndexError:
        return arn
    parts = tail.split("/")
    if parts[0] == "assumed-role" and len(parts) >= 3:
        return f"{parts[1]}/{parts[2]}"
    if parts[0] in ("user", "role") and len(parts) >= 2:
        return parts[-1]
    return tail


def _parse_log_entry(entry: dict) -> tuple[date, int, str, str, str, str, dict, int, int, int, str]:
    """Returns (date, hour, accountId, modelId, region, operation,
              requestMetadata, total_in_tokens, total_out_tokens,
              status_code, principal_arn) or None."""
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
    principal_arn = str((entry.get("identity") or {}).get("arn") or "")
    return d, hr, acct, model_id, region, op, metadata, in_t, out_t, status, principal_arn


async def _ensure_log_objects_table(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_log_objects (
            s3_key       TEXT PRIMARY KEY,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            row_count    BIGINT NOT NULL,
            tag_count    BIGINT NOT NULL DEFAULT 0
        )
    """)


async def _ensure_identity_table(conn: asyncpg.Connection) -> None:
    """Create f_daily_by_identity if missing. Deliberately done here (not
    only in schema.sql): the schema-init Lambda is a CFN custom resource
    that does NOT re-run on an image-only redeploy, so existing stacks
    pick the table up on the next ingest instead."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS f_daily_by_identity (
            event_date          DATE NOT NULL,
            accountId           TEXT NOT NULL,
            modelId             TEXT NOT NULL,
            region              TEXT NOT NULL,
            principal_arn       TEXT NOT NULL,
            principal_label     TEXT NOT NULL DEFAULT '',
            total_requests      BIGINT NOT NULL DEFAULT 0,
            failed_requests     BIGINT NOT NULL DEFAULT 0,
            total_input_tokens  BIGINT NOT NULL DEFAULT 0,
            total_output_tokens BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (event_date, accountId, modelId, region, principal_arn)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_f_daily_identity_arn   ON f_daily_by_identity (principal_arn, event_date)")
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_f_daily_identity_label ON f_daily_by_identity (principal_label, event_date)")


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
    await _ensure_identity_table(conn)

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
                # Per (date, account, model, region, principal_arn) → per-caller
                # aggregates. This is what the "By User" tab reads: the IAM
                # identity is captured automatically on every invocation
                # (identity.arn) — no per-call tagging discipline needed.
                identity_buckets: dict[tuple, dict] = defaultdict(lambda: {
                    "total_requests": 0, "failed_requests": 0,
                    "total_input_tokens": 0, "total_output_tokens": 0,
                })

                for key in pending:
                    obj_rows = 0
                    obj_tag_rows = 0
                    for entry in _read_log_lines(s3, args.bucket, key):
                        parsed = _parse_log_entry(entry)
                        if not parsed:
                            continue
                        d, hr, a, mid, r, op, metadata, in_t, out_t, status, principal_arn = parsed
                        obj_rows += 1
                        # Per-caller tally (skipped when the log has no identity).
                        if principal_arn:
                            ib = identity_buckets[(d, a, mid, r, principal_arn)]
                            ib["total_requests"] += 1
                            if status >= 400:
                                ib["failed_requests"] += 1
                            ib["total_input_tokens"] += in_t
                            ib["total_output_tokens"] += out_t
                        # Real per-code hourly tally (one bump per request).
                        sb = status_buckets[(d, hr, a, mid, r)]
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

                # Per-caller rows → f_daily_by_identity. Additive upsert,
                # same semantics as f_daily_tagged.
                if identity_buckets:
                    id_rows = []
                    for (d, a, mid, r, parn), m in identity_buckets.items():
                        id_rows.append((
                            d, a, mid, r, parn, _principal_label(parn),
                            m["total_requests"], m["failed_requests"],
                            m["total_input_tokens"], m["total_output_tokens"],
                        ))
                    await conn.executemany(
                        """
                        INSERT INTO f_daily_by_identity (
                            event_date, accountId, modelId, region,
                            principal_arn, principal_label,
                            total_requests, failed_requests,
                            total_input_tokens, total_output_tokens
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT (event_date, accountId, modelId, region, principal_arn)
                        DO UPDATE SET
                            principal_label = EXCLUDED.principal_label,
                            total_requests = f_daily_by_identity.total_requests + EXCLUDED.total_requests,
                            failed_requests = f_daily_by_identity.failed_requests + EXCLUDED.failed_requests,
                            total_input_tokens = f_daily_by_identity.total_input_tokens + EXCLUDED.total_input_tokens,
                            total_output_tokens = f_daily_by_identity.total_output_tokens + EXCLUDED.total_output_tokens
                        """,
                        id_rows,
                    )

                # Real per-status-code hourly rows → f_hourly_status. Additive
                # upsert so re-runs over overlapping windows accumulate correctly
                # (matches f_daily_tagged semantics). Only emit hours that had at
                # least one request.
                if status_buckets:
                    status_rows = []
                    for (d, hr, a, mid, r), sc in status_buckets.items():
                        total = sum(sc.values())
                        if total <= 0:
                            continue
                        status_rows.append((
                            d, hr, a, mid, r, total,
                            sc[200], sc[400], sc[403], sc[404], sc[408],
                            sc[424], sc[429], sc[500], sc[503],
                        ))
                    if status_rows:
                        await conn.executemany(
                            """
                            INSERT INTO f_hourly_status (
                                event_date, hour, accountId, modelId, region,
                                total_requests,
                                status_200_count, status_400_count, status_403_count,
                                status_404_count, status_408_count, status_424_count,
                                status_429_count, status_500_count, status_503_count
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                            ON CONFLICT (event_date, hour, accountId, modelId, region)
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

        if new_keys:
            await conn.executemany(
                "INSERT INTO ingestion_log_objects (s3_key, row_count, tag_count) "
                "VALUES ($1, $2, $3) ON CONFLICT (s3_key) DO NOTHING",
                new_keys,
            )

        # Keep f_hourly_status to a rolling 7-day window (matches f_hourly_errors).
        await conn.execute(
            "DELETE FROM f_hourly_status WHERE event_date < current_date - INTERVAL '7 days'")

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
