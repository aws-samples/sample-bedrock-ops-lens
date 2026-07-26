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
DAYS = 90
# Fake accounts follow a POWER-LAW volume shape (a few "whale" accounts dominate,
# a long tail of small ones) — matches the real fleet where the top handful of
# accounts hold most traffic. account_weight() below maps each id to its share.
# All IDs are invented (not real AWS account numbers); safe for an external demo.
ACCOUNTS = [
    "482915037461",  # whale 1
    "739104826355",  # whale 2
    "108462973558",  # mid
    "651037298144",  # mid
    "297461085023",  # mid
    "846203715699",  # small
    "530918274607",  # small
    "914725360881",  # small
    "672038514926",  # small
    "385016749233",  # tiny
    "760284193570",  # tiny
    "203847561092",  # tiny
]
# Account names for the accounts above (dim_account). Shape mirrors a real
# fleet: the whales are shared prod platforms, the tail is dev/sandbox.
ACCOUNT_NAMES = {
    "482915037461": "genai-platform-prod",
    "739104826355": "ml-inference-prod",
    "108462973558": "search-services-prod",
    "651037298144": "data-science-workbench",
    "297461085023": "customer-support-ai",
    "846203715699": "content-gen-staging",
    "530918274607": "fraud-detection-prod",
    "914725360881": "marketing-ai-dev",
    "672038514926": "analytics-sandbox",
    "385016749233": "sre-tools-dev",
    "760284193570": "experimental-llm-poc",
    "203847561092": "training-env",
}
REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1", "eu-central-1"]
# Model mix mirrors the real fleet: by REQUEST COUNT, Amazon Nova + "other"
# (Nova Lite/Micro, Titan embeddings) dominate (~95%); Claude is a small slice of
# requests but a large slice of TOKENS (huge input:output ratio, esp. Opus).
# A couple of legacy Claude 3.x models are included at low volume to exercise the
# model-lifecycle panel (EOL warnings).
MODELS = [
    # --- current Claude (low request share, high token weight) ---
    "anthropic.claude-opus-4-8",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    # --- Amazon Nova + embeddings (the request-count majority) ---
    "amazon.nova-pro-v1:0",
    "amazon.nova-lite-v1:0",
    "amazon.nova-micro-v1:0",
    "amazon.titan-embed-text-v2:0",
    # --- legacy / EOL (low volume, lights up the lifecycle panel) ---
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
]

# Per-model REQUEST-COUNT weight (relative). Nova/embeddings dominate counts;
# Claude is rare by count. Token-heaviness is applied separately in tokens_for().
MODEL_REQ_WEIGHT = {
    "amazon.nova-lite-v1:0":                       0.40,
    "amazon.titan-embed-text-v2:0":                0.20,
    "amazon.nova-micro-v1:0":                      0.11,
    "amazon.nova-pro-v1:0":                        0.24,
    "anthropic.claude-sonnet-4-5-20250929-v1:0":   0.030,
    "anthropic.claude-haiku-4-5-20251001-v1:0":    0.011,
    "anthropic.claude-opus-4-8":                   0.0025,
    "anthropic.claude-3-5-sonnet-20241022-v2:0":   0.004,   # legacy, trickle
    "anthropic.claude-3-haiku-20240307-v1:0":      0.0020,  # legacy, trickle
}
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
# Real hour-of-day shape (UTC), from f_hourly_peak: near-flat global 24/7 traffic
# with a mild US-overnight dip and a peak around 22:00-01:00 UTC — only ~1.3x
# peak-to-trough. Normalized to a 0..1 multiplier (peak hour = 1.0).
_HOD_AVG = [
    1754, 1787, 1734, 1677, 1590, 1513, 1467, 1443, 1440, 1447, 1462, 1449,
    1490, 1519, 1552, 1584, 1655, 1693, 1716, 1719, 1748, 1780, 1833, 1779,
]
_HOD_MAX = max(_HOD_AVG)


def hourly_curve(hour: int) -> float:
    """0..1 multiplier from the REAL hour-of-day shape — flat 24/7 (~1.3x
    peak-to-trough), NOT a business-hours bell. Global workloads run around
    the clock; the dashboard should reflect that."""
    return _HOD_AVG[hour % 24] / _HOD_MAX


def weekday_curve(d: date) -> float:
    """Mild weekend dip only. Real fleet is 24/7 global, so weekends are ~0.85
    of weekdays — not the sharp 0.4 a business-app would show."""
    return 0.85 if d.weekday() >= 5 else 1.0


def account_weight(acct: str) -> float:
    """Power-law-ish request weight by account position: a couple of whales, a
    long tail of small accounts. Index 0/1 dominate; the tail is tiny."""
    idx = ACCOUNTS.index(acct)
    # ~ 1 / (idx+1)^1.6 gives a heavy head + long tail (top-2 hold the majority).
    return 1.0 / ((idx + 1) ** 1.6)


def model_size_factor(model_id: str) -> float:
    """Relative REQUEST-COUNT factor per model — Nova/embeddings dominate counts,
    Claude is rare by count (but token-heavy, see tokens_for())."""
    return MODEL_REQ_WEIGHT.get(model_id, 0.05)


