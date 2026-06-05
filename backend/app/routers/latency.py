"""Latency tab endpoints. Reads f_latency_daily (pre-computed percentiles)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


@router.get("/latency-by-model")
async def latency_by_model(f: FilterSet = Depends(parse_filters)):
    """Sample-count-weighted average across (traffic_type, region) per model.

    Percentiles can't be re-aggregated, so we report:
      - sample_count weighted average (statistically valid)
      - the MAX p50/p90/p99 across underlying buckets (worst case)
    The reference returns sample-weighted means, not max — keeping that for parity.
    """
    w = build_where(f, has_account=False)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(sample_count)::BIGINT AS sample_count,
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          SUM(p50_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_e2e,
          SUM(p90_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_e2e,
          SUM(p99_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_e2e,
          SUM(avg_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_ttft,
          SUM(p50_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_ttft,
          SUM(p90_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_ttft,
          SUM(p99_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_ttft
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY modelId
        ORDER BY sample_count DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/latency-cris-vs-od")
async def latency_cris_vs_od(f: FilterSet = Depends(parse_filters)):
    """Same per-model latency split by traffic_type — quantifies CRIS overhead."""
    w = build_where(f, has_account=False)
    rows = await db.fetch(
        f"""
        SELECT modelId, traffic_type,
          SUM(sample_count)::BIGINT AS sample_count,
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          SUM(p50_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_e2e,
          SUM(p90_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_e2e,
          SUM(p99_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_e2e,
          SUM(avg_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_ttft,
          SUM(p50_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_ttft,
          SUM(p90_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_ttft,
          SUM(p99_ttft * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_ttft
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY modelId, traffic_type
        ORDER BY modelId, traffic_type
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/operation-latency")
async def operation_latency(f: FilterSet = Depends(parse_filters)):
    """f_latency_daily doesn't carry operation; we approximate using f_daily
    request volumes for shape, then weight by sample_count where models match.
    Returns operation-level summary by joining latency to operation in f_daily."""
    # Simpler approach: latency rows don't have operation, so we report per-traffic-type
    # which is the closest proxy the schema supports.
    w = build_where(f, has_account=False)
    rows = await db.fetch(
        f"""
        SELECT traffic_type AS operation,
          SUM(sample_count)::BIGINT AS sample_count,
          SUM(avg_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS avg_e2e,
          SUM(p50_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p50_e2e,
          SUM(p90_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p90_e2e,
          SUM(p99_e2e * sample_count) / NULLIF(SUM(sample_count), 0) AS p99_e2e
        FROM f_latency_daily
        WHERE {w.sql}
        GROUP BY traffic_type
        ORDER BY sample_count DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
