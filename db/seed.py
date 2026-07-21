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
                            rows.append((
                                d, hour, acct, model, region, endpoint, total,
                                s200, s400, s403, s404, s408, s424, s429, s500, s503,
                            ))
    cur.executemany(
        """
        INSERT INTO f_hourly_status (
            event_date, hour, accountId, modelId, region, endpoint, total_requests,
            status_200_count, status_400_count, status_403_count,
            status_404_count, status_408_count, status_424_count,
            status_429_count, status_500_count, status_503_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