def tokens_for(model_id: str, total_requests: int, rng: random.Random) -> tuple[int, int, int, int]:
    """Return (input_tokens, output_tokens, cache_read, cache_write) for a
    (model, request-count) cell, matching the REAL fleet shape:
      - fleet-wide input:output ~72:1, driven by heavy prompt-caching + RAG /
        agentic prompts. Opus runs highest (up to hundreds:1); Nova/embeddings
        lower (embeddings are input-only, ~no output).
      - prompt cache-hit ~76% fleet-wide; Claude high (40-95%), non-Claude ~0%.
    """
    is_claude = model_id.startswith("anthropic.")
    is_embed = "embed" in model_id
    if is_embed:
        in_per = rng.randint(400, 1200)
        io_ratio = 0.0                      # embeddings emit ~no output tokens
        cache_frac = 0.0                    # embeddings aren't prompt-cached
    elif "opus" in model_id:
        in_per = rng.randint(20000, 90000)  # big agentic / long-context prompts
        io_ratio = rng.uniform(90, 260)     # very input-heavy
        cache_frac = rng.uniform(0.55, 0.95)
    elif is_claude and "sonnet" in model_id:
        in_per = rng.randint(6000, 30000)
        io_ratio = rng.uniform(60, 140)
        cache_frac = rng.uniform(0.55, 0.92)
    elif is_claude:                          # haiku / legacy claude
        in_per = rng.randint(2000, 12000)
        io_ratio = rng.uniform(30, 90)
        cache_frac = rng.uniform(0.45, 0.88)
    else:
        # Nova pro/lite/micro DOMINATE request count, so they drive the
        # fleet-wide ratio + cache-hit. The real fleet runs ~72:1 with ~76%
        # cache — only possible if the high-volume traffic is itself heavily
        # cached RAG/agentic (big reused system prompts). So Nova here is
        # input-heavy + well-cached, not the naive low-ratio chat shape.
        in_per = rng.randint(3000, 14000)
        io_ratio = rng.uniform(55, 95)
        cache_frac = rng.uniform(0.65, 0.85)
    in_tok = total_requests * in_per
    out_tok = int(in_tok / io_ratio) if io_ratio > 0 else 0
    cache_read = int(in_tok * cache_frac)
    cache_write = int(in_tok * rng.uniform(0.005, 0.04))
    return in_tok, out_tok, cache_read, cache_write


# Per-tier latency bases (ms), from the REAL f_latency_daily aggregates:
#   haiku  TTFT 93   p50 1151  p90 3418  p99 10581
#   sonnet TTFT 249  p50 2171  p90 7215  p99 22639
#   opus   TTFT 392  p50 4925  p90 18161 p99 55812
#   other  TTFT 284  p50 33825 p90 44199 p99 90039  (embeddings/long-context)
LATENCY_TIERS = {
    "haiku":  {"ttft": 93,  "p50": 1151,  "p90": 3418,  "p99": 10581},
    "sonnet": {"ttft": 249, "p50": 2171,  "p90": 7215,  "p99": 22639},
    "opus":   {"ttft": 392, "p50": 4925,  "p90": 18161, "p99": 55812},
    "other":  {"ttft": 284, "p50": 33825, "p90": 44199, "p99": 90039},
}


def latency_tier(model_id: str) -> str:
    if "opus" in model_id:
        return "opus"
    if "sonnet" in model_id:
        return "sonnet"
    if "haiku" in model_id:
        return "haiku"
    return "other"


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


# Throttling is a PAIR-level property, not per-row: in the real fleet ~4% of
# (account, model) pairs throttle at all; the other ~96% never do. We decide once
# per pair (deterministic hash → stable across a run) whether it's a throttler and
# its characteristic rate, so aggregating rows back up reproduces the ~4% shape.
_THROTTLE_PAIR_CACHE: dict[tuple[str, str], float] = {}
_SEED_SALT = "42"  # set from --seed in main(); salts the pair-throttle hash


