"""Overview tab endpoints: totals, daily trend, by-model/region/op/traffic-type."""
from __future__ import annotations

from dataclasses import replace

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
    out = dict(rows)

    # Per-endpoint breakdown for the Overview KPI tiles — each tile shows the
    # total plus a "runtime X · mantle Y" split without a second round-trip.
    # has_endpoint=False so the tab's active endpoint filter is ignored here
    # and BOTH slices always come back (respecting all OTHER filters).
    wb = build_where(f, has_endpoint=False)
    ep_rows = await db.fetch(
        f"""
        SELECT endpoint,
          COALESCE(SUM(total_requests), 0)::BIGINT       AS total_requests,
          COALESCE(SUM(failed_requests), 0)::BIGINT      AS failed_requests,
          COALESCE(SUM(total_input_tokens), 0)::BIGINT   AS total_input_tokens,
          COALESCE(SUM(total_output_tokens), 0)::BIGINT  AS total_output_tokens,
          COALESCE(SUM(status_429_count), 0)::BIGINT     AS throttled_requests,
          COALESCE(SUM(status_500_count + status_503_count), 0)::BIGINT AS server_errors,
          COUNT(DISTINCT accountId)::BIGINT              AS unique_accounts
        FROM f_daily
        WHERE {wb.sql}
        GROUP BY endpoint
        """,
        *wb.params,
    )
    by_endpoint = {"runtime": {}, "mantle": {}}
    for r in ep_rows:
        d = dict(r)
        ep = d.pop("endpoint", "runtime")
        if ep not in ("runtime", "mantle"):
            ep = "runtime"
        by_endpoint[ep] = d
    out["by_endpoint"] = by_endpoint
    return out


@router.get("/daily-trend")
async def daily_trend(f: FilterSet = Depends(parse_filters)):
    # Endpoint filter is honored normally (has_endpoint default True): when the
    # tab is scoped to one endpoint, total_* reflects only that endpoint. The
    # extra runtime_requests / mantle_requests conditional sums let the Overview
    # trend chart stack the split in the default 'all' view (in a single-endpoint
    # view the other slice is simply 0 — correct).
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
          -- Needed for the cached-prompt-token share: writes are a third
          -- disjoint counter and belong in the denominator (finding 14).
          SUM(total_cache_write_input_tokens)::BIGINT AS cache_write_tokens,
          SUM(status_429_count)::BIGINT     AS throttled,
          SUM(CASE WHEN endpoint = 'runtime' THEN total_requests ELSE 0 END)::BIGINT AS runtime_requests,
          SUM(CASE WHEN endpoint = 'mantle'  THEN total_requests ELSE 0 END)::BIGINT AS mantle_requests
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
    """Requests by API operation (Converse / InvokeModel / *Stream).

    The AWS/Bedrock CloudWatch metrics carry NO operation dimension, so f_daily
    only ever has the '__none__' sentinel. The real per-operation split comes
    from Bedrock model invocation logs -> f_daily_tagged (which has an
    `operation` column).

    f_daily_tagged fans out one row per requestMetadata tag_key, so summing all
    rows multiply-counts. The old code restricted to MIN(tag_key) on the stated
    assumption that "each key covers 100% of requests" - which is false: tags
    are sparse, so a request tagged only `team` is absent from tag_key
    ='application' and an untagged request is absent from every real key. Worse,
    MIN() picks the alphabetically first key, not the most complete one, so the
    operation totals were an arbitrary undercount of the fleet (audit finding
    16).

    The ingester now writes an '__all__' accounting row per request, so reading
    that key gives exact totals. For data ingested before that change there is
    no such row; we fall back to the WIDEST real tag_key and report the coverage
    so the UI can say the split is partial instead of implying a fleet total.
    """
    w = build_where(f, has_traffic_type=False, has_endpoint=False)
    rows = await db.fetch(
        f"""
        WITH scoped AS (
            SELECT * FROM f_daily_tagged WHERE {w.sql}
        ),
        -- Exact when the '__all__' accounting row exists; otherwise the tag_key
        -- with the greatest request coverage, which is the best available lower
        -- bound.
        chosen AS (
            SELECT tag_key, SUM(total_requests) AS reqs
            FROM scoped
            GROUP BY tag_key
            ORDER BY (tag_key = '__all__') DESC, SUM(total_requests) DESC
            LIMIT 1
        )
        SELECT operation,
          SUM(total_requests)::BIGINT      AS total_requests,
          SUM(failed_requests)::BIGINT     AS failed_requests,
          SUM(total_input_tokens)::BIGINT  AS input_tokens,
          SUM(total_output_tokens)::BIGINT AS output_tokens,
          (SELECT tag_key = '__all__' FROM chosen) AS exact,
          (SELECT tag_key FROM chosen)             AS basis_tag_key
        FROM scoped
        WHERE tag_key = (SELECT tag_key FROM chosen)
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
    # replace() keeps every OTHER field. Hand-constructing a new FilterSet
    # silently dropped `endpoint` (and would drop any field added later), so the
    # Mantle region chart summed 23,204,626 requests against a 208,705 Mantle
    # headline — 111x (audit finding 11).
    overridden = replace(f, region="all")
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
