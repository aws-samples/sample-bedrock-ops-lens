#!/usr/bin/env python3
"""
Synthetic data seeder for Bedrock Ops Lens.

Populates a local Postgres with realistic-shaped fake data so the dashboard
and validation harness can exercise every endpoint without needing real AWS
data. Runs in seconds. Idempotent: TRUNCATEs all fact tables first.

Usage:
    pip install psycopg[binary]
    python db/seed.py
    # or with custom connection:
    python db/seed.py --db-url postgresql://user:pass@host:5432/db
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import date, timedelta

try:
    import psycopg
    from psycopg import sql
except ImportError:
    print("FATAL: psycopg not installed. Run: pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(2)


DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens",
)

# ---------------------------------------------------------------------------
# Fixture cardinalities — keep small so seeding is fast but varied enough
# to exercise GROUP BY / TOP-N / "Other" bucketing in every endpoint.
# ---------------------------------------------------------------------------
DAYS = 30
ACCOUNTS = [
    "111111111111",
    "222222222222",
    "333333333333",
    "444444444444",
    "555555555555",
]
REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]
MODELS = [
    "anthropic.claude-opus-4-1-20250805-v1:0",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "amazon.nova-pro-v1:0",
    "amazon.nova-lite-v1:0",
    "meta.llama3-3-70b-instruct-v1:0",
    "cohere.command-r-plus-v1:0",
]
OPERATIONS = ["InvokeModel", "Converse", "InvokeModelWithResponseStream", "ConverseStream"]
TRAFFIC_TYPES = [
    "ON_DEMAND_INFERENCE_REQUEST",
    "CROSS_REGION_OD_INFERENCE_REQUEST",
    "SOURCE_REGION_OD_INFERENCE_REQUEST",
    "PROVISIONED_THROUGHPUT_V1",
]
SERVICE_TIERS = ["default", "flex", "priority"]
PROFILE_PREFIXES = ["us", "eu", "global", "apac"]

# Tag dimensions — what customers will see in the new top-bar dropdowns.
TAGS = {
    "team": ["platform", "ml-research", "consumer-app", "billing", "support-bot"],
    "environment": ["prod", "staging", "dev"],
    "business_unit": ["retail", "finance", "consumer", "enterprise"],
    "application": ["chatbot", "summarizer", "code-review", "rag-pipeline", "analytics"],
}

QUOTA_TRAFFIC_TYPES = ["On-demand", "Cross-region", "Global cross-region"]
QUOTA_METRICS = ["RPM", "TPM"]


# ---------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------
def hourly_curve(hour: int) -> float:
    """Returns a 0..1 multiplier shaped like a workday (peak 9-17 UTC)."""
    # Smooth bell centered at 13:00 with low floor.
    return 0.15 + 0.85 * math.exp(-((hour - 13) ** 2) / 28.0)


def weekday_curve(d: date) -> float:
    """0.4 on weekends, 1.0 on weekdays."""
    return 0.4 if d.weekday() >= 5 else 1.0


def model_size_factor(model_id: str) -> float:
    """Larger models = lower volume (mirrors typical fleet shape)."""
    if "opus" in model_id:
        return 0.15
    if "sonnet" in model_id:
        return 0.55
    if "haiku" in model_id or "lite" in model_id:
        return 1.0
    if "nova-pro" in model_id:
        return 0.35
    return 0.40


def status_split(total: int, throttle_rate: float, error_rate: float) -> tuple[int, int, int, int, int, int]:
    """Returns (failed, s400, s403, s429, s500, s503) in the HONEST CloudWatch
    shape used by f_daily / f_hourly_errors. CloudWatch gives three trustworthy
    counters: all-4xx, all-5xx, and real throttles (InvocationThrottles). So:
      s429 = real throttle count, s400 = remaining non-throttle 4xx aggregate,
      s500 = all-5xx aggregate; s403/s503 stay 0 (indistinguishable from CW).
    The genuine per-code split (403/404/408/424/503) lives in f_hourly_status,
    seeded from the invocation-log model — see seed_hourly_status."""
    c4xx = int(total * throttle_rate)             # throttles dominate 4xx for Bedrock
    c5xx = int(total * error_rate)
    s429 = int(c4xx * rng_uniform_throttle())     # most 4xx are throttles
    non_throttle_4xx = max(0, c4xx - s429)
    failed = c4xx + c5xx
    return failed, non_throttle_4xx, 0, s429, c5xx, 0


def rng_uniform_throttle() -> float:
    """Fraction of 4xx that are throttles in synthetic data. Module-level RNG
    isn't threaded here, so use a fixed realistic ratio (most 4xx = 429)."""
    return 0.85


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def seed_fact_daily(cur, today: date, rng: random.Random) -> int:
    """Generate f_daily rows: every day × account × model × region × op × traffic × tier × profile."""
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        wd_mult = weekday_curve(d)
        for acct in ACCOUNTS:
            acct_factor = 0.3 + (int(acct[:3]) % 7) / 10
            for model in MODELS:
                m_factor = model_size_factor(model)
                for region in REGIONS:
                    region_mult = {"us-east-1": 1.0, "us-west-2": 0.7, "eu-west-1": 0.4, "ap-southeast-1": 0.25}[region]
                    # Don't generate every operation × traffic_type combo — keep cardinality realistic.
                    op_choices = rng.sample(OPERATIONS, k=rng.choice([2, 3, 4]))
                    tt_choices = rng.sample(TRAFFIC_TYPES, k=rng.choice([1, 2, 3]))
                    for op in op_choices:
                        for tt in tt_choices:
                            tier = rng.choice(SERVICE_TIERS)
                            prefix = rng.choice(PROFILE_PREFIXES)
                            base = 5000 * acct_factor * m_factor * region_mult * wd_mult
                            jitter = rng.uniform(0.6, 1.4)
                            total = max(1, int(base * jitter))
                            throttle_rate = rng.uniform(0.0, 0.04) if "PROVISIONED" not in tt else 0.0
                            error_rate = rng.uniform(0.001, 0.01)
                            failed, s400, s403, s429, s500, s503 = status_split(
                                total, throttle_rate, error_rate
                            )
                            successful = total - failed
                            in_tok_per = rng.randint(800, 4000)
                            out_tok_per = rng.randint(200, 1500)
                            in_tok = total * in_tok_per
                            out_tok = total * out_tok_per
                            cache_read = int(in_tok * rng.uniform(0.0, 0.4))
                            cache_write = int(in_tok * rng.uniform(0.0, 0.05))
                            rows.append((
                                d, acct, model, region, op, tt, tier, prefix,
                                total, successful, failed, in_tok, out_tok,
                                cache_read, cache_write,
                                s400, s403, s429, s500, s503,
                            ))
    cur.executemany(
        """
        INSERT INTO f_daily (
            event_date, accountId, modelId, region, operation, traffic_type,
            service_tier, inference_profile_prefix,
            total_requests, successful_requests, failed_requests,
            total_input_tokens, total_output_tokens,
            total_cache_read_input_tokens, total_cache_write_input_tokens,
            status_400_count, status_403_count, status_429_count,
            status_500_count, status_503_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_fact_daily_tagged(cur, today: date, rng: random.Random) -> int:
    """Tag-attributed daily rows. Each request has 2-3 tags randomly assigned;
    write one row per tag (the fan-out pattern the schema is designed for)."""
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        wd_mult = weekday_curve(d)
        for acct in ACCOUNTS:
            for model in MODELS:
                m_factor = model_size_factor(model)
                for region in rng.sample(REGIONS, k=2):
                    op = rng.choice(OPERATIONS)
                    # Fewer tag-attributed requests than total volumetric — model
                    # the realistic case where not 100% of customer traffic uses
                    # request-metadata yet.
                    base = 1500 * m_factor * wd_mult * rng.uniform(0.5, 1.2)
                    total = max(1, int(base))
                    in_tok = total * rng.randint(1000, 3000)
                    out_tok = total * rng.randint(300, 1200)
                    cache_read = int(in_tok * rng.uniform(0.0, 0.3))
                    cache_write = int(in_tok * rng.uniform(0.0, 0.05))
                    failed = int(total * rng.uniform(0.0, 0.03))
                    # Assign 2-3 random tags from 2-3 different keys.
                    tag_keys_picked = rng.sample(list(TAGS.keys()), k=rng.choice([2, 3]))
                    for tk in tag_keys_picked:
                        tv = rng.choice(TAGS[tk])
                        rows.append((
                            d, acct, model, region, op, tk, tv,
                            total, failed, in_tok, out_tok, cache_read, cache_write,
                        ))
    cur.executemany(
        """
        INSERT INTO f_daily_tagged (
            event_date, accountId, modelId, region, operation, tag_key, tag_value,
            total_requests, failed_requests,
            total_input_tokens, total_output_tokens,
            total_cache_read_input_tokens, total_cache_write_input_tokens
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_hourly_peak(cur, today: date, rng: random.Random) -> int:
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        wd_mult = weekday_curve(d)
        for hour in range(24):
            h_mult = hourly_curve(hour)
            for acct in ACCOUNTS:
                for model in rng.sample(MODELS, k=4):
                    m_factor = model_size_factor(model)
                    for region in rng.sample(REGIONS, k=2):
                        base = 200 * m_factor * wd_mult * h_mult * rng.uniform(0.7, 1.3)
                        total = max(1, int(base))
                        in_tok = total * rng.randint(800, 3500)
                        out_tok = total * rng.randint(200, 1200)
                        # High cache-read share so the quota-accurate Peak TPM
                        # (which subtracts cache_read) is meaningfully lower
                        # than raw input+output — exercises the fix in demo data.
                        cache_read = int(in_tok * rng.uniform(0.3, 0.7))
                        s429 = int(total * rng.uniform(0.0, 0.03))
                        rows.append((d, hour, acct, model, region, total,
                                     in_tok, out_tok, cache_read, s429))
    cur.executemany(
        """
        INSERT INTO f_hourly_peak (
            event_date, hour, accountId, modelId, region,
            total_requests, total_input_tokens, total_output_tokens,
            total_cache_read_input_tokens, status_429_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_hourly_errors(cur, today: date, rng: random.Random) -> int:
    """7-day rolling. Only rows where failed_requests > 0."""
    rows = []
    for d_offset in range(7):
        d = today - timedelta(days=d_offset)
        for hour in range(24):
            for acct in rng.sample(ACCOUNTS, k=3):
                for model in rng.sample(MODELS, k=3):
                    for region in rng.sample(REGIONS, k=1):
                        total = rng.randint(50, 500)
                        failed = int(total * rng.uniform(0.01, 0.10))
                        if failed == 0:
                            continue
                        s429 = int(failed * rng.uniform(0.4, 0.8))
                        s500 = int(failed * rng.uniform(0.05, 0.2))
                        s503 = int(failed * rng.uniform(0.0, 0.1))
                        s400 = max(0, failed - s429 - s500 - s503)
                        s403 = int(failed * 0.02)
                        rows.append((
                            d, hour, acct, model, region,
                            total, failed, s400, s403, s429, s500, s503,
                        ))
    cur.executemany(
        """
        INSERT INTO f_hourly_errors (
            event_date, hour, accountId, modelId, region,
            total_requests, failed_requests,
            status_400_count, status_403_count, status_429_count,
            status_500_count, status_503_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_hourly_status(cur, today: date, rng: random.Random) -> int:
    """7-day rolling REAL per-status-code hourly rows → f_hourly_status.

    Represents data sourced from Bedrock model invocation logs (the only place
    a genuine per-code breakdown exists). Distinct from f_hourly_errors, which
    holds the honest CloudWatch 4xx/5xx aggregates. We seed both so the local
    demo exercises the "Status Codes" chart with realistic per-code shapes."""
    rows = []
    for d_offset in range(7):
        d = today - timedelta(days=d_offset)
        for hour in range(24):
            for acct in rng.sample(ACCOUNTS, k=3):
                for model in rng.sample(MODELS, k=3):
                    for region in rng.sample(REGIONS, k=1):
                        total = rng.randint(50, 500)
                        # Realistic distribution: throttles dominate errors,
                        # everything else is comparatively rare.
                        s429 = int(total * rng.uniform(0.0, 0.05))
                        s400 = int(total * rng.uniform(0.0, 0.015))
                        s403 = int(total * rng.uniform(0.0, 0.004))
                        s404 = int(total * rng.uniform(0.0, 0.002))
                        s408 = int(total * rng.uniform(0.0, 0.003))
                        s424 = int(total * rng.uniform(0.0, 0.002))
                        s500 = int(total * rng.uniform(0.0, 0.01))
                        s503 = int(total * rng.uniform(0.0, 0.004))
                        errs = s429 + s400 + s403 + s404 + s408 + s424 + s500 + s503
                        s200 = max(0, total - errs)
                        rows.append((
                            d, hour, acct, model, region, total,
                            s200, s400, s403, s404, s408, s424, s429, s500, s503,
                        ))
    cur.executemany(
        """
        INSERT INTO f_hourly_status (
            event_date, hour, accountId, modelId, region, total_requests,
            status_200_count, status_400_count, status_403_count,
            status_404_count, status_408_count, status_424_count,
            status_429_count, status_500_count, status_503_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_latency_daily(cur, today: date, rng: random.Random) -> int:
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        for model in MODELS:
            m_factor = model_size_factor(model)
            base_e2e = 8000 / max(m_factor, 0.1)  # opus slower, haiku faster
            base_ttft = base_e2e * 0.15
            for tt in rng.sample(TRAFFIC_TYPES, k=2):
                for region in rng.sample(REGIONS, k=2):
                    samples = rng.randint(500, 50000)
                    avg_e2e = base_e2e * rng.uniform(0.85, 1.15)
                    p50 = avg_e2e * rng.uniform(0.85, 1.0)
                    p90 = avg_e2e * rng.uniform(1.3, 1.8)
                    p99 = avg_e2e * rng.uniform(2.0, 3.5)
                    avg_ttft = base_ttft * rng.uniform(0.85, 1.15)
                    p50_t = avg_ttft * rng.uniform(0.85, 1.0)
                    p90_t = avg_ttft * rng.uniform(1.3, 1.8)
                    p99_t = avg_ttft * rng.uniform(2.0, 3.5)
                    rows.append((d, model, tt, region, samples,
                                 avg_e2e, p50, p90, p99,
                                 avg_ttft, p50_t, p90_t, p99_t))
    cur.executemany(
        """
        INSERT INTO f_latency_daily (
            event_date, modelId, traffic_type, region, sample_count,
            avg_e2e, p50_e2e, p90_e2e, p99_e2e,
            avg_ttft, p50_ttft, p90_ttft, p99_ttft
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_context_length(cur, today: date, rng: random.Random) -> int:
    """Only Claude models route across context-length variants."""
    variants = {
        "anthropic.claude-sonnet-4-5-20250929-v1:0": [
            "anthropic.claude-sonnet-4-5-20250929-v1:0:18k",
            "anthropic.claude-sonnet-4-5-20250929-v1:0:200k",
            "anthropic.claude-sonnet-4-5-20250929-v1:0:1024k",
        ],
        "anthropic.claude-opus-4-1-20250805-v1:0": [
            "anthropic.claude-opus-4-1-20250805-v1:0:18k",
            "anthropic.claude-opus-4-1-20250805-v1:0:200k",
        ],
    }
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        for acct in ACCOUNTS:
            for model, routes in variants.items():
                for route in routes:
                    for region in rng.sample(REGIONS, k=2):
                        total = rng.randint(100, 5000)
                        in_tok = total * rng.randint(1000, 50000)
                        rows.append((d, acct, model, route, region, total, in_tok))
    cur.executemany(
        """
        INSERT INTO f_context_length (
            event_date, accountId, modelId, routed_model_id, region,
            total_requests, total_input_tokens
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


def seed_quotas(cur, rng: random.Random) -> int:
    rows = []
    quota_codes_seen = set()
    for acct in ACCOUNTS:
        for region in REGIONS:
            for model in MODELS:
                model_short = model.split(".", 1)[1].split("-v")[0]
                for tt in QUOTA_TRAFFIC_TYPES:
                    for metric in QUOTA_METRICS:
                        quota_code = f"L-{abs(hash((tt, metric, model))) % 100000:05X}"
                        # Skip dupes (different accts/regions share the same quota code)
                        # but enforce PK by acct+region+code anyway.
                        quota_name = f"{tt} model inference {'requests' if metric == 'RPM' else 'tokens'} per minute for {model_short}"
                        default_v = (1000 if metric == "RPM" else 5_000_000) * rng.uniform(0.5, 1.5)
                        applied_v = default_v * rng.choice([1.0, 1.0, 2.0, 5.0])  # most stay at default
                        rows.append((
                            acct, region, quota_code, quota_name, model_short,
                            tt, metric, default_v, applied_v, True,
                        ))
    cur.executemany(
        """
        INSERT INTO f_quotas (
            accountId, region, quota_code, quota_name, model_name,
            traffic_type, metric, default_value, applied_value, adjustable
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (accountId, region, quota_code) DO NOTHING
        """,
        rows,
    )
    return len(rows)


def refresh_dim_tags(cur) -> int:
    """Recompute dim_tags from f_daily_tagged."""
    cur.execute("DELETE FROM dim_tags")
    cur.execute(
        """
        INSERT INTO dim_tags (tag_key, tag_value, first_seen, last_seen, total_requests_30d)
        SELECT tag_key, tag_value,
               MIN(event_date), MAX(event_date),
               SUM(total_requests)
        FROM f_daily_tagged
        WHERE event_date >= current_date - INTERVAL '30 days'
        GROUP BY tag_key, tag_value
        """
    )
    cur.execute("SELECT COUNT(*) FROM dim_tags")
    return cur.fetchone()[0]


def stamp_meta(cur) -> None:
    cur.execute(
        """
        INSERT INTO ingestion_meta (key, value, updated_at)
        VALUES ('last_refresh_utc', now()::text, now()),
               ('seed_source', 'synthetic', now()),
               ('last_invocation_logs_refresh', now()::text, now()),
               ('days_window', %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        (str(DAYS),),
    )


def truncate_facts(cur) -> None:
    cur.execute(
        """
        TRUNCATE
            f_daily, f_daily_tagged, f_hourly_peak, f_hourly_errors,
            f_hourly_status,
            f_latency_daily, f_context_length, f_quotas,
            dim_tags, ingestion_days, ingestion_meta
        """
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    today = date.today()

    print(f"Seeding {args.db_url}")
    print(f"  {DAYS} days, {len(ACCOUNTS)} accounts, {len(MODELS)} models, {len(REGIONS)} regions")

    with psycopg.connect(args.db_url) as conn:
        with conn.cursor() as cur:
            print("[1/8] truncating fact tables...")
            truncate_facts(cur)

            print("[2/8] seeding f_daily...")
            n = seed_fact_daily(cur, today, rng)
            print(f"      {n:,} rows")

            print("[3/8] seeding f_daily_tagged...")
            n = seed_fact_daily_tagged(cur, today, rng)
            print(f"      {n:,} rows")

            print("[4/8] seeding f_hourly_peak...")
            n = seed_hourly_peak(cur, today, rng)
            print(f"      {n:,} rows")

            print("[5/8] seeding f_hourly_errors (7-day rolling)...")
            n = seed_hourly_errors(cur, today, rng)
            print(f"      {n:,} rows")

            print("[5b/8] seeding f_hourly_status (real per-code, 7-day)...")
            n = seed_hourly_status(cur, today, rng)
            print(f"      {n:,} rows")

            print("[6/8] seeding f_latency_daily...")
            n = seed_latency_daily(cur, today, rng)
            print(f"      {n:,} rows")

            print("[7/8] seeding f_context_length...")
            n = seed_context_length(cur, today, rng)
            print(f"      {n:,} rows")

            print("[8/8] seeding f_quotas + dim_tags + meta...")
            n = seed_quotas(cur, rng)
            print(f"      {n:,} quota rows")
            n = refresh_dim_tags(cur)
            print(f"      {n:,} distinct (tag_key, tag_value) pairs")
            stamp_meta(cur)

        conn.commit()

    print("DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