def pair_throttle_rate(acct: str, model_id: str, rng: random.Random) -> float:
    """Characteristic throttle rate for an (account, model) pair. ~96% → 0.0;
    the ~4% throttlers get a small rate, with a rare dramatic tail (up to ~100%)
    skewed toward capacity-constrained models (opus/sonnet).

    Decided by a STABLE hash of the pair (not the shared module RNG), so the
    ~4% fraction and the tail are reproducible regardless of call order and
    don't get starved by RNG draws elsewhere. Seeded off the same base seed via
    _SEED_SALT so a different --seed still varies the selection."""
    key = (acct, model_id)
    if key in _THROTTLE_PAIR_CACHE:
        return _THROTTLE_PAIR_CACHE[key]
    import hashlib
    h = hashlib.sha256(f"{_SEED_SALT}:{acct}:{model_id}".encode()).digest()
    u = int.from_bytes(h[:8], "big") / 2**64        # stable uniform 0..1
    u2 = int.from_bytes(h[8:16], "big") / 2**64
    u3 = int.from_bytes(h[16:24], "big") / 2**64
    # Real fleet: ~4% of pairs throttle. This demo has only ~100 pairs, so we
    # aim a touch higher (~6%) and make the dramatic tail more likely, so the
    # dashboard reliably shows BOTH the common small-throttle case AND at least
    # one alarming spike (the story a customer needs to see). Still realistic.
    if u >= 0.06:
        rate = 0.0                                  # the ~94% that never throttle
    else:
        constrained = ("opus" in model_id or "sonnet" in model_id)
        if u2 < (0.55 if constrained else 0.30):
            rate = 0.30 + u3 * 0.65                 # dramatic tail (30%..95%)
        else:
            rate = 0.005 + u3 * 0.075               # typical small throttle
    _THROTTLE_PAIR_CACHE[key] = rate
    return rate


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
            acct_factor = account_weight(acct)     # power-law: whales + long tail
            for model in MODELS:
                m_factor = model_size_factor(model)
                for region in REGIONS:
                    region_mult = {"us-east-1": 1.0, "us-west-2": 0.7, "eu-west-1": 0.4,
                                   "ap-southeast-1": 0.25, "eu-central-1": 0.2}[region]
                    # Don't generate every operation × traffic_type combo — keep cardinality realistic.
                    op_choices = rng.sample(OPERATIONS, k=rng.choice([2, 3, 4]))
                    tt_choices = rng.sample(TRAFFIC_TYPES, k=rng.choice([1, 2, 3]))
                    for op in op_choices:
                        for tt in tt_choices:
                            tier = rng.choice(SERVICE_TIERS)
                            prefix = rng.choice(PROFILE_PREFIXES)
                            # High base so whale×Nova cells reach millions of reqs
                            # while tail×Claude cells stay small (power-law spread).
                            base = 120000 * acct_factor * m_factor * region_mult * wd_mult
                            jitter = rng.uniform(0.6, 1.4)
                            total = max(1, int(base * jitter))
                            pair_rate = pair_throttle_rate(acct, model, rng)
                            # per-row jitter around the pair's characteristic rate
                            throttle_rate = 0.0 if ("PROVISIONED" in tt or pair_rate == 0.0) \
                                else min(1.0, pair_rate * rng.uniform(0.5, 1.5))
                            error_rate = rng.uniform(0.001, 0.008)
                            failed, s400, s403, s429, s500, s503 = status_split(
                                total, throttle_rate, error_rate
                            )
                            successful = total - failed
                            in_tok, out_tok, cache_read, cache_write = tokens_for(model, total, rng)
                            # Split this cell across bedrock endpoints so the
                            # runtime/mantle sub-tabs both populate. Only Claude
                            # models serve on the bedrock-mantle (OpenAI-compat)
                            # endpoint, and mantle is a smaller share of their
                            # traffic (early adoption); Nova/Titan are runtime-only.
                            is_claude = model.startswith("anthropic.")
                            mantle_share = 0.18 if is_claude else 0.0
                            for endpoint, escale in (("runtime", 1.0 - mantle_share),
                                                     ("mantle", mantle_share)):
                                if escale <= 0:
                                    continue
                                e_total = max(1, int(total * escale))
                                e_succ  = int(successful * escale)
                                e_fail  = e_total - e_succ
                                e_in    = int(in_tok * escale)
                                e_out   = int(out_tok * escale)
                                e_cr    = int(cache_read * escale)
                                e_cw    = int(cache_write * escale)
                                e_s400  = int(s400 * escale); e_s403 = int(s403 * escale)
                                e_s429  = int(s429 * escale); e_s500 = int(s500 * escale)
                                e_s503  = int(s503 * escale)
                                # For models AWS marks legacy (Claude 3.x), all
                                # calls are legacy calls — feeds the Lifecycle
                                # tab's "Legacy calls" column. Non-legacy → NULL.
                                legacy_inv = e_total if ("claude-3-" in model) else None
                                rows.append((
                                    d, acct, model, region, op, tt, tier, prefix, endpoint,
                                    e_total, e_succ, e_fail, e_in, e_out,
                                    e_cr, e_cw,
                                    e_s400, e_s403, e_s429, e_s500, e_s503, legacy_inv,
                                ))
    cur.executemany(
        """
        INSERT INTO f_daily (
            event_date, accountId, modelId, region, operation, traffic_type,
            service_tier, inference_profile_prefix, endpoint,
            total_requests, successful_requests, failed_requests,
            total_input_tokens, total_output_tokens,
            total_cache_read_input_tokens, total_cache_write_input_tokens,
            status_400_count, status_403_count, status_429_count,
            status_500_count, status_503_count, legacy_invocations
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                acct_factor = account_weight(acct)
                for model in rng.sample(MODELS, k=5):
                    m_factor = model_size_factor(model)
                    for region in rng.sample(REGIONS, k=2):
                        base = 6000 * acct_factor * m_factor * wd_mult * h_mult * rng.uniform(0.7, 1.3)
                        total = max(1, int(base))
                        in_tok, out_tok, cache_read, _cw = tokens_for(model, total, rng)
                        # Throttling is a pair-level property (~4% of pairs); reuse
                        # the same characteristic rate so hourly agrees with daily.
                        pair_rate = pair_throttle_rate(acct, model, rng)
                        tr = 0.0 if pair_rate == 0.0 else min(1.0, pair_rate * rng.uniform(0.5, 1.5))
                        s429 = int(total * tr)
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
    """REAL per-status-code hourly rows → f_hourly_status (DAYS-day window).

    Represents data sourced from Bedrock model invocation logs (the only place
    a genuine per-code breakdown exists). Distinct from f_hourly_errors, which
    holds the honest CloudWatch 4xx/5xx aggregates. We seed both so the local
    demo exercises the "Status Codes" chart with realistic per-code shapes.
    Seeds the full DAYS window (not 7) so the chart is exercised across the
    dashboard's wider date-range filters — a 7-day seed masked the retention
    cap bug where any filter > 7 days showed the same last-7-days."""
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        for hour in range(24):
            for acct in rng.sample(ACCOUNTS, k=3):
                for model in rng.sample(MODELS, k=3):
                    for region in rng.sample(REGIONS, k=1):
                        # Emit both endpoints so the Status Codes chart's
                        # runtime/mantle sub-tabs render distinct series. Mantle
                        # gets ~1/4 the runtime volume (adoption is early).
                        for endpoint, scale in (("runtime", 1.0), ("mantle", 0.25)):
                            total = int(rng.randint(50, 500) * scale)
                            if total <= 0:
                                continue
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
                            # Per-account latency (007): plausible avg per model
                            # tier × sample count — powers the accounts-impacted
                            # and per-account latency drill-downs.
                            t = LATENCY_TIERS[latency_tier(model)]
                            lat_avg = t["p50"] * rng.uniform(0.8, 1.3)
                            lat_n = max(1, int(total * rng.uniform(0.6, 1.0)))
                            rows.append((
                                d, hour, acct, model, region, endpoint, total,
                                s200, s400, s403, s404, s408, s424, s429, s500, s503,
                                lat_avg * lat_n, lat_n,
                            ))
    cur.executemany(
        """
        INSERT INTO f_hourly_status (
            event_date, hour, accountId, modelId, region, endpoint, total_requests,
            status_200_count, status_400_count, status_403_count,
            status_404_count, status_408_count, status_424_count,
            status_429_count, status_500_count, status_503_count,
            latency_sum_ms, latency_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        rows,
    )
    return len(rows)


# Provider token pricing ($/1M) — mirrors backend model_insights.BEDROCK_PRICING
# so seeded spend is consistent with what the cost allocation would compute.
_PRICING = {
    "anthropic": {"input": 3.00, "output": 15.00},
    "amazon":    {"input": 0.20, "output": 0.80},
    "meta":      {"input": 0.50, "output": 1.50},
    "cohere":    {"input": 0.50, "output": 1.50},
}


def _provider_of(model_id: str) -> str:
    return (model_id or "").split(".", 1)[0] if "." in (model_id or "") else "anthropic"


def _service_label(model_id: str) -> str:
    """A Cost-Explorer-style service string per model family, so the Cost tab's
    'Spend by account/service' has realistic named line items (not one blob)."""
    m = model_id
    if "opus" in m:   return "Claude Opus (Amazon Bedrock Edition)"
    if "sonnet" in m: return "Claude Sonnet (Amazon Bedrock Edition)"
    if "haiku" in m:  return "Claude Haiku (Amazon Bedrock Edition)"
    if "nova" in m:   return "Amazon Nova (Amazon Bedrock Edition)"
    if "embed" in m or "titan" in m: return "Amazon Titan (Amazon Bedrock Edition)"
    return "Amazon Bedrock"


def seed_daily_cost(cur, today: date, rng: random.Random) -> int:
    """Derive f_daily_cost (Cost Explorer shape) from the token volumes already
    in f_daily: cost = input_tok/1e6*in_price + output_tok/1e6*out_price, priced
    per provider, grouped by (event_date, accountId, service, region). So spend
    tracks usage exactly like the real fleet, and the Cost tab shows realistic
    per-account / per-service line items."""
    cur.execute(
        """
        SELECT event_date, accountId, modelId, region,
               SUM(total_input_tokens)  AS in_tok,
               SUM(total_output_tokens) AS out_tok
        FROM f_daily
        GROUP BY event_date, accountId, modelId, region
        """
    )
    agg: dict[tuple, float] = {}
    for ev, acct, model, region, in_tok, out_tok in cur.fetchall():
        price = _PRICING.get(_provider_of(model), {"input": 0.50, "output": 1.50})
        cost = (int(in_tok or 0) / 1_000_000) * price["input"] \
             + (int(out_tok or 0) / 1_000_000) * price["output"]
        if cost <= 0:
            continue
        key = (ev, acct, _service_label(model), region)
        agg[key] = agg.get(key, 0.0) + cost
    rows = [(ev, acct, svc, region, round(c, 6), "USD")
            for (ev, acct, svc, region), c in agg.items()]
    if rows:
        cur.executemany(
            """
            INSERT INTO f_daily_cost (event_date, accountId, service, region, total_cost, currency)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_date, accountId, service, region)
            DO UPDATE SET total_cost = EXCLUDED.total_cost
            """,
            rows,
        )
    return len(rows)


def seed_latency_daily(cur, today: date, rng: random.Random) -> int:
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        for model in MODELS:
            tier = LATENCY_TIERS[latency_tier(model)]
            for tt in rng.sample(TRAFFIC_TYPES, k=2):
                for region in rng.sample(REGIONS, k=2):
                    samples = rng.randint(500, 50000)
                    # Anchor percentiles on the REAL per-tier numbers with mild
                    # per-cell jitter; keep p50 < p90 < p99 monotonic.
                    j = lambda: rng.uniform(0.9, 1.12)
                    p50 = tier["p50"] * j()
                    p90 = max(p50 * 1.2, tier["p90"] * j())
                    p99 = max(p90 * 1.2, tier["p99"] * j())
                    avg_e2e = p50 * rng.uniform(1.0, 1.25)   # mean pulled up by tail
                    avg_ttft = tier["ttft"] * j()
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


def seed_model_lifecycle(cur, today: date, rng: random.Random) -> int:
    """Populate dim_model_lifecycle so the Model Lifecycle tab has content.

    Two kinds of rows:
      * LEGACY + in use — the legacy Claude 3.x models that ALSO appear in
        f_daily (so the tab shows "legacy AND actively used" = migration
        urgency). Their legacy_time is in the past; EOL a few months out.
      * LEGACY, not in use — a couple of catalog-only legacy models (no
        f_daily traffic) so "All legacy" lists more than just the in-use ones,
        including one already PAST extended access (critical).
    Dates are relative to today so the timeline always straddles "now"."""
    from datetime import datetime, timezone

    def ts(days_from_today: int):
        return datetime.now(timezone.utc) + timedelta(days=days_from_today)

    # (modelId, model_name, provider, legacy_offset_days, ext_offset, eol_offset)
    entries = [
        # In-use legacy (these modelIds also have f_daily traffic) — LEGACY now,
        # EOL still ahead → "in use" + upcoming urgency.
        ("anthropic.claude-3-5-sonnet-20241022-v2:0", "Claude 3.5 Sonnet v2", "anthropic", -120, 45, 120),
        ("anthropic.claude-3-haiku-20240307-v1:0",    "Claude 3 Haiku",       "anthropic", -200, -10, 60),
        # Catalog-only legacy (no traffic) — one already PAST extended access.
        ("anthropic.claude-3-opus-20240229-v1:0",     "Claude 3 Opus",        "anthropic", -260, -40, -5),
        ("anthropic.claude-3-sonnet-20240229-v1:0",   "Claude 3 Sonnet",      "anthropic", -260, -60, -20),
        ("amazon.titan-text-express-v1",              "Amazon Titan Text Express", "amazon", -150, 60, 150),
        ("meta.llama3-1-8b-instruct-v1:0",            "Llama 3.1 8B Instruct", "meta",     -90,  120, 240),
    ]
    rows = []
    for mid, name, provider, leg, ext, eol in entries:
        for region in rng.sample(REGIONS, k=rng.choice([2, 3])):
            rows.append((
                mid, region, "LEGACY", name, provider,
                ts(leg - 365),   # start_of_life (~1y before legacy)
                ts(leg),         # legacy_time
                ts(ext),         # public_extended_access_time
                ts(eol),         # end_of_life_time
            ))
    cur.executemany(
        """
        INSERT INTO dim_model_lifecycle (
            modelId, region, status, model_name, provider,
            start_of_life_time, legacy_time,
            public_extended_access_time, end_of_life_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (modelId, region) DO UPDATE SET
            status = EXCLUDED.status, legacy_time = EXCLUDED.legacy_time,
            public_extended_access_time = EXCLUDED.public_extended_access_time,
            end_of_life_time = EXCLUDED.end_of_life_time, refreshed_at = now()
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
        "anthropic.claude-opus-4-8": [
            "anthropic.claude-opus-4-8:18k",
            "anthropic.claude-opus-4-8:200k",
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


# ---------------------------------------------------------------------------
# Pierre's governance/agent tabs — By User, Compliance, Agents & MCP
# ---------------------------------------------------------------------------
# Per-account caller fleets. Roles model teams/apps (the trustworthy "group"
# axis); sessions model SSO logins or service instances (the "user" axis).
# Human-session roles (adhoc/notebook) rotate through people; service roles
# keep one stable session per day — matches the real identity.arn shape.
_ROLE_POOL = [
    # (role name, [sessions], relative weight, is_human)
    ("ml-platform-prod",       ["genai-gateway"],                       1.00, False),
    ("search-reco-svc",        ["reco-ranker", "query-expand"],         0.55, False),
    ("genai-chatbot-prod",     ["chat-orchestrator"],                   0.40, False),
    ("data-science-adhoc",     ["jsmith", "achen", "mgarcia", "tpatel"], 0.18, True),
    ("batch-eval-pipeline",    ["nightly-eval"],                        0.12, False),
    ("sagemaker-notebook-role", ["priyak", "dwilliams"],                0.06, True),
]


def _principals_for(acct: str) -> list[tuple[str, str, str, str, float]]:
    """Stable per-account principal set: (arn, label, group, user, weight).
    Whales run the full role fleet; small accounts just a couple — so the
    By User tab's top-N is dominated by whale principals, matching f_daily."""
    idx = ACCOUNTS.index(acct)
    n_roles = 6 if idx < 2 else (4 if idx < 5 else 2)
    out = []
    for role, sessions, weight, is_human in _ROLE_POOL[:n_roles]:
        for sess in sessions:
            arn = f"arn:aws:sts::{acct}:assumed-role/{role}/{sess}"
            out.append((arn, f"{role}/{sess}", role, sess,
                        weight / len(sessions)))
    return out


def seed_by_identity(cur, today: date, rng: random.Random) -> int:
    """f_daily_by_identity — per IAM caller attribution (By User tab).
    Volumes reuse account_weight() and MODEL_REQ_WEIGHT so per-caller totals
    aggregate back up consistent with f_daily's account/model shape."""
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        wd_mult = weekday_curve(d)
        for acct in ACCOUNTS:
            acct_factor = account_weight(acct)
            for arn, label, group, user, p_weight in _principals_for(acct):
                # Each principal sticks to a small stable model set (teams
                # standardize on 1-3 models) — stable hash pick, not rng, so
                # the set survives call-order changes.
                import hashlib
                h = int.from_bytes(hashlib.sha256(arn.encode()).digest()[:4], "big")
                models = [MODELS[(h + i * 3) % len(MODELS)] for i in range(1 + h % 3)]
                for model in set(models):
                    m_factor = model_size_factor(model)
                    for region in rng.sample(REGIONS, k=rng.choice([1, 2])):
                        base = 90000 * acct_factor * p_weight * m_factor * wd_mult
                        total = max(1, int(base * rng.uniform(0.6, 1.4)))
                        failed = int(total * rng.uniform(0.001, 0.02))
                        in_tok, out_tok, _cr, _cw = tokens_for(model, total, rng)
                        rows.append((d, acct, model, region, arn, label,
                                     group, user, total, failed, in_tok, out_tok))
    cur.executemany(
        """
        INSERT INTO f_daily_by_identity (
            event_date, accountId, modelId, region, principal_arn,
            principal_label, principal_group, principal_user,
            total_requests, failed_requests,
            total_input_tokens, total_output_tokens
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_date, accountId, modelId, region, principal_arn)
        DO UPDATE SET total_requests = EXCLUDED.total_requests
        """,
        rows,
    )
    return len(rows)


def seed_client_telemetry(cur, today: date, rng: random.Random) -> int:
    """f_proxy_dim_hourly — client-reported telemetry (Workloads tab + the
    By-Provider panel). Seeds per-user events across all four paths so the
    demo shows what only client telemetry can: direct anthropic-api /
    openai-api traffic, client TTFT on mantle, retries, and estimated cost.

    Fleet shape mirrors an enterprise (Adobe/Salesforce scale): ~1,500
    distinct users on a power law — a heavy head with daily activity and a
    long tail of occasional users — WITHOUT exploding the hourly table
    (tail users get a handful of sparse buckets, not a dense grid)."""
    first = ["priya", "david", "jane", "alex", "maria", "wei", "raj", "sofia",
             "tom", "nina", "omar", "lena", "carlos", "yuki", "ivan", "amara",
             "sam", "kate", "leo", "zara"]
    last = ["k", "williams", "smith", "chen", "garcia", "patel", "nguyen",
            "mueller", "silva", "tanaka", "brown", "rossi", "kim", "lopez",
            "singh", "dubois", "novak", "haddad", "olsen", "costa"]
    teams = [f"{t}" for t in (
        "ml-platform", "search-reco", "chat-platform", "data-science",
        "fraud-detection", "content-gen", "sales-assist", "doc-intel",
        "code-assist", "support-ai", "marketing-ai", "risk-analytics",
        "personalization", "voice-ai", "translation", "summarization",
        "qa-automation", "knowledge-base", "billing-ai", "hr-assist",
        "legal-review", "catalog-ai", "logistics-ai", "growth-ml", "sec-ops")]
    N_USERS = 1500
    users = []
    # Workload/BU axes ride along with team (stable mapping team → workload/BU
    # so the numbers agree when pivoting between axes).
    _WORKLOADS = ["search-service", "chat-assistant", "doc-summarizer",
                  "reco-engine", "fraud-scorer", "content-pipeline",
                  "support-triage", "code-review-bot", "sales-insights",
                  "kb-retrieval", "voice-transcribe", "batch-eval"]
    _BUSINESS_UNITS = ["retail", "finance", "enterprise", "consumer", "platform"]
    for i in range(N_USERS):
        name = f"{first[i % 20]}{last[(i // 20) % 20]}{i // 400 or ''}"
        # Power law: user #1 weight 1.0, #100 ~0.02, #1000 ~0.002.
        weight = 1.0 / ((i + 1) ** 0.85)
        users.append((f"{name}@example.com", teams[i % len(teams)], weight))

    # (model, endpoint, share, $/1K-ish blended, ttft base)
    paths = [
        ("us.anthropic.claude-sonnet-5",  "runtime",       0.55, 0.011, None),
        ("anthropic.claude-opus-4-8",     "mantle",        0.15, 0.075, 420),
        ("claude-sonnet-5",               "anthropic-api", 0.20, 0.012, 300),
        ("gpt-5.2-mini",                  "openai-api",    0.10, 0.002, 140),
    ]
    hours = (9, 11, 14, 16, 20)
    rows = []
    for rank, (user, team, w) in enumerate(users):
        # Density scales with rank: head users are active most days on most
        # paths; tail users appear in only a few sparse buckets.
        if rank < 50:
            buckets = [(d, h, p) for d in range(30) for h in hours for p in paths]
            keep = 1.0
        elif rank < 300:
            buckets = [(d, h, p) for d in range(30) for h in rng.sample(hours, 2)
                       for p in rng.sample(paths, 2)]
            keep = 0.5
        else:
            n = rng.randint(2, 8)
            buckets = [(rng.randrange(30), rng.choice(hours), rng.choice(paths))
                       for _ in range(n)]
            keep = 1.0
        for d_offset, hour, (model, ep, share, rate, ttft_base) in buckets:
            if keep < 1.0 and rng.random() > keep:
                continue
            d = today - timedelta(days=d_offset)
            base = 1100 * w * share * weekday_curve(d) * rng.uniform(0.6, 1.4)
            reqs = max(1, int(base))
            in_tok = reqs * rng.randint(3000, 12000)
            out_tok = reqs * rng.randint(150, 700)
            cache = int(in_tok * (0.7 if "claude" in model or "anthropic" in model else 0.2))
            errs = int(reqs * rng.uniform(0.0, 0.02))
            # Throttling concentrates in the heavy head (they hit limits);
            # the tail almost never throttles — matches real fleets.
            thr = int(reqs * rng.uniform(0.0, 0.02)) if rank < 40 and rng.random() < 0.3 else 0
            retried = int(reqs * rng.uniform(0.0, 0.05))
            lat = rng.uniform(1500, 4500) if "sonnet" in model else rng.uniform(3000, 9000)
            ttft50 = ttft_base * rng.uniform(0.8, 1.1) if ttft_base else None
            ttft90 = ttft_base * rng.uniform(1.6, 2.6) if ttft_base else None
            cost = round((in_tok + out_tok * 4) / 1000 * rate * 0.001 * 1000, 4)
            # Every request carries the FULL dimension map — the identity axes
            # (user/team) AND the workload-attribution axes (workload/env/
            # business_unit) that are the original workload-attribution story. Dropping the
            # latter when the fleet scaled up broke the Settings key picker and
            # the Workloads pivot (regression caught by the user 2026-07-26).
            team_idx = teams.index(team)
            workload = _WORKLOADS[team_idx % len(_WORKLOADS)]
            env = "prod" if (rank + d_offset) % 5 else ("staging" if rank % 2 else "dev")
            bu = _BUSINESS_UNITS[team_idx % len(_BUSINESS_UNITS)]
            for dk, dv in (("user", user), ("team", team), ("workload", workload),
                           ("env", env), ("business_unit", bu)):
                rows.append((d, hour, dk, dv, model, ep, "us-east-1", "__none__",
                             reqs, in_tok, out_tok, cache, thr, errs,
                             lat, lat * 1.8, lat * 3.2, ttft50, ttft90,
                             retried, cost))

    # Pre-aggregate: many users share a team, so team rows collide on the PK
    # (and the upsert REPLACES rather than adds). Merge duplicates in Python —
    # sums for counts/cost, max for the percentile columns.
    merged: dict = {}
    for r in rows:
        key = r[:8]
        m = merged.get(key)
        if m is None:
            merged[key] = list(r)
            continue
        for i in (8, 9, 10, 11, 12, 13, 19):          # reqs/tokens/thr/errs/retried
            m[i] += r[i]
        for i in (14, 15, 16, 17, 18):                 # latency/ttft percentiles
            a, b = m[i], r[i]
            m[i] = max(a, b) if (a is not None and b is not None) else (a if a is not None else b)
        m[20] = round(m[20] + r[20], 4)                # cost
    rows = [tuple(v) for v in merged.values()]
    cur.executemany(
        """
        INSERT INTO f_proxy_dim_hourly (
            event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId,
            total_requests, input_tokens, output_tokens, cache_read_tokens,
            throttled_count, error_count,
            p50_latency_ms, p90_latency_ms, p99_latency_ms,
            p50_ttft_ms, p90_ttft_ms, retried_count, cost_usd_est
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (event_date, hour, dim_key, dim_value, modelId, endpoint, region, accountId)
        DO UPDATE SET total_requests = EXCLUDED.total_requests
        """,
        rows,
    )
    # Refresh the dimension picker source like the real ingester does.
    cur.execute("DELETE FROM dim_proxy_dimensions")
    cur.execute("""
        INSERT INTO dim_proxy_dimensions (dim_key, dim_value, first_seen, last_seen, total_requests_30d, endpoints)
        SELECT dim_key, dim_value, MIN(event_date), MAX(event_date), SUM(total_requests),
               array_agg(DISTINCT endpoint)
        FROM f_proxy_dim_hourly
        WHERE event_date >= current_date - INTERVAL '30 days'
        GROUP BY dim_key, dim_value
    """)
    return len(rows)


# Guardrails: only some accounts have them deployed (whales + one mid) —
# realistic, and it keeps the Compliance tab's by-guardrail table readable.
_GUARDRAILS = [
    # (account index, guardrail id, version)
    (0, "prod-content-safety", "3"),
    (0, "pii-shield",          "1"),
    (1, "prod-content-safety", "2"),
    (3, "brand-safety-filter", "DRAFT"),
]
_POLICY_TYPES = ["CONTENT_FILTER", "DENIED_TOPIC", "SENSITIVE_INFORMATION", "WORD_FILTER"]
# CONTENT_FILTER dominates interventions in real fleets.
_POLICY_WEIGHTS = [0.62, 0.16, 0.15, 0.07]


def seed_guardrails(cur, today: date, rng: random.Random) -> int:
    """f_daily_guardrails — Compliance tab. Grain contract (compliance.py):
    the '__all__'/'__all__' row carries invocations + total intervened
    (totals / by-guardrail / daily-trend); per-policy rows carry intervened +
    text_units with content_source='__all__' (summary excludes '__all__')."""
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        wd_mult = weekday_curve(d)
        for acct_idx, gr_id, version in _GUARDRAILS:
            acct = ACCOUNTS[acct_idx]
            for region in ("us-east-1", "us-west-2"):
                arn = f"arn:aws:bedrock:{region}:{acct}:guardrail/{gr_id}"
                base = 45000 * account_weight(acct) * wd_mult
                invocations = max(10, int(base * rng.uniform(0.7, 1.3)))
                # Intervention rate: content-safety guardrails sit ~2-6%.
                intervened_total = int(invocations * rng.uniform(0.02, 0.06))
                text_units_total = invocations * rng.randint(2, 6)
                # Per-policy split of the interventions.
                remaining = intervened_total
                for i, (pt, pw) in enumerate(zip(_POLICY_TYPES, _POLICY_WEIGHTS)):
                    part = remaining if i == len(_POLICY_TYPES) - 1 \
                        else int(intervened_total * pw * rng.uniform(0.8, 1.2))
                    part = min(part, remaining)
                    remaining -= part
                    if part == 0:
                        continue
                    rows.append((d, acct, region, arn, version, pt, "__all__",
                                 0, part, int(text_units_total * pw)))
                # Rollup row: the only grain carrying Invocations.
                rows.append((d, acct, region, arn, version, "__all__", "__all__",
                             invocations, intervened_total, text_units_total))
    cur.executemany(
        """
        INSERT INTO f_daily_guardrails (
            event_date, accountId, region, guardrail_arn, guardrail_version,
            policy_type, content_source, invocations, intervened, text_units
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_date, accountId, region, guardrail_arn,
                     guardrail_version, policy_type, content_source)
        DO UPDATE SET invocations = EXCLUDED.invocations
        """,
        rows,
    )
    return len(rows)


# AgentCore fleets: whale accounts only. Runtimes feed the summary pivot;
# tools feed the MCP gateway-tools panel (resource_type IN gateway/tool).
_AGENT_RUNTIMES = [
    # (account index, runtime id, invocations/day base, err rate)
    (0, "customer-support-agent", 42000, 0.012),
    (0, "code-assist-agent",      18000, 0.008),
    (1, "ops-copilot",             9500, 0.020),
    (3, "doc-summarizer-agent",    2200, 0.005),
]
_MCP_TOOLS = [
    # (account index, tool name, calls/day base)
    (0, "search_knowledge_base", 65000),
    (0, "create_ticket",         12000),
    (0, "lookup_order",          28000),
    (1, "query_metrics",          7400),
    (1, "run_diagnostic",         3100),
]


def seed_agentcore(cur, today: date, rng: random.Random) -> int:
    """f_daily_agentcore — Agents & MCP tab. Metric-per-row (agents.py pivots
    Invocations/SessionCount/SystemErrors/UserErrors/Throttles at stat='sum'
    and Latency at stat='average'/'p99' for resource_type IN runtime/account;
    gateway/tool rows feed /agents/gateway-tools)."""
    ns = "bedrock-agentcore"
    rows = []
    for d_offset in range(DAYS):
        d = today - timedelta(days=d_offset)
        wd_mult = weekday_curve(d)
        for acct_idx, rt_id, inv_base, err_rate in _AGENT_RUNTIMES:
            acct, region = ACCOUNTS[acct_idx], "us-east-1"
            inv = max(1, int(inv_base * wd_mult * rng.uniform(0.7, 1.3)))
            sessions = int(inv * rng.uniform(0.10, 0.25))   # multi-turn sessions
            sys_err = int(inv * err_rate * rng.uniform(0.2, 0.5))
            usr_err = int(inv * err_rate) - sys_err
            throttles = int(inv * rng.uniform(0.0, 0.004))
            avg_lat = rng.uniform(2800, 9500)               # agent loops are slow
            p99_lat = avg_lat * rng.uniform(3.0, 6.5)
            for metric, stat, value in (
                ("Invocations",  "sum",     inv),
                ("SessionCount", "sum",     sessions),
                ("SystemErrors", "sum",     sys_err),
                ("UserErrors",   "sum",     max(0, usr_err)),
                ("Throttles",    "sum",     throttles),
                ("Latency",      "average", avg_lat),
                ("Latency",      "p99",     p99_lat),
            ):
                rows.append((d, acct, region, ns, "runtime", rt_id,
                             metric, stat, float(value)))
        for acct_idx, tool, call_base in _MCP_TOOLS:
            acct, region = ACCOUNTS[acct_idx], "us-east-1"
            calls = max(1, int(call_base * wd_mult * rng.uniform(0.7, 1.3)))
            avg_lat = rng.uniform(120, 900)                 # tool calls are fast
            for metric, stat, value in (
                ("Invocations", "sum",     calls),
                ("Errors",      "sum",     int(calls * rng.uniform(0.0, 0.01))),
                ("Latency",     "average", avg_lat),
                ("Latency",     "p99",     avg_lat * rng.uniform(2.5, 5.0)),
            ):
                rows.append((d, acct, region, ns, "tool", tool,
                             metric, stat, float(value)))
    cur.executemany(
        """
        INSERT INTO f_daily_agentcore (
            event_date, accountId, region, namespace,
            resource_type, resource_id, metric_name, stat, value
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_date, accountId, region, namespace,
                     resource_type, resource_id, metric_name, stat)
        DO UPDATE SET value = EXCLUDED.value
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
               -- the freshness pill reads last_cw_metrics_refresh; stamp it so a
               -- seeded demo DB shows "Fresh" rather than "run the CW ingester".
               ('last_cw_metrics_refresh', now()::text, now()),
               ('last_invocation_logs_refresh', now()::text, now()),
               ('days_window', %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        (str(DAYS),),
    )


def seed_account_names(cur) -> int:
    # dim_account is normally maintained by the ingester's name-resolution
    # chain (config > org > Account API); the demo has no real accounts, so
    # give the synthetic fleet the same account-name resolution the
    # resolver would produce. source='config' = highest-precedence source.
    for acct_id, name in ACCOUNT_NAMES.items():
        cur.execute(
            """
            INSERT INTO dim_account (accountId, account_name, source, refreshed_at)
            VALUES (%s, %s, 'config', now())
            ON CONFLICT (accountId) DO UPDATE SET
                account_name = EXCLUDED.account_name,
                source       = EXCLUDED.source,
                refreshed_at = EXCLUDED.refreshed_at
            """,
            (acct_id, name),
        )
    return len(ACCOUNT_NAMES)


def truncate_facts(cur) -> None:
    cur.execute(
        """
        TRUNCATE
            f_daily, f_daily_tagged, f_hourly_peak, f_hourly_errors,
            f_hourly_status, f_daily_cost,
            f_latency_daily, f_context_length, f_quotas,
            dim_model_lifecycle,
            dim_tags, ingestion_days, ingestion_meta
        """
    )
    # Pierre's governance tables — existence-checked (no try/except: a failed
    # TRUNCATE would abort the transaction and undo the truncate above) so
    # seeding still works against an older DB that predates them.
    for tbl in ("f_daily_by_identity", "f_daily_guardrails", "f_daily_agentcore",
                "f_proxy_dim_hourly", "dim_proxy_dimensions"):
        cur.execute("SELECT to_regclass(%s)", (tbl,))
        if cur.fetchone()[0] is not None:
            cur.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(tbl)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url", default=DEFAULT_DB_URL)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--i-understand-this-writes-synthetic-data", action="store_true",
                    help="required override to seed a non-localhost database")
    args = ap.parse_args()

    # SAFETY GUARD: this script writes SYNTHETIC data. It must NEVER touch a
    # real/deployed database — customers must only ever see their own live
    # Bedrock telemetry. Refuse unless the target is an obvious localhost dev DB
    # (or the explicit override flag is passed for a throwaway test DB). The
    # ingester/deploy path never imports this module, so this is defence in
    # depth against accidental misuse.
    _u = (args.db_url or "").lower()
    _is_local = ("@localhost" in _u or "@127.0.0.1" in _u or "@/" in _u
                 or "host=localhost" in _u or "host=127.0.0.1" in _u)
    if not _is_local and not args.i_understand_this_writes_synthetic_data:
        print("REFUSING: seed.py writes SYNTHETIC data and the target is not a "
              "localhost dev DB. Customer deployments must show only real "
              "CloudWatch/Cost Explorer/Service Quotas data. Pass "
              "--i-understand-this-writes-synthetic-data to override for a "
              "throwaway test DB.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    global _SEED_SALT
    _SEED_SALT = str(args.seed)
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

            print("[5c/8] seeding f_daily_cost (derived from token volumes)...")
            n = seed_daily_cost(cur, today, rng)
            print(f"      {n:,} rows")

            print("[6/8] seeding f_latency_daily...")
            n = seed_latency_daily(cur, today, rng)
            print(f"      {n:,} rows")

            print("[6b/8] seeding dim_model_lifecycle...")
            n = seed_model_lifecycle(cur, today, rng)
            print(f"      {n:,} rows")

            print("[7/8] seeding f_context_length...")
            n = seed_context_length(cur, today, rng)
            print(f"      {n:,} rows")

            # Governance/agent tabs (By User / Compliance / Agents & MCP).
            # Existence-guarded: an older DB without these tables still seeds.
            def _has(tbl):
                cur.execute("SELECT to_regclass(%s)", (tbl,))
                return cur.fetchone()[0] is not None

            if _has("f_daily_by_identity"):
                print("[7b/8] seeding f_daily_by_identity (By User tab)...")
                n = seed_by_identity(cur, today, rng)
                print(f"      {n:,} rows")
            if _has("f_daily_guardrails"):
                print("[7c/8] seeding f_daily_guardrails (Compliance tab)...")
                n = seed_guardrails(cur, today, rng)
                print(f"      {n:,} rows")
            if _has("f_daily_agentcore"):
                print("[7d/8] seeding f_daily_agentcore (Agents & MCP tab)...")
                n = seed_agentcore(cur, today, rng)
                print(f"      {n:,} rows")
            if _has("f_proxy_dim_hourly"):
                print("[7e/8] seeding f_proxy_dim_hourly (client telemetry: Workloads + By Provider)...")
                n = seed_client_telemetry(cur, today, rng)
                print(f"      {n:,} rows")
            if _has("dim_account"):
                print("[7f/8] seeding dim_account (account names)...")
                n = seed_account_names(cur)
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
