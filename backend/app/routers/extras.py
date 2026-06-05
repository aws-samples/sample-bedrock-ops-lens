"""Endpoints required by Overview / Ops Insights / Ops Review tabs that aren't
covered by the basic per-tab routers. Kept separate so the basic routers stay
focused.

Endpoints:
  GET  /api/wow-comparison       — week-over-week deltas for KPI ribbon badges
  GET  /api/account-type-split   — split by account category (placeholder; the
                                   internal version distinguished Internal vs
                                   External, customer version groups by
                                   accountId — kept for shape parity)
  GET  /api/region-model-matrix  — pivot table: top N models × region
  GET  /api/account-detail       — per (account, model, region, op, traffic)
                                   detail rows for a given account list
  GET  /api/lifecycle-status     — model lifecycle alerts (Active/Legacy/EOL),
                                   joined against fleet usage so the UI can
                                   show only alerts for models the fleet uses
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


# ---------------------------------------------------------------------------
# /api/wow-comparison
# ---------------------------------------------------------------------------
@router.get("/wow-comparison")
async def wow_comparison():
    """Compare last 7 days against the prior 7 days. Fleet-wide; ignores
    filters by design (the WoW pill is a fleet-health signal, not a slice).

    Returns:
      {
        "current":  {total_requests, unique_accounts, total_input_tokens, ...},
        "previous": {... same fields ...}
      }
    """
    today = date.today()
    cur_start = today - timedelta(days=6)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    async def _row(start_d, end_d):
        return await db.fetchrow(
            """
            SELECT
              COALESCE(SUM(total_requests), 0)::BIGINT AS total_requests,
              COUNT(DISTINCT accountId)::BIGINT AS unique_accounts,
              COALESCE(SUM(total_input_tokens), 0)::BIGINT AS total_input_tokens,
              COALESCE(SUM(total_output_tokens), 0)::BIGINT AS total_output_tokens,
              COALESCE(SUM(failed_requests), 0)::BIGINT AS failed_requests,
              COALESCE(SUM(status_429_count), 0)::BIGINT AS throttled_requests
            FROM f_daily
            WHERE event_date BETWEEN $1 AND $2
            """,
            start_d, end_d,
        )

    cur = await _row(cur_start, today)
    prev = await _row(prev_start, prev_end)
    return {"current": dict(cur), "previous": dict(prev)}


# ---------------------------------------------------------------------------
# /api/account-type-split
# ---------------------------------------------------------------------------
@router.get("/account-type-split")
async def account_type_split(f: FilterSet = Depends(parse_filters)):
    """Split by account_type. The internal version distinguished
    Internal/External; the customer version doesn't have that dimension —
    we group by accountId and label each as "Account <id>" so the pie still
    has meaningful slices."""
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId AS account_type,
               SUM(total_requests)::BIGINT AS total_requests,
               1::BIGINT AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId
        ORDER BY total_requests DESC
        LIMIT 8
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# /api/region-model-matrix
# ---------------------------------------------------------------------------
@router.get("/region-model-matrix")
async def region_model_matrix(f: FilterSet = Depends(parse_filters)):
    """Top-N models × region pivot. Forces region='all' so it's always a
    cross-region comparison. Top 8 models by total_requests; rest folded
    out (the internal version pivots client-side)."""
    overridden = FilterSet(
        start=f.start, end=f.end, provider=f.provider, region="all",
        accounts=f.accounts, traffic_type=f.traffic_type, tag_filter=f.tag_filter,
    )
    w = build_where(overridden)
    # Pick top-N models in the window first.
    top_rows = await db.fetch(
        f"""
        SELECT modelId, SUM(total_requests)::BIGINT AS total
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        ORDER BY total DESC
        LIMIT 8
        """,
        *w.params,
    )
    top_models = [r["modelid"] if "modelid" in r else r["modelId"] for r in top_rows]
    if not top_models:
        return []

    rows = await db.fetch(
        f"""
        SELECT region, modelId, SUM(total_requests)::BIGINT AS total_requests
        FROM f_daily
        WHERE {w.sql} AND modelId = ANY(${len(w.params)+1}::text[])
        GROUP BY region, modelId
        """,
        *w.params, top_models,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# /api/account-detail
# ---------------------------------------------------------------------------
@router.get("/account-detail")
async def account_detail(
    account_id: str = Query(..., description="comma-separated 12-digit IDs"),
    days: int = Query(7, ge=1, le=90),
):
    """Per (account, model, region, op, traffic_type) detail rows for the
    given accounts in the last N days. Used by the Ops Review "Detailed
    breakdown" lazy-loaded section."""
    accts = tuple(
        a.strip() for a in account_id.split(",")
        if a.strip().isdigit() and len(a.strip()) == 12
    )
    if not accts:
        return []

    rows = await db.fetch(
        """
        SELECT accountId, modelId, region, operation, traffic_type,
               SUM(total_requests)::BIGINT AS total_requests,
               SUM(failed_requests)::BIGINT AS failed_requests,
               SUM(total_input_tokens)::BIGINT AS total_input_tokens,
               SUM(total_output_tokens)::BIGINT AS total_output_tokens,
               SUM(status_429_count)::BIGINT AS throttled
        FROM f_daily
        WHERE event_date >= current_date - $1::int
          AND accountId = ANY($2::text[])
        GROUP BY accountId, modelId, region, operation, traffic_type
        ORDER BY total_requests DESC
        LIMIT 1000
        """,
        days, list(accts),
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# /api/lifecycle-status
# ---------------------------------------------------------------------------
@router.get("/quotas")
async def list_quotas(f: FilterSet = Depends(parse_filters)):
    """All Bedrock service quotas the dashboard has ingested. Optionally
    filtered to selected accounts/regions. Used by the Quotas tab to
    overlay applied limits on peak utilization charts."""
    parts = []
    params: list = []
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    where_sql = (" WHERE " + " AND ".join(parts)) if parts else ""

    rows = await db.fetch(
        f"""
        SELECT accountId, region, quota_code, quota_name, model_name,
               traffic_type, metric, default_value, applied_value,
               adjustable, last_refreshed_at
        FROM f_quotas
        {where_sql}
        ORDER BY model_name, traffic_type, metric
        """,
        *params,
    )
    return [
        {
            "accountId":       r["accountid"] if "accountid" in r else r["accountId"],
            "region":          r["region"],
            "quota_code":      r["quota_code"],
            "quota_name":      r["quota_name"],
            "model_name":      r["model_name"],
            "traffic_type":    r["traffic_type"],
            "metric":          r["metric"],
            "default_value":   float(r["default_value"]) if r["default_value"] is not None else None,
            "applied_value":   float(r["applied_value"]) if r["applied_value"] is not None else None,
            "adjustable":      r["adjustable"],
            "last_refreshed":  r["last_refreshed_at"].isoformat() if r["last_refreshed_at"] else None,
        }
        for r in rows
    ]


_LIFECYCLE_PATH = Path(__file__).parent.parent / "ops_review" / "model_lifecycle_dates.json"


def _load_lifecycle() -> dict:
    if not _LIFECYCLE_PATH.exists():
        return {"models": {}, "_updated": None, "_source": None}
    with _LIFECYCLE_PATH.open() as fh:
        return json.load(fh)


@router.get("/lifecycle-status")
async def lifecycle_status(f: FilterSet = Depends(parse_filters)):
    """Lifecycle alerts joined to fleet usage in the window. Returns one
    record per model the fleet actually uses, with severity:
      critical = past EOL,
      warning  = legacy (LEGACY status reached),
      info     = legacy date approaching within 90 days.
    """
    lifecycle = _load_lifecycle()
    models = lifecycle.get("models", {}) or {}

    w = build_where(f)
    used = await db.fetch(
        f"""
        SELECT modelId, SUM(total_requests)::BIGINT AS total_requests,
               COUNT(DISTINCT accountId)::BIGINT AS account_count
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        """,
        *w.params,
    )

    today = date.today()
    out: list[dict] = []
    for r in used:
        mid_raw = r["modelid"] if "modelid" in r else r["modelId"]
        # Lifecycle JSON uses bare model IDs without the `us.`/`eu.`/`global.`
        # CRIS prefix; strip prefix if present so we can match.
        bare = mid_raw
        for pfx in ("us.", "eu.", "global.", "apac.", "amer.", "jp.", "au.", "ca."):
            if bare.startswith(pfx):
                bare = bare[len(pfx):]
                break

        meta = models.get(bare)
        if not meta:
            continue  # not a tracked model
        legacy_str = meta.get("legacy_date")
        eol_str = meta.get("eol_date")
        try:
            legacy_d = date.fromisoformat(legacy_str) if legacy_str else None
            eol_d = date.fromisoformat(eol_str) if eol_str else None
        except ValueError:
            continue

        severity = None
        if eol_d and today >= eol_d:
            severity = "critical"
        elif legacy_d and today >= legacy_d:
            severity = "warning"
        elif legacy_d and (legacy_d - today).days <= 90:
            severity = "info"
        if not severity:
            continue
        out.append({
            "modelId": mid_raw,
            "base_modelId": bare,
            "severity": severity,
            "legacy_date": legacy_str,
            "eol_date": eol_str,
            "extended_access_date": meta.get("extended_access_date"),
            "total_requests": int(r["total_requests"]),
            "account_count": int(r["account_count"]),
        })
    out.sort(key=lambda x: ({"critical": 0, "warning": 1, "info": 2}.get(x["severity"], 3),
                              -x["total_requests"]))
    return {
        "alerts": out,
        "meta": {
            "source": lifecycle.get("_source"),
            "updated": lifecycle.get("_updated"),
            "model_count": len(models),
        },
    }
