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


def _parse_log_entry(entry: dict) -> tuple[date, str, str, str, str, dict, int, int, int]:
    """Returns (date, accountId, modelId, region, operation, requestMetadata,
              total_in_tokens, total_out_tokens, status_code) or None."""
    ts = entry.get("timestamp")
    acct = entry.get("accountId", "")
    region = entry.get("region", "")
    op = entry.get("operation", "")
    model_id = entry.get("modelId", "")
    if not (ts and acct and region and op and model_id):
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (ValueError, AttributeError):
        return None
    metadata = entry.get("requestMetadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    in_t = int((entry.get("input") or {}).get("inputTokenCount") or 0)
    out_t = int((entry.get("output") or {}).get("outputTokenCount") or 0)
    err = entry.get("errorCode") or entry.get("error")
    status = 200 if not err else (
        429 if "Throttl" in str(err) else
        500 if "Server" in str(err) or "Internal" in str(err) else
        400
    )
    return d, acct, model_id, region, op, metadata, in_t, out_t, status


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

                for key in pending:
                    obj_rows = 0
                    obj_tag_rows = 0
                    for entry in _read_log_lines(s3, args.bucket, key):
                        parsed = _parse_log_entry(entry)
                        if not parsed:
                            continue
                        d, a, mid, r, op, metadata, in_t, out_t, status = parsed
                        obj_rows += 1
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

        if new_keys:
            await conn.executemany(
                "INSERT INTO ingestion_log_objects (s3_key, row_count, tag_count) "
                "VALUES ($1, $2, $3) ON CONFLICT (s3_key) DO NOTHING",
                new_keys,
            )

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

        print(f"DONE. parsed {total_logs} log lines → {total_tagged_rows} f_daily_tagged rows.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
