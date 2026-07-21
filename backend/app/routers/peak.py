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
    return db.rows_to_dicts(rows)
