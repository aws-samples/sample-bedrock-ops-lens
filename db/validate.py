#!/usr/bin/env python3
"""
Validation harness — proves the schema math is correct on the seeded database.

Each check is a single SQL query that returns a single boolean (or violation
count). Failures print expected vs. actual and the offending rows. Exit code
is the number of failed checks.

Run AFTER seeding:
    .venv/bin/python db/validate.py

This is the gate before we touch any cloud infrastructure: if the local DB
math doesn't add up, ingestion against real AWS data won't either.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable

try:
    import psycopg
except ImportError:
    print("FATAL: psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(2)


DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)


CHECKS: list[tuple[str, Callable[[psycopg.Cursor], tuple[bool, str]]]] = []


def check(name: str):
    """Decorator: register a function as a validation check."""
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------
@check("f_daily has 30 days of data")
def _(cur):
    cur.execute("SELECT COUNT(DISTINCT event_date) FROM f_daily")
    n = cur.fetchone()[0]
    return (n == 30, f"expected 30 distinct dates, got {n}")


@check("f_daily_tagged has 30 days of data")
def _(cur):
    cur.execute("SELECT COUNT(DISTINCT event_date) FROM f_daily_tagged")
    n = cur.fetchone()[0]
    return (n == 30, f"expected 30 distinct dates, got {n}")


@check("f_hourly_peak has 30 days × 24 hours")
def _(cur):
    cur.execute("SELECT COUNT(DISTINCT (event_date, hour)) FROM f_hourly_peak")
    n = cur.fetchone()[0]
    return (n == 720, f"expected 720 distinct (date, hour), got {n}")


@check("f_hourly_errors covers exactly 7 days (rolling window)")
def _(cur):
    cur.execute("SELECT COUNT(DISTINCT event_date) FROM f_hourly_errors")
    n = cur.fetchone()[0]
    return (n == 7, f"expected 7 distinct dates, got {n}")


# ---------------------------------------------------------------------------
# Generated columns
# ---------------------------------------------------------------------------
@check("f_daily generated year/month/day match event_date")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM f_daily
        WHERE year <> EXTRACT(YEAR FROM event_date)::SMALLINT
           OR month <> EXTRACT(MONTH FROM event_date)::SMALLINT
           OR day <> EXTRACT(DAY FROM event_date)::SMALLINT
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows with mismatched year/month/day")


# ---------------------------------------------------------------------------
# Tag fan-out math — the most important consistency check.
# A request with N tags writes N rows to f_daily_tagged (one per tag),
# so summing across tag_value within a single tag_key MUST equal the
# untagged f_daily total. Drift here means double-counting in cost charts.
# ---------------------------------------------------------------------------
@check("f_daily_tagged: per-tag-key totals are internally consistent")
def _(cur):
    """For a given (date, account, model, region, operation), the total within
    one tag_key bucket should equal the total within any other tag_key bucket
    — every request gets one row per pinned tag, never one + many."""
    cur.execute(
        """
        WITH per_key AS (
          SELECT event_date, accountId, modelId, region, operation, tag_key,
                 SUM(total_requests) AS total
          FROM f_daily_tagged
          GROUP BY event_date, accountId, modelId, region, operation, tag_key
        ),
        spread AS (
          SELECT event_date, accountId, modelId, region, operation,
                 MAX(total) - MIN(total) AS spread
          FROM per_key
          GROUP BY event_date, accountId, modelId, region, operation
        )
        SELECT COUNT(*) FROM spread WHERE spread > 0
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} (date, acct, model, region, op) groups where per-tag totals diverge")


@check("f_daily_tagged: no NULL or empty tag_key/tag_value")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM f_daily_tagged
        WHERE tag_key IS NULL OR tag_key = '' OR tag_value IS NULL OR tag_value = ''
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows with NULL/empty tag dimensions")


