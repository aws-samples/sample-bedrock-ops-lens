"""Overview tab endpoints: totals, daily trend, by-model/region/op/traffic-type."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


@router.get("/summary")
async def summary(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetchrow(
        f"""
        SELECT
          COALESCE(SUM(total_requests), 0)::BIGINT       AS total_requests,
          COALESCE(SUM(successful_requests), 0)::BIGINT  AS successful_requests,
          COALESCE(SUM(failed_requests), 0)::BIGINT      AS failed_requests,
          COALESCE(SUM(total_input_tokens), 0)::BIGINT   AS total_input_tokens,
          COALESCE(SUM(total_output_tokens), 0)::BIGINT  AS total_output_tokens,
          COALESCE(SUM(status_429_count), 0)::BIGINT     AS throttled_requests,
          COALESCE(SUM(status_500_count + status_503_count), 0)::BIGINT AS server_errors,
          COUNT(DISTINCT accountId)::BIGINT              AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        """,
        *w.params,
    )
    return dict(rows)


@router.get("/daily-trend")
async def daily_trend(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT year, month, day,
          SUM(total_requests)::BIGINT       AS total_requests,
          SUM(successful_requests)::BIGINT  AS successful_requests,
          SUM(failed_requests)::BIGINT      AS failed_requests,
          SUM(total_input_tokens)::BIGINT   AS input_tokens,
          SUM(total_output_tokens)::BIGINT  AS output_tokens,
          SUM(total_cache_read_input_tokens)::BIGINT AS cache_read_tokens,
          SUM(status_429_count)::BIGINT     AS throttled
        FROM f_daily
        WHERE {w.sql}
        GROUP BY year, month, day
        ORDER BY year, month, day
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/daily-breakdown")
async def daily_breakdown(
    f: FilterSet = Depends(parse_filters),
    group_by: str = Query("model", pattern="^(model|provider|traffic|region)$"),
    top_n: int = Query(8, ge=1, le=20),
):
    """Stacked bar data: top-N categories per day + 'Other' bucket."""
    w = build_where(f)
    if group_by == "model":
        cat_expr = "modelId"
    elif group_by == "provider":
        cat_expr = "split_part(modelId, '.', 1)"
    elif group_by == "traffic":
        cat_expr = "traffic_type"
    else:  # region
        cat_expr = "region"

    # First pass: pick top-N categories overall.
    top_rows = await db.fetch(
        f"""
        SELECT {cat_expr} AS cat, SUM(total_requests)::BIGINT AS total
        FROM f_daily
        WHERE {w.sql}
        GROUP BY {cat_expr}
        ORDER BY total DESC
        LIMIT {top_n}
        """,
        *w.params,
    )
    top_cats = [r["cat"] for r in top_rows]

    if not top_cats:
        return []

    # Second pass: per-day per-category, with non-top folded into 'Other'.
    rows = await db.fetch(
        f"""
        SELECT year, month, day,
          CASE WHEN {cat_expr} = ANY(${len(w.params)+1}::text[])
               THEN {cat_expr} ELSE 'Other' END AS category,
          SUM(total_requests)::BIGINT AS total_requests
        FROM f_daily
        WHERE {w.sql}
        GROUP BY year, month, day, category
        ORDER BY year, month, day, category
        """,
        *w.params, top_cats,
    )
    return db.rows_to_dicts(rows)


@router.get("/requests-by-model")
async def requests_by_model(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_requests)::BIGINT      AS total_requests,
          SUM(total_input_tokens)::BIGINT  AS input_tokens,
          SUM(total_output_tokens)::BIGINT AS output_tokens
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/traffic-types")
async def traffic_types(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT traffic_type, inference_profile_prefix,
          SUM(total_requests)::BIGINT      AS total_requests,
          SUM(total_input_tokens)::BIGINT  AS input_tokens,
          SUM(total_output_tokens)::BIGINT AS output_tokens
        FROM f_daily
        WHERE {w.sql}
        GROUP BY traffic_type, inference_profile_prefix
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/operations")
async def operations(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT operation,
          SUM(total_requests)::BIGINT      AS total_requests,
          SUM(failed_requests)::BIGINT     AS failed_requests,
          SUM(total_input_tokens)::BIGINT  AS input_tokens,
          SUM(total_output_tokens)::BIGINT AS output_tokens
        FROM f_daily
        WHERE {w.sql}
        GROUP BY operation
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/regions")
async def regions(f: FilterSet = Depends(parse_filters)):
    """Per-region rollup. Forces region='all' (always groups by region regardless
    of the user's region selection)."""
    overridden = FilterSet(start=f.start, end=f.end, provider=f.provider, region="all",
                           accounts=f.accounts, traffic_type=f.traffic_type,
                           tag_filter=f.tag_filter)
    w = build_where(overridden)
    rows = await db.fetch(
        f"""
        SELECT region,
          SUM(total_requests)::BIGINT       AS total_requests,
          SUM(failed_requests)::BIGINT      AS failed_requests,
          SUM(status_429_count)::BIGINT     AS throttled,
          SUM(total_input_tokens)::BIGINT   AS input_tokens,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        GROUP BY region
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
