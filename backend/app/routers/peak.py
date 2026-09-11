"""Peak Hours tab. Hour-of-day heatmap from f_hourly_peak."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, parse_filters

router = APIRouter()


@router.get("/hourly-heatmap")
async def hourly_heatmap(f: FilterSet = Depends(parse_filters)):
    """f_hourly_peak only has account/model/region — no operation/traffic_type/etc.
    So we apply only filters this table can express."""
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.provider != "all":
        from ..filters import PROVIDER_PREFIX
        parts.append(f"modelId LIKE ${len(params)+1}")
        params.append(PROVIDER_PREFIX[f.provider] + "%")
    if f.endpoint != "all":
        parts.append(f"endpoint = ${len(params)+1}")
        params.append(f.endpoint)
    where_sql = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        SELECT hour,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(status_429_count)::BIGINT AS throttled
        FROM f_hourly_peak
        WHERE {where_sql}
        GROUP BY hour
        ORDER BY hour
        """,
        *params,
    )
    # Report the window actually covered. f_hourly_peak is loaded by the
    # ingester's rolling lookback (INGESTER_DAYS_DEFAULT, 14 days by default),
    # but the filter bar offers up to 90 - so the panel used to title itself
    # "last 90 days" over 14 days of data (audit finding 17). The UI now states
    # the covered span whenever it is shorter than the selection.
    cov = await db.fetchrow(
        f"""
        SELECT MIN(event_date) AS min_date, MAX(event_date) AS max_date,
               COUNT(DISTINCT event_date)::INT AS days_covered
        FROM f_hourly_peak
        WHERE {where_sql}
        """,
        *params,
    )
    requested_days = (f.end - f.start).days + 1
    return {
        "rows": db.rows_to_dicts(rows),
        "coverage": {
            "min_date": cov["min_date"].isoformat() if cov and cov["min_date"] else None,
            "max_date": cov["max_date"].isoformat() if cov and cov["max_date"] else None,
            "days_covered": int(cov["days_covered"] or 0) if cov else 0,
            "days_requested": requested_days,
            "hour_basis": "utc",
        },
    }
