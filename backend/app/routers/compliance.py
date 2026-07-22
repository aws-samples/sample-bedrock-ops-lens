"""Compliance tab endpoints — Guardrails intervention metrics.

Source: f_daily_guardrails (CloudWatch AWS/Bedrock/Guardrails namespace,
ingested by cw_guardrails.py). Routes tolerate a missing table (the table
is created by the ingester's first run, which may happen after deploy):
they return [] instead of 500 so the UI shows its empty-state.
"""
from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


async def _safe_fetch(sql: str, *params):
    try:
        return db.rows_to_dicts(await db.fetch(sql, *params))
    except asyncpg.exceptions.UndefinedTableError:
        return []


@router.get("/compliance/summary")
async def compliance_summary(f: FilterSet = Depends(parse_filters)):
    """Interventions by policy type. Uses the PolicyType grain
    (only published for InvocationsIntervened / TextUnitCount)."""
    w = build_where(f, has_traffic_type=False)
    return await _safe_fetch(
        f"""
        SELECT policy_type,
               SUM(intervened)::BIGINT AS intervened,
               SUM(text_units)::BIGINT AS text_units
        FROM f_daily_guardrails
        WHERE {w.sql} AND policy_type <> '__all__'
        GROUP BY policy_type
        ORDER BY intervened DESC
        """,
        *w.params,
    )


@router.get("/compliance/totals")
async def compliance_totals(f: FilterSet = Depends(parse_filters)):
    """Overall invocations vs interventions (the __all__ grain carries
    Invocations, which is NOT published per policy type)."""
    w = build_where(f, has_traffic_type=False)
    rows = await _safe_fetch(
        f"""
        SELECT SUM(invocations)::BIGINT AS invocations,
               SUM(intervened)::BIGINT  AS intervened,
               SUM(text_units)::BIGINT  AS text_units
        FROM f_daily_guardrails
        WHERE {w.sql} AND policy_type = '__all__' AND content_source = '__all__'
        """,
        *w.params,
    )
    return rows[0] if rows else {"invocations": 0, "intervened": 0, "text_units": 0}


@router.get("/compliance/by-guardrail")
async def compliance_by_guardrail(f: FilterSet = Depends(parse_filters)):
    w = build_where(f, has_traffic_type=False)
    return await _safe_fetch(
        f"""
        SELECT guardrail_arn, guardrail_version,
               SUM(invocations)::BIGINT AS invocations,
               SUM(intervened)::BIGINT  AS intervened
        FROM f_daily_guardrails
        WHERE {w.sql} AND policy_type = '__all__' AND content_source = '__all__'
        GROUP BY guardrail_arn, guardrail_version
        ORDER BY intervened DESC
        """,
        *w.params,
    )


@router.get("/compliance/daily-trend")
async def compliance_daily_trend(f: FilterSet = Depends(parse_filters)):
    w = build_where(f, has_traffic_type=False)
    return await _safe_fetch(
        f"""
        SELECT event_date,
               SUM(invocations)::BIGINT AS invocations,
               SUM(intervened)::BIGINT  AS intervened
        FROM f_daily_guardrails
        WHERE {w.sql} AND policy_type = '__all__' AND content_source = '__all__'
        GROUP BY event_date
        ORDER BY event_date
        """,
        *w.params,
    )
