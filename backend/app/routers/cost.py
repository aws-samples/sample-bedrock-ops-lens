"""Cost endpoints — backed by f_daily_cost (populated by ingestion.cost
from AWS Cost Explorer).

Endpoints:
  GET /api/cost-summary       totals over the window + previous-window deltas
  GET /api/cost-by-model      daily stacked-bar series keyed by service
                              (CE often returns one consolidated 'Amazon Bedrock'
                              service, in which case we derive a per-model
                              proxy from f_daily token volumes — labeled
                              'derived' so the UI can disclose it)
  GET /api/cost-by-account    per-account totals
"""
from __future__ import annotations

from dataclasses import replace

from datetime import timedelta

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, parse_filters
from .model_insights import BEDROCK_PRICING, _provider_of
from .. import cost_alloc

router = APIRouter()


# NOTE: the two former endpoint allocators (_endpoint_fraction, per-key, and
# _endpoint_cost_weights, whole-window) were DELETED. Having two bases is what
# made the headline and the daily chart disagree (audit finding 07). All cost
# widgets now derive from app/cost_alloc.allocate(), which allocates once per
# (event_date, accountId). Do not reintroduce a second allocator here.


# ---------------------------------------------------------------------------
# Filter helper — cost queries don't need traffic_type / provider filters,
# only date + accounts. Keep it surgical.
# ---------------------------------------------------------------------------
def _cost_where(f: FilterSet) -> tuple[str, list]:
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    return " AND ".join(parts), params


# ---------------------------------------------------------------------------
@router.get("/cost-summary")
async def cost_summary(f: FilterSet = Depends(parse_filters)):
    """Totals + WoW delta for the cost ribbon."""
    where_sql, params = _cost_where(f)
    cur = await db.fetchrow(
        f"""
        SELECT COALESCE(SUM(total_cost), 0)::numeric AS total_cost,
               MIN(currency)                         AS currency,
               COUNT(DISTINCT accountId)             AS unique_accounts,
               COUNT(DISTINCT service)               AS unique_services
        FROM f_daily_cost
        WHERE {where_sql}
        """,
        *params,
    )

    # Previous window of equal length, for WoW comparison.
    days = (f.end - f.start).days + 1
    prev_end = f.start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    prev_where = "event_date BETWEEN $1::date AND $2::date"
    prev_params = [prev_start, prev_end]
    if f.accounts:
        prev_where += f" AND accountId = ANY($3::text[])"
        prev_params.append(list(f.accounts))
    prev = await db.fetchrow(
        f"""
        SELECT COALESCE(SUM(total_cost), 0)::numeric AS total_cost
        FROM f_daily_cost
        WHERE {prev_where}
        """,
        *prev_params,
    )
    total = float(cur["total_cost"] or 0)

    # ONE allocation basis for every cost widget (app/cost_alloc.py): per
    # (event_date, accountId), then aggregated. The headline previously used
    # WHOLE-WINDOW token weights while the daily chart used per-day weights, and
    # those are not additive ($90@90% + $10@10% is $82 per day but $50 by
    # window). Now the headline is the sum of the same per-day numbers.
    alloc = await cost_alloc.allocate(f)
    by_endpoint = {
        "runtime": round(alloc.by_endpoint.get("runtime", 0.0), 6),
        "mantle":  round(alloc.by_endpoint.get("mantle", 0.0), 6),
        # Charges with no token basis in their (date, account): NOT smeared onto
        # the endpoints that do have usage.
        "unknown": round(alloc.by_endpoint.get("unknown", 0.0), 6),
        "allocated": True,   # derived split, not a native CE dimension
        "basis": alloc.basis,
    }

    # Prior period: allocate using the PRIOR window's own inputs. Reusing the
    # current window's endpoint fraction made growth wrong (the audit measured a
    # displayed 8.1% where the prior window's own allocation gives 7.21%).
    prev_f = replace(f, start=prev_start, end=prev_end)
    prev_alloc = await cost_alloc.allocate(prev_f)

    return {
        "total_cost": round(alloc.selected_total, 6) if f.endpoint in ("runtime", "mantle") else total,
        "scope_total_all_endpoints": total,
        "currency": cur["currency"] or "USD",
        "unique_accounts": int(cur["unique_accounts"] or 0),
        "unique_services": int(cur["unique_services"] or 0),
        "previous_total_cost": round(prev_alloc.selected_total, 6)
            if f.endpoint in ("runtime", "mantle") else float(prev["total_cost"] or 0),
        "unattributed_cost": round(alloc.unattributed, 6),
        "has_unattributed": alloc.has_unattributed,
        "by_endpoint": by_endpoint,
        "allocation_basis": alloc.basis,
        "window": {"start": f.start.isoformat(), "end": f.end.isoformat(), "days": days},
        "previous_window": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
    }


