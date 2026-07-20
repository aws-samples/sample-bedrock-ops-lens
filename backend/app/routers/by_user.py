"""By-User tab endpoints — per IAM caller identity attribution.

Data source: f_daily_by_identity, populated by the invocation-log ingester
from the `identity.arn` field Bedrock stamps on every invocation log record.
This is the attribution that is "always present with no per-call code"
(per the Bedrock cost-management FAQ) — unlike request-metadata tags, which
require every caller to opt in.

Filter notes: the table has no traffic_type or operation column, so
build_where is called with has_traffic_type=False. Tag filters don't apply
here either (identity is orthogonal to request metadata).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


@router.get("/by-user/summary")
async def by_user_summary(
    f: FilterSet = Depends(parse_filters),
    top_n: int = Query(25, ge=1, le=200),
):
    """Top principals by requests in the window, with token totals and the
    number of distinct models each principal used."""
    w = build_where(f, has_traffic_type=False)
    rows = await db.fetch(
        f"""
        SELECT
          principal_arn,
          MAX(principal_label)                       AS principal_label,
          SUM(total_requests)::BIGINT                AS total_requests,
          SUM(failed_requests)::BIGINT               AS failed_requests,
          SUM(total_input_tokens)::BIGINT            AS input_tokens,
          SUM(total_output_tokens)::BIGINT           AS output_tokens,
          COUNT(DISTINCT modelId)::BIGINT            AS distinct_models,
          COUNT(DISTINCT accountId)::BIGINT          AS distinct_accounts
        FROM f_daily_by_identity
        WHERE {w.sql}
        GROUP BY principal_arn
        ORDER BY total_requests DESC
        LIMIT {int(top_n)}
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/by-user/by-model")
async def by_user_by_model(f: FilterSet = Depends(parse_filters)):
    """Principal × model split — which models each caller uses."""
    w = build_where(f, has_traffic_type=False)
    rows = await db.fetch(
        f"""
        SELECT
          principal_arn,
          MAX(principal_label)             AS principal_label,
          modelId,
          SUM(total_requests)::BIGINT      AS total_requests,
          SUM(total_input_tokens)::BIGINT  AS input_tokens,
          SUM(total_output_tokens)::BIGINT AS output_tokens
        FROM f_daily_by_identity
        WHERE {w.sql}
        GROUP BY principal_arn, modelId
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/by-user/daily-trend")
async def by_user_daily_trend(
    f: FilterSet = Depends(parse_filters),
    top_n: int = Query(8, ge=1, le=30),
):
    """Daily request trend for the top-N principals in the window."""
    w = build_where(f, has_traffic_type=False)
    rows = await db.fetch(
        f"""
        WITH top AS (
          SELECT principal_arn
          FROM f_daily_by_identity
          WHERE {w.sql}
          GROUP BY principal_arn
          ORDER BY SUM(total_requests) DESC
          LIMIT {int(top_n)}
        )
        SELECT
          event_date,
          principal_arn,
          MAX(principal_label)        AS principal_label,
          SUM(total_requests)::BIGINT AS total_requests
        FROM f_daily_by_identity
        WHERE {w.sql} AND principal_arn IN (SELECT principal_arn FROM top)
        GROUP BY event_date, principal_arn
        ORDER BY event_date, total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
