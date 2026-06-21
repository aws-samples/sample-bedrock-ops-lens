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


@router.get("/status-codes")
async def status_codes(f: FilterSet = Depends(parse_filters)):
    """Real per-HTTP-status-code hourly breakdown for the "Status Codes" chart.

    Source: f_hourly_status, populated ONLY from Bedrock model invocation logs
    (which carry a genuine per-request errorCode). CloudWatch metrics can't
    distinguish individual codes — they only expose all-4xx / all-5xx — so when
    a customer hasn't enabled invocation logging this table is empty.

    Degrades gracefully: never errors. Returns
      {
        "logging_enabled": bool,   # true only if invocation logs have produced data
        "last_log_refresh": iso8601 | null,
        "series": [ {ts, total, ok, s400, s403, s404, s408, s424, s429, s500, s503}, ... ]
      }
    When logging_enabled is false, `series` is empty and the UI shows a note +
    falls back to the CloudWatch 4xx/5xx aggregates from the other charts.
    """
    # Window + optional account/region/provider filters (f_hourly_status has no
    # traffic_type/tag columns, so only the dimensions it carries are applied).
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
    where = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        SELECT (event_date::timestamp + (hour || ' hours')::interval) AS ts,
          SUM(total_requests)::BIGINT    AS total,
          SUM(status_200_count)::BIGINT  AS ok,
          SUM(status_400_count)::BIGINT  AS s400,
          SUM(status_403_count)::BIGINT  AS s403,
          SUM(status_404_count)::BIGINT  AS s404,
          SUM(status_408_count)::BIGINT  AS s408,
          SUM(status_424_count)::BIGINT  AS s424,
          SUM(status_429_count)::BIGINT  AS s429,
          SUM(status_500_count)::BIGINT  AS s500,
          SUM(status_503_count)::BIGINT  AS s503
        FROM f_hourly_status
        WHERE {where}
        GROUP BY ts
        ORDER BY ts
        """,
        *params,
    )

    # logging_enabled: real data present in window AND the ingester has recorded
    # at least one invocation-logs run. Either alone is enough to say "on", but
    # we surface the refresh timestamp regardless for the freshness note.
    refresh_row = await db.fetch(
        "SELECT value FROM ingestion_meta WHERE key = 'last_invocation_logs_refresh'"
    )
    last_refresh = refresh_row[0]["value"] if refresh_row else None

    series = [
        {
            "ts": r["ts"].isoformat() if r["ts"] else None,
            "total": int(r["total"] or 0),
            "ok":   int(r["ok"] or 0),
            "s400": int(r["s400"] or 0),
            "s403": int(r["s403"] or 0),
            "s404": int(r["s404"] or 0),
            "s408": int(r["s408"] or 0),
            "s424": int(r["s424"] or 0),
            "s429": int(r["s429"] or 0),
            "s500": int(r["s500"] or 0),
            "s503": int(r["s503"] or 0),
        }
        for r in rows
    ]
    return {
        "logging_enabled": bool(series),
        "last_log_refresh": last_refresh,
        "series": series,
    }
