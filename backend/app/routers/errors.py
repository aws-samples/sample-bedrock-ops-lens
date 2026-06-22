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
        "state": "ok" | "no_logging" | "no_data" | "out_of_window",
        "logging_enabled": bool,   # has the invocation-logs ingester ever run?
        "last_log_refresh": iso8601 | null,
        "available_range": {"min": "YYYY-MM-DD", "max": "YYYY-MM-DD"} | null,
        "series": [ {ts, total, ok, s400, s403, s404, s408, s424, s429, s500, s503}, ... ]
      }

    `state` tells the UI WHICH accurate message to show — these are distinct
    situations the old `bool(series)` flag wrongly collapsed into one:
      ok            — rows exist in the selected window; render the chart.
      no_logging    — the invocation-logs ingester has never run (no
                      last_invocation_logs_refresh marker AND table empty):
                      logging likely not enabled / not wired.
      no_data       — ingester has run but produced zero rows at all (logging
                      on, but no per-request log records ingested yet).
      out_of_window — rows DO exist, just not in the selected date range; tell
                      the user to widen/shift the window (here's the range).
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

    # Distinguish the three "no chart" situations instead of always blaming
    # "logging not enabled" (the old bug). Two extra cheap probes:
    #   1. Has the invocation-logs ingester ever run? (meta marker)
    #   2. Does f_hourly_status hold ANY rows, and over what date range?
    refresh_row = await db.fetch(
        "SELECT value FROM ingestion_meta WHERE key = 'last_invocation_logs_refresh'"
    )
    last_refresh = refresh_row[0]["value"] if refresh_row else None
    ingester_has_run = last_refresh is not None

    range_row = await db.fetch(
        "SELECT MIN(event_date) AS mn, MAX(event_date) AS mx, COUNT(*) AS n FROM f_hourly_status"
    )
    total_rows = int(range_row[0]["n"] or 0) if range_row else 0
    available_range = None
    if total_rows > 0 and range_row[0]["mn"] is not None:
        available_range = {
            "min": range_row[0]["mn"].isoformat(),
            "max": range_row[0]["mx"].isoformat(),
        }

    if series:
        state = "ok"
    elif total_rows > 0:
        # Data exists in the table, just not in the selected window/filters.
        state = "out_of_window"
    elif ingester_has_run:
        # Ingester ran but produced zero rows: logging is wired, but there are
        # no per-request log records to attribute (e.g. logs not flowing yet).
        state = "no_data"
    else:
        # No marker and no rows: the invocation-logs path has never run.
        state = "no_logging"

    return {
        "state": state,
        "logging_enabled": ingester_has_run,
        "last_log_refresh": last_refresh,
        "available_range": available_range,
        "series": series,
    }
