#!/usr/bin/env python3
"""
Service Quotas ingester for Bedrock Ops Lens.

Calls service-quotas:ListServiceQuotas (applied) and ListAWSDefaultServiceQuotas
(default) for the bedrock service in each region, parses the RPM/TPM quota
names with the spec regex, and upserts into f_quotas with both default_value
and applied_value. The applied_value reflects any post-quota-increase
the customer has received.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
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

PATTERN = re.compile(
    r"(On-demand|Cross-region|Global cross-region) model inference (requests|tokens) per minute for (.+)"
)


def _sq_client(region: str, session: boto3.Session | None = None):
    s = session or boto3._get_default_session()
    return s.client(
        "service-quotas",
        region_name=region,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_all(client, op: str, **kwargs) -> list[dict]:
    """Paginate any list API."""
    paginator = client.get_paginator(op)
    out: list[dict] = []
    for page in paginator.paginate(**kwargs):
        # Service Quotas returns 'Quotas' in both APIs
        out.extend(page.get("Quotas", []))
    return out


def _parse(q: dict) -> dict | None:
    name = q.get("QuotaName", "")
    m = PATTERN.match(name)
    if not m:
        return None
    return {
        "quota_code":  q["QuotaCode"],
        "quota_name":  name,
        "model_name":  m.group(3),
        "traffic_type": m.group(1),
        "metric":      "RPM" if m.group(2) == "requests" else "TPM",
        "value":       float(q.get("Value", 0) or 0),
        "adjustable":  bool(q.get("Adjustable", False)),
    }


def _fetch_quotas_for_region(region: str, session: boto3.Session | None = None) -> list[tuple]:
    """Returns rows ready for INSERT. Joins applied + default by quota_code."""
    sq = _sq_client(region, session=session)

    applied = {q["QuotaCode"]: q for q in _list_all(sq, "list_service_quotas",
                                                     ServiceCode="bedrock")}
    default = {q["QuotaCode"]: q for q in _list_all(sq, "list_aws_default_service_quotas",
                                                     ServiceCode="bedrock")}

    rows: list[tuple] = []
    all_codes = set(applied.keys()) | set(default.keys())
    for code in all_codes:
        a = applied.get(code)
        d = default.get(code)
        # Prefer applied for parsing (it always has the live name).
        parsed = _parse(a or d or {})
        if not parsed:
            continue
        applied_v = float((a or {}).get("Value", 0)) if a else None
        default_v = float((d or {}).get("Value", 0)) if d else None
        rows.append((
            parsed["quota_code"],
            parsed["quota_name"],
            parsed["model_name"],
            parsed["traffic_type"],
            parsed["metric"],
            default_v,
            applied_v,
            parsed["adjustable"],
            region,
        ))
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest Bedrock Service Quotas (default + applied) into f_quotas.",
    )
    _add_common_args(ap)
    ap.add_argument("--regions", default="",
                    help="comma-separated AWS regions; defaults to config.yaml monitored_regions")
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

    conn = await asyncpg.connect(args.db_url)
    failures: list[tuple[str, str, str]] = []
    try:
        total = 0
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
                    rows = _fetch_quotas_for_region(region, session=session)
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    print(f"  [{acct}/{region}] ERROR — {msg}", flush=True)
                    failures.append((acct, region, msg))
                    continue
                # Bind accountId column
                bound = [(acct, *r) for r in rows]
                if bound:
                    await conn.executemany(
                        """
                        INSERT INTO f_quotas (
                            accountId, quota_code, quota_name, model_name,
                            traffic_type, metric, default_value, applied_value,
                            adjustable, region
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT (accountId, region, quota_code) DO UPDATE SET
                            quota_name = EXCLUDED.quota_name,
                            model_name = EXCLUDED.model_name,
                            traffic_type = EXCLUDED.traffic_type,
                            metric = EXCLUDED.metric,
                            default_value = EXCLUDED.default_value,
                            applied_value = EXCLUDED.applied_value,
                            adjustable = EXCLUDED.adjustable,
                            last_refreshed_at = now()
                        """,
                        bound,
                    )
                print(f"  [{acct}/{region}] {len(bound)} quotas")
                total += len(bound)

        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_quotas_refresh', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
        )
        if failures:
            print(f"\nDONE with {len(failures)} failure(s); {total} rows ingested.")
            for acct, region, msg in failures:
                print(f"  [{acct}/{region}] {msg}")
            return 1
        print(f"DONE. {total} quota rows.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
