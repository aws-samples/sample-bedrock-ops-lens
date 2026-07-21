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


async def _order_accounts(conn: asyncpg.Connection, accts: list) -> list:
    """Sort accounts so the ones with real Bedrock traffic come FIRST.

    At org scale the Service Quotas API rate-limits hard and a single quotas
    pass can run for 10+ minutes, so the Lambda may time out before covering
    every (account, region). Whichever accounts we process first are the ones
    whose util% actually renders. Accounts that appear in f_hourly_peak /
    f_daily are the ones with traffic (and therefore the only ones where a
    quota util% is even meaningful), so we front-load them. Idle accounts
    still get covered — just after the ones that matter, and across successive
    runs via the resumable skip in main(). This keeps full multi-account
    support while guaranteeing the util-critical accounts never starve.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT accountId, SUM(cnt)::BIGINT AS cnt FROM (
                SELECT accountId, COUNT(*) AS cnt FROM f_hourly_peak GROUP BY accountId
                UNION ALL
                SELECT accountId, COUNT(*) AS cnt FROM f_daily GROUP BY accountId
            ) t GROUP BY accountId
            """
        )
        weight = {str(r["accountid"]): int(r["cnt"] or 0) for r in rows}
    except Exception:
        weight = {}
    # Stable sort: higher traffic weight first, then preserve discovery order.
    return sorted(accts, key=lambda m: -weight.get(str(m.accountId), 0))


async def _recent_refresh(conn: asyncpg.Connection, stale_hours: int) -> set[tuple[str, str]]:
    """Return {(accountId, region)} refreshed within the last `stale_hours`.

    Lets a rate-limited quotas pass RESUME across scheduled runs: each run
    skips (account, region) pairs already refreshed recently, so successive
    runs make forward progress and the whole org gets covered instead of every
    run dying at the same throttled spot. A daily full refresh is plenty fresh
    for quota limits, which change rarely.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT accountId, region FROM f_quotas
            WHERE last_refreshed_at > now() - ($1::text || ' hours')::interval
            GROUP BY accountId, region
            """,
            str(stale_hours),
        )
        return {(str(r["accountid"]), str(r["region"])) for r in rows}
    except Exception:
        return set()


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
    ap.add_argument("--stale-hours", type=int, default=20,
                    help="skip (account, region) pairs refreshed within this many "
                         "hours so a rate-limited pass resumes across runs (0=always refresh)")
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
        # Traffic-first ordering so accounts whose util% actually renders get
        # covered before the pass risks timing out; recently-refreshed pairs
        # are skipped so a throttled run resumes rather than restarting.
        accts = await _order_accounts(conn, accts)
        recent = await _recent_refresh(conn, args.stale_hours) if args.stale_hours > 0 else set()
        if recent:
            print(f"  resumable: skipping {len(recent)} (account, region) pair(s) "
                  f"refreshed within {args.stale_hours}h", flush=True)

        total = 0
        skipped = 0
        for monitored in accts:
            acct = monitored.accountId
            # Skip whole account only if EVERY target region is fresh.
            if all((acct, r) in recent for r in regions):
                skipped += len(regions)
                continue
            try:
                session = session_for(acct, role_name=args.role_name,
                                      external_id=args.external_id)
            except Exception as e:
                msg = f"sts:AssumeRole failed: {type(e).__name__}: {e}"
                print(f"  [{acct}] SKIP — {msg}", flush=True)
                failures.append((acct, "*", msg))
                continue

            for region in regions:
                if (acct, region) in recent:
                    skipped += 1
                    continue
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
            print(f"\nDONE with {len(failures)} failure(s); {total} rows ingested, "
                  f"{skipped} pair(s) skipped as fresh.")
            for acct, region, msg in failures:
                print(f"  [{acct}/{region}] {msg}")
            return 1
        print(f"DONE. {total} quota rows; {skipped} pair(s) skipped as fresh.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