# ---------------------------------------------------------------------------
@router.get("/cost-daily")
async def cost_daily(f: FilterSet = Depends(parse_filters)):
    """Daily total spend across the window. Single line/bar chart."""
    where_sql, params = _cost_where(f)
    rows = await db.fetch(
        f"""
        SELECT event_date, SUM(total_cost)::numeric AS total_cost,
               MIN(currency) AS currency
        FROM f_daily_cost
        WHERE {where_sql}
        GROUP BY event_date
        ORDER BY event_date
        """,
        *params,
    )
    # Endpoint slice comes from the SHARED per-(date, account) allocation, so
    # these daily values sum exactly to the headline (finding 07).
    out = []
    if f.endpoint in ("runtime", "mantle"):
        alloc = await cost_alloc.allocate(f)
        for r in rows:
            d = r["event_date"]
            out.append({
                "event_date": d.isoformat(),
                "total_cost": round(alloc.by_date.get(d, 0.0), 6),
                "currency": r["currency"] or "USD",
                "allocation_basis": alloc.basis,
            })
        return out
    for r in rows:
        out.append({
            "event_date": r["event_date"].isoformat(),
            "total_cost": float(r["total_cost"] or 0),
            "currency": r["currency"] or "USD",
        })
    return out


# ---------------------------------------------------------------------------
@router.get("/cost-by-account")
async def cost_by_account(f: FilterSet = Depends(parse_filters)):
    """Per-account spend totals over the window with WoW delta — sorted DESC.

    The previous-window numbers come from the same date math the
    /cost-summary endpoint uses: a window of equal length immediately
    preceding the current one. Used by the Cost tab's account table.
    """
    where_sql, params = _cost_where(f)
    days = (f.end - f.start).days + 1
    prev_end = f.start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    rows = await db.fetch(
        f"""
        SELECT accountId, SUM(total_cost)::numeric AS total_cost,
               MIN(currency) AS currency
        FROM f_daily_cost
        WHERE {where_sql}
        GROUP BY accountId
        """,
        *params,
    )
    cur_by_acct = {
        (r["accountid"] if "accountid" in r else r["accountId"]): {
            "total_cost": float(r["total_cost"] or 0),
            "currency": r["currency"] or "USD",
        } for r in rows
    }

    prev_where = "event_date BETWEEN $1::date AND $2::date"
    prev_params = [prev_start, prev_end]
    if f.accounts:
        prev_where += f" AND accountId = ANY($3::text[])"
        prev_params.append(list(f.accounts))
    prev_rows = await db.fetch(
        f"""
        SELECT accountId, SUM(total_cost)::numeric AS prev_cost
        FROM f_daily_cost
        WHERE {prev_where}
        GROUP BY accountId
        """,
        *prev_params,
    )
    prev_by_acct = {
        (r["accountid"] if "accountid" in r else r["accountId"]): float(r["prev_cost"] or 0)
        for r in prev_rows
    }

    # Endpoint slice from the SHARED per-(date, account) allocation. Each period
    # is allocated with ITS OWN inputs: the previous window used to be multiplied
    # by the CURRENT window's endpoint fraction, which made growth wrong (the
    # audit measured a displayed 8.1% against 7.21% computed properly).
    cur_alloc = prev_alloc = None
    if f.endpoint in ("runtime", "mantle"):
        cur_alloc = await cost_alloc.allocate(f)
        prev_alloc = await cost_alloc.allocate(
            replace(f, start=prev_start, end=prev_end))

    out = []
    for acct, cur in cur_by_acct.items():
        prev = prev_by_acct.get(acct, 0)
        tc, pc = cur["total_cost"], prev
        if f.endpoint in ("runtime", "mantle"):
            tc = cur_alloc.by_account.get(acct, 0.0)
            pc = prev_alloc.by_account.get(acct, 0.0)
        out.append({
            "accountId": acct,
            "total_cost": tc,
            "previous_cost": pc,
            "currency": cur["currency"],
        })
    out = [r for r in out if r["total_cost"] > 0 or f.endpoint == "all"]
    out.sort(key=lambda x: -x["total_cost"])
    return out


