#!/usr/bin/env python3
"""
AgentCore CloudWatch metrics ingester (G2 — Agents & MCP tab).

Reads the AWS/Bedrock-AgentCore namespace (service-provided metrics:
Runtime, Gateway, Memory, built-in Tools) plus the `bedrock-agentcore`
EMF namespace (ADOT-instrumented agent custom metrics), and populates
f_daily_agentcore.

Verified metric names (docs, 2026-07): Runtime → Invocations, SessionCount,
ActiveSessionCount, Latency, Throttles, SystemErrors, UserErrors.
Gateway → per-tool breakdown via the `Name` dimension, target type via
`TargetType` (MCP|Lambda|OpenAPI), plus TargetExecutionTime.

Generic metric-per-row schema: AgentCore is a young service whose metric
set moves fast; a column per metric would need an ALTER per launch.
Stat map is explicit (counts → Sum, latency → p50/p99/avg) — never blindly
percentile a count metric.

Semantics: REPLACE upsert (rolling window re-read, like cw_metrics.py).
Table self-created (schema-init custom resource doesn't re-run on
image-only redeploys).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import boto3
from botocore.config import Config

from .accounts import _add_common_args, discover_accounts, session_for

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

NAMESPACES = ["AWS/Bedrock-AgentCore", "bedrock-agentcore"]

# metric name → list of stats to pull. Explicit map: counts get Sum,
# latency gets percentiles. Unknown metric names default to Sum only.
LATENCY_RE = re.compile(r"latency|duration|time", re.IGNORECASE)
COUNT_STATS = ["Sum"]
LATENCY_STATS = ["Average", "p50", "p99"]

def _cw_client(region: str, session: boto3.Session | None = None):
    s = session or boto3._get_default_session()
    return s.client("cloudwatch", region_name=region,
                    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))


async def _ensure_agentcore_table(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS f_daily_agentcore (
            event_date      DATE NOT NULL,
            accountId       TEXT NOT NULL,
            region          TEXT NOT NULL,
            namespace       TEXT NOT NULL,
            resource_type   TEXT NOT NULL,
            resource_id     TEXT NOT NULL,
            metric_name     TEXT NOT NULL,
            stat            TEXT NOT NULL,
            value           DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (event_date, accountId, region, namespace,
                         resource_type, resource_id, metric_name, stat)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_f_daily_agentcore_res "
        "ON f_daily_agentcore (resource_type, resource_id, event_date)")


def _classify(dims: dict) -> tuple[str, str]:
    """(resource_type, resource_id) from a metric's dimension set.

    ARN values identify the resource directly — dimension NAMES vary
    (Resource/Service/Name all may carry the ARN), so classify by value.
    """
    for v in dims.values():
        for marker, rtype in ((":runtime/", "runtime"),
                              (":gateway/", "gateway"),
                              (":memory/", "memory")):
            if marker in v:
                return rtype, v.split(marker, 1)[1]
    if dims.get("TargetType"):
        return "tool", dims.get("Name", dims["TargetType"])
    for k in ("Operation", "AggregateOperation", "ItemType"):
        if k in dims:
            return "operation", dims[k]
    if dims:
        k = sorted(dims.keys())[0]
        return "other", f"{k}={dims[k]}"
    return "account", "__all__"


def _discover(cw, namespace: str) -> list[tuple[str, dict]]:
    combos: list[tuple[str, dict]] = []
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=namespace):
        for m in page["Metrics"]:
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            combos.append((m["MetricName"], dims))
    return combos


def _get_metric_data(cw, queries: list[dict], start: datetime, end: datetime) -> dict[str, dict]:
    out: dict[str, dict] = {}
    CHUNK = 500
    for i in range(0, len(queries), CHUNK):
        batch = queries[i:i + CHUNK]
        next_token = None
        while True:
            kwargs: dict[str, Any] = dict(
                StartTime=start, EndTime=end,
                MetricDataQueries=batch, ScanBy="TimestampAscending",
            )
            if next_token:
                kwargs["NextToken"] = next_token
            resp = cw.get_metric_data(**kwargs)
            for r in resp["MetricDataResults"]:
                cur = out.setdefault(r["Id"], {"timestamps": [], "values": []})
                cur["timestamps"].extend(r["Timestamps"])
                cur["values"].extend(r["Values"])
            next_token = resp.get("NextToken")
            if not next_token:
                break
    return out


async def _ingest_target(conn: asyncpg.Connection, account: str, region: str,
                          session, start: datetime, end: datetime) -> int:
    cw = _cw_client(region, session)
    # Wipe-and-reload the window for this (account, region): the REPLACE
    # semantics re-read the whole window anyway, and this self-heals rows
    # whose classification changed between versions (same precedent as
    # cw_metrics' f_hourly_errors full reload).
    await conn.execute(
        "DELETE FROM f_daily_agentcore WHERE accountId=$1 AND region=$2 AND event_date >= $3",
        account, region, start.date())
    rows_total = 0
    for namespace in NAMESPACES:
        combos = _discover(cw, namespace)
        if not combos:
            print(f"  [{account}/{region}] {namespace}: no metrics — skipping")
            continue
        print(f"  [{account}/{region}] {namespace}: {len(combos)} metric/dim combos")

        queries, meta = [], {}
        qn = 0
        for name, dims in combos:
            stats = LATENCY_STATS if LATENCY_RE.search(name) else COUNT_STATS
            for stat in stats:
                qid = f"a{qn}"; qn += 1
                queries.append({
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace, "MetricName": name,
                            "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                        },
                        "Period": 86400, "Stat": stat,
                    },
                    "ReturnData": True,
                })
                meta[qid] = (name, stat, dims)

        raw = _get_metric_data(cw, queries, start, end)

        rows = []
        for qid, (name, stat, dims) in meta.items():
            rtype, rid = _classify(dims)
            for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                             raw.get(qid, {}).get("values", [])):
                d = ts.astimezone(timezone.utc).date()
                rows.append((d, account, region, namespace, rtype, rid,
                             name, stat.lower(), float(v)))
        if rows:
            await conn.executemany(
                """
                INSERT INTO f_daily_agentcore (
                    event_date, accountId, region, namespace,
                    resource_type, resource_id, metric_name, stat, value
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (event_date, accountId, region, namespace,
                             resource_type, resource_id, metric_name, stat)
                DO UPDATE SET value = EXCLUDED.value
                """,
                rows,
            )
            rows_total += len(rows)
    print(f"  [{account}/{region}] upserted {rows_total} f_daily_agentcore rows")
    return rows_total


async def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest AgentCore CW metrics.")
    _add_common_args(ap)
    ap.add_argument("--regions", default="")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    accts = discover_accounts(args)
    if not accts:
        print("ERROR: no monitored accounts resolved", file=sys.stderr)
        return 2
    if args.regions:
        regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    else:
        try:
            from .config import load_config
            regions = load_config().resolved_regions()
        except Exception:
            regions = ["us-east-1"]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    conn = await asyncpg.connect(args.db_url)
    total = 0
    try:
        await _ensure_agentcore_table(conn)
        for monitored in accts:
            acct = monitored.accountId
            try:
                session = session_for(acct, role_name=args.role_name,
                                      external_id=args.external_id)
            except Exception as e:
                print(f"  [{acct}] SKIP — sts:AssumeRole failed: {e}")
                continue
            for region in regions:
                try:
                    total += await _ingest_target(conn, acct, region,
                                                   session, start, end)
                except Exception as e:
                    print(f"  [{acct}/{region}] ERROR: {type(e).__name__}: {e}")
    finally:
        await conn.close()
    print(f"DONE. {total} agentcore rows.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
