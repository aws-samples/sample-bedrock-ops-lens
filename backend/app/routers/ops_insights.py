"""Ops Insights tab — CRIS adoption, throttle hotspots, peak RPM, caching, etc."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


# ---------------------------------------------------------------------------
# CRIS adoption
# ---------------------------------------------------------------------------
@router.get("/ops-cris-adoption")
async def ops_cris_adoption(f: FilterSet = Depends(parse_filters)):
    """Per-model CRIS vs on-demand request split."""
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)::BIGINT AS cris_requests,
          SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)::BIGINT AS od_requests,
          ROUND(100.0 * SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)
              / NULLIF(SUM(total_requests), 0), 2) AS cris_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(total_requests) > 0
        ORDER BY (SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)
              + SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)) DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/ops-cris-by-account")
async def ops_cris_by_account(f: FilterSet = Depends(parse_filters)):
    """Per-account CRIS vs OD breakdown — find accounts not using CRIS."""
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId,
          SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)::BIGINT AS cris_requests,
          SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)::BIGINT AS od_requests
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId
        HAVING SUM(total_requests) > 0
        ORDER BY (SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)
              + SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)) DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Throttle rate hotspots
# ---------------------------------------------------------------------------
@router.get("/ops-throttle-rate")
async def ops_throttle_rate(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(status_429_count)::BIGINT AS throttled,
          ROUND(100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0), 3)
            AS throttle_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(status_429_count) > 0 AND SUM(total_requests) > 100
        ORDER BY throttle_pct DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Peak RPM / TPM (max-over-hour from f_hourly_peak)
# ---------------------------------------------------------------------------
@router.get("/ops-peak-rpm")
async def ops_peak_rpm(f: FilterSet = Depends(parse_filters)):
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    if f.provider != "all":
        from ..filters import PROVIDER_PREFIX
        parts.append(f"modelId LIKE ${len(params)+1}")
        params.append(PROVIDER_PREFIX[f.provider] + "%")
    w = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          MAX(total_requests)::BIGINT       AS peak_requests_hour,
          MAX(CASE WHEN total_cache_read_input_tokens IS NULL THEN NULL ELSE GREATEST(total_input_tokens - total_cache_read_input_tokens, 0) END)::BIGINT AS peak_input_tpm,
          MAX(total_output_tokens)::BIGINT  AS peak_output_tpm
        FROM f_hourly_peak
        WHERE {w}
        GROUP BY accountId, modelId, region
        HAVING MAX(total_requests) > 0
        ORDER BY peak_requests_hour DESC
        LIMIT 200
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Burndown risk — Claude 4+ family using > some % of applied quota.
# ---------------------------------------------------------------------------
@router.get("/ops-burndown-risk")
async def ops_burndown_risk(f: FilterSet = Depends(parse_filters)):
    """Joins f_hourly_peak (peak input + output TPM) with f_quotas (applied TPM)
    to surface Claude-4 family deployments approaching their limit.

    Replaces the reference's hardcoded LIKE '%coffee%' codename filter with
    the public 'claude-' family pattern."""
    parts = ["h.event_date BETWEEN $1::date AND $2::date",
             "h.modelId LIKE 'anthropic.claude-%'"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"h.accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"h.region = ${len(params)+1}")
        params.append(f.region)
    w = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        WITH peaks AS (
          SELECT accountId, modelId, region,
            MAX(CASE WHEN total_cache_read_input_tokens IS NULL THEN NULL ELSE GREATEST(total_input_tokens - total_cache_read_input_tokens, 0) END)  AS peak_input_tpm,
            MAX(total_output_tokens) AS peak_output_tpm,
            MAX(CASE WHEN total_cache_read_input_tokens IS NULL THEN NULL
                     ELSE GREATEST((total_input_tokens - total_cache_read_input_tokens) + total_output_tokens, 0) END) AS peak_combined_tpm
          FROM f_hourly_peak h
          WHERE {w}
          GROUP BY accountId, modelId, region
        )
        SELECT p.accountId, p.modelId, p.region,
          p.peak_combined_tpm::BIGINT AS peak_tpm,
          p.peak_output_tpm::BIGINT,
          q.applied_value::BIGINT     AS effective_tpm,
          ROUND((100.0 * p.peak_combined_tpm / NULLIF(q.applied_value, 0))::numeric, 2)
            AS overhead_pct
        FROM peaks p
        LEFT JOIN f_quotas q
          ON q.accountId = p.accountId
         AND q.region = p.region
         AND q.metric = 'TPM'
         AND p.modelId LIKE '%' || q.model_name || '%'
        WHERE q.applied_value IS NOT NULL
        ORDER BY overhead_pct DESC NULLS LAST
        LIMIT 200
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
@router.get("/ops-caching")
async def ops_caching(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_input_tokens)::BIGINT              AS total_input_tokens,
          SUM(total_cache_read_input_tokens)::BIGINT   AS cache_read_tokens,
          SUM(total_cache_write_input_tokens)::BIGINT  AS cache_write_tokens,
          -- Hit rate: cached / (cached + fresh-input). Cloudwatch's
          -- InputTokenCount excludes cache reads; they ship as a separate
          -- counter (CacheReadInputTokenCount), so the denominator is the
          -- sum or the ratio is unbounded.
          ROUND(100.0 * COALESCE(SUM(total_cache_read_input_tokens), 0)
              / NULLIF(COALESCE(SUM(total_cache_read_input_tokens), 0)
                       + COALESCE(SUM(total_input_tokens), 0), 0), 2) AS hit_rate_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(total_input_tokens) > 0
        ORDER BY total_input_tokens DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Context length routing
# ---------------------------------------------------------------------------
@router.get("/ops-context-length")
async def ops_context_length(f: FilterSet = Depends(parse_filters)):
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    w = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        SELECT routed_model_id, modelId,
          SUM(total_requests)::BIGINT     AS total_requests,
          SUM(total_input_tokens)::BIGINT AS input_tokens
        FROM f_context_length
        WHERE {w}
        GROUP BY routed_model_id, modelId
        ORDER BY total_requests DESC
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Request shape (avg input/output ratio)
# ---------------------------------------------------------------------------
@router.get("/ops-request-shape")
async def ops_request_shape(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          ROUND(SUM(total_input_tokens)::NUMERIC
                / NULLIF(SUM(total_requests), 0), 1) AS avg_input,
          ROUND(SUM(total_output_tokens)::NUMERIC
                / NULLIF(SUM(total_requests), 0), 1) AS avg_output,
          ROUND(SUM(total_output_tokens)::NUMERIC
                / NULLIF(SUM(total_input_tokens), 0), 3) AS ratio
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) > 100
        ORDER BY SUM(total_requests) DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Service tier
# ---------------------------------------------------------------------------
@router.get("/ops-service-tier")
async def ops_service_tier(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT service_tier,
          SUM(total_requests)::BIGINT       AS total_requests,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts,
          ROUND(100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0), 3)
            AS throttle_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY service_tier
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Inference profile prefix adoption
# ---------------------------------------------------------------------------
@router.get("/ops-inference-profile")
async def ops_inference_profile(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT inference_profile_prefix,
          SUM(total_requests)::BIGINT       AS total_requests,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        GROUP BY inference_profile_prefix
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
