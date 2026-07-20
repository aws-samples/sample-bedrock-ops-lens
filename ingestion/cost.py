#!/usr/bin/env python3
"""
Cost Explorer ingester for Bedrock Ops Lens.

Calls AWS Cost Explorer GetCostAndUsage from the CENTRAL account (the
management/payer account) once per refresh, grouped by LINKED_ACCOUNT and
SERVICE, daily granularity, UnblendedCost. Filters to Bedrock-related
services so the chart isn't drowned in unrelated AWS spend.

Why central-only:
  - The management account naturally sees all linked-account billing.
  - One API call covers the whole org instead of N (CE charges $0.01/call).
  - Member accounts often don't have CE enabled at all; the central path
    Just Works.

CE has two important quirks the schema absorbs:
  - 24-48h lag — today's cost surfaces tomorrow or the day after.
  - The CE API itself is only available in us-east-1 regardless of where
    the cost was incurred. The `region` column in f_daily_cost is filled
    in only when CE returns regional grouping (it doesn't, by default,
    because the LINKED_ACCOUNT × SERVICE breakdown is what we want).

Usage:
    python -m ingestion.cost --days 30
    python -m ingestion.cost --days 30 --regions us-east-1   # CE region; ignored otherwise
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .accounts import session_for, session_cache
from .config import load_config

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)


# Cost Explorer is GA only in us-east-1. The `region` flag below is for
# config-parity with other ingesters; CE itself ignores it.
CE_API_REGION = "us-east-1"


# Bedrock spend in CE shows up under several service names — the parent
# "Amazon Bedrock" line item plus per-model SaaS lines like
# "Claude Opus 4 (Amazon Bedrock Edition)". Filter to anything Bedrock-related.
BEDROCK_SERVICE_NAME_FILTER = {
    "Dimensions": {
        "Key": "SERVICE",
        # Substring matches against the SERVICE dimension value. "Bedrock"
        # catches both the parent service and the per-model SaaS lines.
        "Values": [
            "Amazon Bedrock",
            "Amazon Bedrock AgentCore",
        ],
        "MatchOptions": ["EQUALS"],
    },
}

# We can't use a single Dimensions filter for substring matching — CE only
# offers EQUALS / CASE_SENSITIVE / CASE_INSENSITIVE. The reliable way to
# capture all "Claude X (Amazon Bedrock Edition)" services is to list them
# explicitly. We expand at runtime by listing distinct services first, then
# filtering ones that contain "Bedrock Edition" or are exactly "Amazon Bedrock".


def _ce_client(session: boto3.Session | None = None):
    s = session or boto3._get_default_session() or boto3.Session()
    return s.client(
        "ce",
        region_name=CE_API_REGION,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def _list_bedrock_services(ce, start: date, end: date) -> list[str]:
    """Discover the full set of Bedrock-related service names that show up
    in CE for the given window. Returns the SERVICE dimension values to
    pass into the subsequent GetCostAndUsage call."""
    out: set[str] = set()
    next_token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Dimension="SERVICE",
            Context="COST_AND_USAGE",
            MaxResults=1000,
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_dimension_values(**kwargs)
        for entry in resp.get("DimensionValues", []) or []:
            v = entry.get("Value", "")
            if not v:
                continue
            if v == "Amazon Bedrock" or "Bedrock" in v:
                out.add(v)
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return sorted(out)


def _fetch_cost(
    ce,
    start: date,
    end: date,
    service_filter: list[str],
    only_accounts: list[str] | None = None,
) -> list[dict]:
    """Issue GetCostAndUsage calls grouping by LINKED_ACCOUNT + SERVICE.
    Returns a flat list of {date, accountId, service, total_cost, currency}."""
    if not service_filter:
        return []

    flt = {
        "Dimensions": {
            "Key": "SERVICE",
            "Values": service_filter,
            "MatchOptions": ["EQUALS"],
        },
    }
    if only_accounts:
        flt = {
            "And": [
                flt,
                {
                    "Dimensions": {
                        "Key": "LINKED_ACCOUNT",
                        "Values": only_accounts,
                        "MatchOptions": ["EQUALS"],
                    },
                },
            ],
        }

    rows: list[dict] = []
    next_token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
            Filter=flt,
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []) or []:
            d = date.fromisoformat(period["TimePeriod"]["Start"])
            for grp in period.get("Groups", []) or []:
                keys = grp.get("Keys", []) or []
                if len(keys) < 2:
                    continue
                acct, svc = keys[0], keys[1]
                amt_obj = (grp.get("Metrics") or {}).get("UnblendedCost") or {}
                amt = float(amt_obj.get("Amount") or 0)
                cur = amt_obj.get("Unit") or "USD"
                if amt <= 0:
                    continue
                rows.append({
                    "event_date": d,
                    "accountId": acct,
                    "service": svc,
                    "total_cost": amt,
                    "currency": cur,
                })
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return rows


def _fetch_cost_by_usage_type(
    ce,
    start: date,
    end: date,
    service_filter: list[str],
    only_accounts: list[str] | None = None,
) -> list[dict]:
    """Second pass: real billed dollars per (account, service, USAGE_TYPE).

    Motivation: for composite services like Amazon Bedrock AgentCore, the
    SERVICE-level number hides the split (Runtime vCPU vs Memory vs
    BrowserTool vs Evaluations...). The usage type IS the billed line item
    — no estimation, straight from Cost Explorer.
    """
    if not service_filter:
        return []
    flt = {"Dimensions": {"Key": "SERVICE", "Values": service_filter,
                          "MatchOptions": ["EQUALS"]}}
    if only_accounts:
        flt = {"And": [flt, {"Dimensions": {"Key": "LINKED_ACCOUNT",
                                             "Values": only_accounts,
                                             "MatchOptions": ["EQUALS"]}}]}
    rows: list[dict] = []
    next_token = None
    while True:
        kwargs = dict(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
            ],
            Filter=flt,
        )
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []) or []:
            d = date.fromisoformat(period["TimePeriod"]["Start"])
            for grp in period.get("Groups", []) or []:
                keys = grp.get("Keys", []) or []
                if len(keys) < 2:
                    continue
                svc, usage_type = keys[0], keys[1]
                m = grp.get("Metrics") or {}
                amt = float((m.get("UnblendedCost") or {}).get("Amount") or 0)
                qty = float((m.get("UsageQuantity") or {}).get("Amount") or 0)
                if amt <= 0:
                    continue
                rows.append({
                    "event_date": d, "service": svc, "usage_type": usage_type,
                    "total_cost": amt, "usage_qty": qty,
                })
        next_token = resp.get("NextPageToken")
        if not next_token:
            break
    return rows


async def _ensure_usage_type_table(conn: asyncpg.Connection) -> None:
    """Self-created (schema-init custom resource doesn't re-run on
    image-only redeploys)."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS f_daily_cost_usage_type (
            event_date  DATE NOT NULL,
            service     TEXT NOT NULL,
            usage_type  TEXT NOT NULL,
            total_cost  DOUBLE PRECISION NOT NULL DEFAULT 0,
            usage_qty   DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY (event_date, service, usage_type)
        )
    """)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest Bedrock spend from AWS Cost Explorer into f_daily_cost.",
    )
    ap.add_argument("--days", type=int, default=30,
                    help="lookback window (CE max ~365). Default 30.")
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    ap.add_argument("--accounts",
                    help="comma-separated account IDs to filter to "
                         "(default: every active account in the org)")
    args = ap.parse_args()

    end = date.today()
    start = end - timedelta(days=args.days - 1)
    # CE's `End` is exclusive — bump by one day so today is included.
    end_excl = end + timedelta(days=1)

    print(f"Cost Explorer: window {start} to {end} (inclusive), "
          f"CE region={CE_API_REGION}")

    # 1) Service discovery — figure out which Bedrock services to filter on.
    ce = _ce_client()
    print("[1/3] discovering Bedrock service names in CE...")
    services = _list_bedrock_services(ce, start, end_excl)
    print(f"      found {len(services)} services:")
    for s in services:
        print(f"        - {s}")
    if not services:
        print("      (no Bedrock spend in this window — nothing to do)")
        return 0

    # 2) Decide which accounts to scope to.
    only_accounts: list[str] | None = None
    if args.accounts:
        only_accounts = [a.strip() for a in args.accounts.split(",")
                         if a.strip().isdigit() and len(a.strip()) == 12]
    else:
        # Use config to scope. mode=discover-org → no filter (CE will return
        # every linked account in this billing family). mode=explicit → use
        # the configured list. mode=single → just the running account.
        try:
            cfg = load_config()
            if cfg.monitored_accounts.mode == "explicit" and cfg.monitored_accounts.ids:
                only_accounts = list(cfg.monitored_accounts.ids)
            elif cfg.monitored_accounts.mode == "single":
                only_accounts = [session_cache().self_account]
            # discover-org → leave unfiltered, CE returns the whole org
        except Exception:
            pass

    # 3) Fetch + upsert.
    print("[2/3] fetching daily cost data...")
    rows = _fetch_cost(ce, start, end_excl, services, only_accounts=only_accounts)
    print(f"      got {len(rows)} non-zero (date, account, service) rows")

    print(f"[3/3] upserting into f_daily_cost...")
    ut_rows = _fetch_cost_by_usage_type(ce, start, end_excl, services, only_accounts)
    print(f"      + {len(ut_rows)} usage-type rows (real billed line items)")
    conn = await asyncpg.connect(args.db_url)
    try:
        await _ensure_usage_type_table(conn)
        if ut_rows:
            await conn.executemany(
                """
                INSERT INTO f_daily_cost_usage_type
                    (event_date, service, usage_type, total_cost, usage_qty)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (event_date, service, usage_type) DO UPDATE SET
                    total_cost = EXCLUDED.total_cost,
                    usage_qty  = EXCLUDED.usage_qty
                """,
                [(r["event_date"], r["service"], r["usage_type"],
                  r["total_cost"], r["usage_qty"]) for r in ut_rows],
            )
        await conn.executemany(
            """
            INSERT INTO f_daily_cost
                (event_date, accountId, service, region, total_cost, currency)
            VALUES ($1, $2, $3, '__none__', $4, $5)
            ON CONFLICT (event_date, accountId, service, region) DO UPDATE SET
                total_cost = EXCLUDED.total_cost,
                currency   = EXCLUDED.currency
            """,
            [(r["event_date"], r["accountId"], r["service"],
              r["total_cost"], r["currency"]) for r in rows],
        )
        await conn.execute(
            """
            INSERT INTO ingestion_meta (key, value, updated_at)
            VALUES ('last_cost_refresh', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
            """,
            datetime.now(timezone.utc).isoformat(),
        )
    finally:
        await conn.close()

    if rows:
        total = sum(r["total_cost"] for r in rows)
        cur = rows[0]["currency"]
        print(f"DONE. Total spend in window: {total:,.2f} {cur}")
    else:
        print("DONE. No spend in window.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