@check("dim_tags rolls up to f_daily_tagged 30-day totals exactly")
def _(cur):
    cur.execute(
        """
        WITH expected AS (
          SELECT tag_key, tag_value, SUM(total_requests) AS total
          FROM f_daily_tagged
          WHERE event_date >= current_date - INTERVAL '30 days'
          GROUP BY tag_key, tag_value
        )
        SELECT COUNT(*) FROM dim_tags d
        JOIN expected e USING (tag_key, tag_value)
        WHERE d.total_requests_30d <> e.total
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} dim_tags rows where total_requests_30d disagrees with f_daily_tagged")


# ---------------------------------------------------------------------------
# Status code arithmetic
# ---------------------------------------------------------------------------
@check("f_daily: failed_requests <= total_requests")
def _(cur):
    cur.execute("SELECT COUNT(*) FROM f_daily WHERE failed_requests > total_requests")
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows with failed > total")


@check("f_daily: successful + failed == total")
def _(cur):
    cur.execute(
        "SELECT COUNT(*) FROM f_daily "
        "WHERE successful_requests + failed_requests <> total_requests"
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows where successful + failed != total")


@check("f_hourly_errors: every row has failed_requests > 0")
def _(cur):
    cur.execute("SELECT COUNT(*) FROM f_hourly_errors WHERE failed_requests = 0 OR failed_requests IS NULL")
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows with zero/null failed_requests in error table")


@check("f_hourly_errors: per-code columns sum to failed_requests (honest CW mapping)")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM f_hourly_errors
        WHERE (COALESCE(status_400_count,0) + COALESCE(status_403_count,0)
             + COALESCE(status_429_count,0) + COALESCE(status_500_count,0)
             + COALESCE(status_503_count,0)) <> failed_requests
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows where 429+4xx+5xx != failed_requests")


@check("f_hourly_status: per-code counts sum to total_requests")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM f_hourly_status
        WHERE (COALESCE(status_200_count,0) + COALESCE(status_400_count,0)
             + COALESCE(status_403_count,0) + COALESCE(status_404_count,0)
             + COALESCE(status_408_count,0) + COALESCE(status_424_count,0)
             + COALESCE(status_429_count,0) + COALESCE(status_500_count,0)
             + COALESCE(status_503_count,0)) <> total_requests
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows where sum of status codes != total_requests")


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------
@check("f_latency_daily: percentiles are non-null and ordered (p50 <= p90 <= p99)")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM f_latency_daily
        WHERE p50_e2e IS NULL OR p90_e2e IS NULL OR p99_e2e IS NULL
           OR p50_e2e > p90_e2e OR p90_e2e > p99_e2e
           OR p50_ttft > p90_ttft OR p90_ttft > p99_ttft
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows with bad percentile ordering or nulls")


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------
@check("f_quotas: applied_value >= default_value (you can't have less than default)")
def _(cur):
    cur.execute(
        "SELECT COUNT(*) FROM f_quotas "
        "WHERE applied_value IS NOT NULL AND default_value IS NOT NULL "
        "AND applied_value < default_value"
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} quota rows with applied < default")


# ---------------------------------------------------------------------------
# Partition pruning (proves the optimizer skips irrelevant partitions)
# ---------------------------------------------------------------------------
@check("partition pruning: 7-day query prunes most partitions")
def _(cur):
    """We have 16 monthly partitions plus a default. A 7-day lookback should
    prune the older ones (the planner shows 'Subplans Removed: N'). Future-month
    partitions remain in the plan because Postgres can't prove emptiness without
    stats, but they're cheap. The hard requirement: at least 10 of 17 pruned."""
    cur.execute(
        """
        EXPLAIN (FORMAT TEXT)
        SELECT SUM(total_requests) FROM f_daily
        WHERE event_date >= current_date - 7
        """
    )
    plan = "\n".join(row[0] for row in cur.fetchall())
    import re
    pruned = re.search(r"Subplans Removed:\s+(\d+)", plan)
    n_pruned = int(pruned.group(1)) if pruned else 0
    return (
        n_pruned >= 10,
        f"only {n_pruned} subplans pruned (need >= 10):\n{plan}",
    )


# ---------------------------------------------------------------------------
# UPSERT semantics (re-running ingestion shouldn't duplicate)
# ---------------------------------------------------------------------------
@check("PRIMARY KEY uniqueness: no duplicate keys in f_daily")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT event_date, accountId, modelId, region, operation,
                 traffic_type, service_tier, inference_profile_prefix
          FROM f_daily
          GROUP BY 1,2,3,4,5,6,7,8
          HAVING COUNT(*) > 1
        ) d
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} duplicate primary keys in f_daily")


@check("PRIMARY KEY uniqueness: no duplicate keys in f_daily_tagged")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT event_date, accountId, modelId, region, operation, tag_key, tag_value
          FROM f_daily_tagged
          GROUP BY 1,2,3,4,5,6,7
          HAVING COUNT(*) > 1
        ) d
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} duplicate primary keys in f_daily_tagged")


# ---------------------------------------------------------------------------
# Sentinel hygiene — '__none__' should appear in dimension columns where
# nullable, NEVER in measures or join keys like accountId.
# ---------------------------------------------------------------------------
@check("no '__none__' in identity columns (accountId, modelId, region)")
def _(cur):
    cur.execute(
        """
        SELECT COUNT(*) FROM f_daily
        WHERE accountId = '__none__' OR modelId = '__none__' OR region = '__none__'
        """
    )
    n = cur.fetchone()[0]
    return (n == 0, f"{n} rows with sentinel '__none__' leaking into identity columns")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = ap.parse_args()

    print(f"Validating {args.db_url}\n")
    failed = 0
    with psycopg.connect(args.db_url) as conn:
        with conn.cursor() as cur:
            for name, fn in CHECKS:
                try:
                    ok, msg = fn(cur)
                except Exception as e:
                    ok, msg = False, f"EXCEPTION: {e}"
                status = "PASS" if ok else "FAIL"
                marker = " " if ok else "✗"
                print(f"  [{status}] {marker} {name}")
                if not ok:
                    print(f"          → {msg}")
                    failed += 1

    print()
    if failed:
        print(f"FAILED: {failed}/{len(CHECKS)} checks did not pass")
        return failed
    print(f"PASSED: all {len(CHECKS)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
