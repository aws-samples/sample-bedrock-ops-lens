"""Agents & MCP tab endpoints — AgentCore metrics.

Source: f_daily_agentcore (generic metric-per-row table fed by
cw_agentcore.py from AWS/Bedrock-AgentCore + bedrock-agentcore namespaces).
Routes tolerate a missing table (created at the module's first ingest run)
by returning [] so the UI shows its governance empty-state.
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


@router.get("/agents/summary")
async def agents_summary(f: FilterSet = Depends(parse_filters)):
    """Per runtime/agent resource: invocations, sessions, errors, latency.
    Pivots the metric-per-row table into one row per resource."""
    w = build_where(f, has_traffic_type=False)
    return await _safe_fetch(
        f"""
        SELECT resource_type, resource_id,
          SUM(value) FILTER (WHERE metric_name = 'Invocations'   AND stat = 'sum')::BIGINT AS invocations,
          SUM(value) FILTER (WHERE metric_name IN ('SessionCount','ActiveSessionCount') AND stat = 'sum')::BIGINT AS sessions,
          SUM(value) FILTER (WHERE metric_name IN ('SystemErrors','UserErrors','TotalErrors','Errors') AND stat = 'sum')::BIGINT AS errors,
          SUM(value) FILTER (WHERE metric_name = 'Throttles'     AND stat = 'sum')::BIGINT AS throttles,
          MAX(value) FILTER (WHERE metric_name IN ('Latency','Duration') AND stat = 'p99') AS p99_latency_ms,
          AVG(value) FILTER (WHERE metric_name IN ('Latency','Duration') AND stat = 'average') AS avg_latency_ms
        FROM f_daily_agentcore
        WHERE {w.sql} AND resource_type IN ('runtime', 'account')
        GROUP BY resource_type, resource_id
        ORDER BY invocations DESC NULLS LAST
        """,
        *w.params,
    )


@router.get("/agents/gateway-tools")
async def agents_gateway_tools(f: FilterSet = Depends(parse_filters)):
    """MCP tool call view: gateway metrics broken down by tool Name /
    TargetType dimension."""
    w = build_where(f, has_traffic_type=False)
    return await _safe_fetch(
        f"""
        SELECT resource_type, resource_id, metric_name,
               SUM(value) FILTER (WHERE stat = 'sum')::BIGINT AS total,
               MAX(value) FILTER (WHERE stat = 'p99') AS p99,
               AVG(value) FILTER (WHERE stat = 'average') AS avg
        FROM f_daily_agentcore
        WHERE {w.sql} AND resource_type IN ('gateway', 'tool')
        GROUP BY resource_type, resource_id, metric_name
        ORDER BY total DESC NULLS LAST
        """,
        *w.params,
    )


@router.get("/agents/metrics-inventory")
async def agents_metrics_inventory(f: FilterSet = Depends(parse_filters)):
    """Diagnostic: which AgentCore metrics/namespaces were discovered.
    Useful to distinguish 'no AgentCore usage' from 'wrong namespace'."""
    w = build_where(f, has_traffic_type=False)
    return await _safe_fetch(
        f"""
        SELECT namespace, resource_type, metric_name, COUNT(*)::BIGINT AS datapoints
        FROM f_daily_agentcore
        WHERE {w.sql}
        GROUP BY namespace, resource_type, metric_name
        ORDER BY namespace, resource_type, metric_name
        """,
        *w.params,
    )
