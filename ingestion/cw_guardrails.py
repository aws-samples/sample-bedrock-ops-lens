#!/usr/bin/env python3
"""
Guardrails CloudWatch metrics ingester (G3 — Compliance tab).

Reads the AWS/Bedrock/Guardrails namespace (exact name verified against
docs.aws.amazon.com/bedrock/latest/userguide/monitoring-guardrails-cw-metrics.html)
and populates f_daily_guardrails.

Dimension model (per docs): GuardrailArn + GuardrailVersion, Operation
(ApplyGuardrail), GuardrailContentSource (Input|Output), GuardrailPolicyType
(only available for InvocationsIntervened and TextUnitCount). Grains that
lack a dimension are stored with the '__all__' sentinel.

Semantics: REPLACE upsert (DO UPDATE SET x = EXCLUDED.x) — this module
re-reads a rolling N-day window on every run, exactly like cw_metrics.py.
Additive would double-count.

The table is self-created (_ensure_guardrails_table): the schema-init CFN
custom resource does not re-run on image-only redeploys.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
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

NAMESPACE = "AWS/Bedrock/Guardrails"

# metric name → (column, stat). Counts are Sum; latency is deliberately
# skipped in v1 (no column — keep the table lean).
METRIC_MAP = {
    "Invocations":           ("invocations", "Sum"),
    "InvocationsIntervened": ("intervened",  "Sum"),
    "TextUnitCount":         ("text_units",  "Sum"),
}


def _cw_client(region: str, session: boto3.Session | None = None):
    s = session or boto3._get_default_session()
    return s.client("cloudwatch", region_name=region,
                    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))


async def _ensure_guardrails_table(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS f_daily_guardrails (
            event_date        DATE NOT NULL,
            accountId         TEXT NOT NULL,
            region            TEXT NOT NULL,
            guardrail_arn     TEXT NOT NULL,
            guardrail_version TEXT NOT NULL DEFAULT '',
            policy_type       TEXT NOT NULL DEFAULT '__all__',
            content_source    TEXT NOT NULL DEFAULT '__all__',
            invocations       BIGINT NOT NULL DEFAULT 0,
            intervened        BIGINT NOT NULL DEFAULT 0,
            text_units        BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY (event_date, accountId, region, guardrail_arn,
                         guardrail_version, policy_type, content_source)
        )
    """)


def _discover(cw) -> list[tuple[str, dict]]:
    """List (metric_name, dims) combos actually published in the namespace."""
    combos: list[tuple[str, dict]] = []
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=NAMESPACE):
        for m in page["Metrics"]:
            name = m["MetricName"]
            if name not in METRIC_MAP:
                continue
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            combos.append((name, dims))
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
    combos = _discover(cw)
    if not combos:
        print(f"  [{account}/{region}] no Guardrails metrics published — skipping")
        return 0
    print(f"  [{account}/{region}] {len(combos)} guardrails metric/dim combos discovered")

    queries, meta = [], {}
    for idx, (name, dims) in enumerate(combos):
        qid = f"g{idx}"
        col, stat = METRIC_MAP[name]
        queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": NAMESPACE, "MetricName": name,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                },
                "Period": 86400, "Stat": stat,
            },
            "ReturnData": True,
        })
        meta[qid] = (col, dims)

    raw = _get_metric_data(cw, queries, start, end)

    # bucket: (date, arn, version, policy_type, content_source) → {col: value}
    buckets: dict[tuple, dict] = defaultdict(dict)
    for qid, (col, dims) in meta.items():
        arn = dims.get("GuardrailArn", "__unknown__")
        ver = dims.get("GuardrailVersion", "")
        ptype = dims.get("GuardrailPolicyType", "__all__")
        csrc = dims.get("GuardrailContentSource", "__all__")
        for ts, v in zip(raw.get(qid, {}).get("timestamps", []),
                         raw.get(qid, {}).get("values", [])):
            d = ts.astimezone(timezone.utc).date()
            key = (d, arn, ver, ptype, csrc)
            buckets[key][col] = buckets[key].get(col, 0) + v

    rows = []
    for (d, arn, ver, ptype, csrc), m in buckets.items():
        rows.append((
            d, account, region, arn, ver, ptype, csrc,
            int(m.get("invocations", 0)), int(m.get("intervened", 0)),
            int(m.get("text_units", 0)),
        ))
    if rows:
        await conn.executemany(
            """
            INSERT INTO f_daily_guardrails (
                event_date, accountId, region, guardrail_arn, guardrail_version,
                policy_type, content_source, invocations, intervened, text_units
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (event_date, accountId, region, guardrail_arn,
                         guardrail_version, policy_type, content_source)
            DO UPDATE SET
                invocations = EXCLUDED.invocations,
                intervened  = EXCLUDED.intervened,
                text_units  = EXCLUDED.text_units
            """,
            rows,
        )
    print(f"  [{account}/{region}] upserted {len(rows)} f_daily_guardrails rows")
    return len(rows)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest AWS/Bedrock/Guardrails CW metrics.")
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
        await _ensure_guardrails_table(conn)
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
    print(f"DONE. {total} guardrails rows.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
