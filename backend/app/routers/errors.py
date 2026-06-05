"""Errors tab endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


@router.get("/errors-by-model")
async def errors_by_model(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(failed_requests)::BIGINT  AS failed_requests,
          SUM(status_400_count)::BIGINT AS status_400,
          SUM(status_403_count)::BIGINT AS status_403,
          SUM(status_429_count)::BIGINT AS status_429,
          SUM(status_500_count)::BIGINT AS status_500,
          SUM(status_503_count)::BIGINT AS status_503
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(failed_requests) > 0
        ORDER BY failed_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/errors-by-account")
async def errors_by_account(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(failed_requests)::BIGINT  AS failed_requests,
          SUM(status_400_count)::BIGINT AS status_400,
          SUM(status_403_count)::BIGINT AS status_403,
          SUM(status_429_count)::BIGINT AS status_429,
          SUM(status_500_count)::BIGINT AS status_500,
          SUM(status_503_count)::BIGINT AS status_503
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(failed_requests) > 0
        ORDER BY failed_requests DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/errors-daily-trend")
async def errors_daily_trend(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT year, month, day,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(failed_requests)::BIGINT  AS failed_requests,
          SUM(status_400_count)::BIGINT AS status_400,
          SUM(status_403_count)::BIGINT AS status_403,
          SUM(status_429_count)::BIGINT AS status_429,
          SUM(status_500_count)::BIGINT AS status_500,
          SUM(status_503_count)::BIGINT AS status_503
        FROM f_daily
        WHERE {w.sql}
        GROUP BY year, month, day
        ORDER BY year, month, day
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/errors-hourly-trend")
async def errors_hourly_trend(
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    day: int = Query(..., ge=1, le=31),
):
    """Hourly drill-down for one specific day. Reads f_hourly_errors (rolling 7-day)."""
    rows = await db.fetch(
        """
        SELECT hour,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(failed_requests)::BIGINT  AS failed_requests,
          SUM(status_400_count)::BIGINT AS status_400,
          SUM(status_403_count)::BIGINT AS status_403,
          SUM(status_429_count)::BIGINT AS status_429,
          SUM(status_500_count)::BIGINT AS status_500,
          SUM(status_503_count)::BIGINT AS status_503
        FROM f_hourly_errors
        WHERE event_date = make_date($1, $2, $3)
        GROUP BY hour
        ORDER BY hour
        """,
        year, month, day,
    )
    return db.rows_to_dicts(rows)