@router.get("/cost-by-model-detailed")
async def cost_by_model_detailed(f: FilterSet = Depends(parse_filters)):
    """Per-model spend joined to per-model token/request volumes.

    Returns enough columns to derive cost-per-1M-tokens and cost-per-request
    in the Cost tab without a second round-trip. Cost is allocated per-model
    by token-share when CE returns consolidated Bedrock spend (same logic
    as /cost-by-model)."""
    where_sql, params = _cost_where(f)

    # Per-model usage from f_daily. When an endpoint is selected, restrict usage
    # to that endpoint so per-model token weights (and thus the allocated spend)
    # reflect only that endpoint's activity — a runtime-only model then shows $0
    # under mantle, never a smeared fraction.
    fd_where = "event_date BETWEEN $1::date AND $2::date"
    fd_params = [f.start, f.end]
    if f.accounts:
        fd_where += f" AND accountId = ANY(${len(fd_params)+1}::text[])"
        fd_params.append(list(f.accounts))
    if f.endpoint in ("runtime", "mantle"):
        fd_where += f" AND endpoint = ${len(fd_params)+1}"
        fd_params.append(f.endpoint)

    usage_rows = await db.fetch(
        f"""
        SELECT modelId,
               SUM(total_input_tokens + total_output_tokens)::BIGINT AS total_tokens,
               SUM(total_input_tokens)::BIGINT  AS input_tokens,
               SUM(total_output_tokens)::BIGINT AS output_tokens,
               SUM(total_requests)::BIGINT      AS total_requests
        FROM f_daily
        WHERE {fd_where}
        GROUP BY modelId
        """,
        *fd_params,
    )
    usage = {
        (r["modelid"] if "modelid" in r else r["modelId"]): r
        for r in usage_rows
    }
    fleet_total_tokens = sum(int(r["total_tokens"] or 0) for r in usage_rows) or 1

    # Total spend in the window — we'll allocate this proportionally if CE
    # returns consolidated Bedrock spend.
    cost_total = await db.fetchval(
        f"SELECT COALESCE(SUM(total_cost), 0)::numeric FROM f_daily_cost WHERE {where_sql}",
        *params,
    )
    currency = await db.fetchval(
        f"SELECT MIN(currency) FROM f_daily_cost WHERE {where_sql}",
        *params,
    ) or "USD"

    # Endpoint slice from the SHARED per-(date, account) allocation, so this
    # endpoint's per-model amounts sum to the same headline the ribbon shows
    # (previously a whole-window weight, inconsistent with the daily chart).
    detailed_alloc = await cost_alloc.allocate(f)
    if f.endpoint in ("runtime", "mantle"):
        cost_total = detailed_alloc.selected_total

    # Try direct per-model service rows first.
    direct = await db.fetch(
        f"""
        SELECT service, SUM(total_cost)::numeric AS spend
        FROM f_daily_cost
        WHERE {where_sql} AND service ILIKE '%Bedrock Edition%'
        GROUP BY service
        """,
        *params,
    )
    direct_by_label = {r["service"]: float(r["spend"]) for r in direct}

    out = []
    for model_id, u in usage.items():
        toks = int(u["total_tokens"] or 0)
        reqs = int(u["total_requests"] or 0)
        # Cost: allocated proportionally by token share. (Direct per-model
        # rows are handled by /cost-by-model; this endpoint always uses
        # the proportional allocation so the table is consistent.)
        spend = float(cost_total or 0) * (toks / fleet_total_tokens) if fleet_total_tokens else 0
        out.append({
            "modelId":             model_id,
            "total_cost":          round(spend, 4),
            "currency":            currency,
            "total_tokens":        toks,
            "input_tokens":        int(u["input_tokens"] or 0),
            "output_tokens":       int(u["output_tokens"] or 0),
            "total_requests":      reqs,
            "cost_per_million_tokens": round(spend / (toks / 1_000_000), 4) if toks else None,
            "cost_per_request":    round(spend / reqs, 4) if reqs else None,
            "derived":             True,  # honest disclosure: spend allocated, not direct
        })
    out.sort(key=lambda x: -x["total_cost"])
    return out


@router.get("/cost-concentration")
async def cost_concentration(
    f: FilterSet = Depends(parse_filters),
    top_n: int = Query(10, ge=1, le=50),
):
    """Top (account, model) spend concentration with WoW delta.

    Spend is allocated per-(account, model) by joining f_daily_cost
    (per-account per-day cost) with f_daily (per-(account, model, day)
    token mix) and weighting by token share — same approach as the
    proportional path in /cost-by-model.
    """
    where_sql, params = _cost_where(f)
    days = (f.end - f.start).days + 1
    prev_end = f.start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    async def _per_acct_model(start_d, end_d):
        """Per-(account, model) spend from the SHARED allocation.

        This used to run its own copy of the allocation, without any endpoint
        filter — which is how a Mantle view showed $24,638.38 of Nova Lite under
        an $11,428.82 Mantle headline (a single row exceeding the whole total).
        Delegating to app/cost_alloc keeps every cost widget on one basis and one
        endpoint scope."""
        scoped = replace(f, start=start_d, end=end_d)
        a = await cost_alloc.allocate(scoped)
        return dict(a.by_account_model)

    cur_agg = await _per_acct_model(f.start, f.end)
    prev_agg = await _per_acct_model(prev_start, prev_end)

    out = []
    for (aid, mid), spend in cur_agg.items():
        prev = prev_agg.get((aid, mid), 0)
        out.append({
            "accountId":     aid,
            "modelId":       mid,
            "total_cost":    round(spend, 4),
            "previous_cost": round(prev, 4),
            "wow_delta":     round(spend - prev, 4),
            "wow_pct":       round((spend - prev) / prev * 100, 1) if prev else None,
        })
    out.sort(key=lambda x: -x["total_cost"])
    return out[:top_n]


# ---------------------------------------------------------------------------
@router.get("/cost-by-model")
async def cost_by_model(
    f: FilterSet = Depends(parse_filters),
    derive: bool = Query(
        True,
        description="When CE returns a consolidated 'Amazon Bedrock' service "
                    "(no per-model breakout), allocate the daily total to "
                    "modelIds in proportion to their token volume from f_daily. "
                    "Result rows are flagged `derived=true`.",
    ),
):
    """Per-model daily stacked-bar series.

    Two modes depending on what Cost Explorer returns:

      1. CE returns per-model SaaS services (e.g. "Claude Opus 4 (Amazon
         Bedrock Edition)") — we use those directly and `derived=false`.

      2. CE returns the consolidated "Amazon Bedrock" service only — we
         derive a per-model approximation from token volumes (`derived=true`).

    Most non-EDP customers see (2). The UI surfaces `derived` so the
    disclosure is honest.
    """
    # ONE allocation (app/cost_alloc.py). The old implementation returned early
    # whenever CE reported any "... (Amazon Bedrock Edition)" service, BEFORE the
    # endpoint slice was applied — so runtime, mantle and all returned the same
    # rows and the same total (the audit measured a $96,699.54 chart under an
    # $11,428.82 Mantle headline). It also suppressed consolidated charges and
    # dropped billed account-days with no token rows. The shared allocator scopes
    # by endpoint, attributes per-model services to their own family, and emits
    # an explicit unattributed bucket so money is conserved.
    alloc = await cost_alloc.allocate(f)
    out = [
        {
            "event_date": d.isoformat(),
            "model_label": mid,
            "total_cost": round(amt, 6),
            "currency": alloc.currency,
            "derived": alloc.derived,
            "allocation_basis": alloc.basis,
        }
        for (d, mid), amt in alloc.by_date_model.items()
        if amt != 0
    ]
    # Unattributable charges are shown, not hidden: sum(models) + unattributed
    # equals the same-scope CE total for the selected endpoint.
    if alloc.unattributed:
        out.append({
            "event_date": f.end.isoformat(),
            "model_label": cost_alloc.UNATTRIBUTED_LABEL,
            "total_cost": round(alloc.unattributed, 6),
            "currency": alloc.currency,
            "derived": True,
            "unattributed": True,
            "allocation_basis": alloc.basis,
        })
    return sorted(out, key=lambda x: (x["event_date"], -x["total_cost"]))
