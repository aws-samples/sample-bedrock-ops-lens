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

from datetime import timedelta

from fastapi import APIRouter, Depends, Query

from .. import db
from ..filters import FilterSet, parse_filters
from .model_insights import BEDROCK_PRICING, _provider_of

router = APIRouter()


async def _endpoint_fraction(f: FilterSet, key_cols: list[str]) -> dict:
    """Return, per grouping key, the fraction of that key's token-cost weight
    attributable to EACH endpoint — used to slice CE spend by endpoint
    accurately at the row level (not a global smear).

    key_cols: f_daily columns to group by (e.g. ['event_date'], ['accountId'],
    ['modelId']). Returns {key_tuple: {'runtime': frac, 'mantle': frac}} where
    fracs sum to 1 (or {} for a key with no usage → caller keeps full amount).

    Weight = input×in_price + output×out_price, provider-priced (same basis as
    the model cost allocation), so it tracks real dollar mix, not raw tokens."""
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    cols = ", ".join(key_cols)
    rows = await db.fetch(
        f"""
        SELECT {cols}, endpoint, modelId,
               SUM(total_input_tokens)::BIGINT  AS in_tok,
               SUM(total_output_tokens)::BIGINT AS out_tok
        FROM f_daily
        WHERE {" AND ".join(parts)}
        GROUP BY {cols}, endpoint, modelId
        """,
        *params,
    )
    # Accumulate weight per (key, endpoint).
    from collections import defaultdict
    w = defaultdict(lambda: {"runtime": 0.0, "mantle": 0.0})
    for r in rows:
        rd = dict(r)
        key = tuple(rd[c.lower()] if c.lower() in rd else rd[c] for c in key_cols)
        ep = rd.get("endpoint") if rd.get("endpoint") in ("runtime", "mantle") else "runtime"
        price = BEDROCK_PRICING.get(_provider_of(rd.get("modelid") or rd.get("modelId")),
                                    {"input": 0.50, "output": 1.50})
        w[key][ep] += (int(rd.get("in_tok") or 0) / 1_000_000) * price["input"] \
                    + (int(rd.get("out_tok") or 0) / 1_000_000) * price["output"]
    out = {}
    for key, ew in w.items():
        tot = ew["runtime"] + ew["mantle"]
        if tot > 0:
            out[key] = {"runtime": ew["runtime"] / tot, "mantle": ew["mantle"] / tot}
    return out


async def _endpoint_cost_weights(f: FilterSet) -> dict:
    """Per-endpoint token-cost WEIGHT from f_daily (input×in_price +
    output×out_price, provider-priced). Cost Explorer gives an invoice-accurate
    TOTAL but no runtime-vs-mantle dimension; we allocate that real total across
    endpoints by each endpoint's share of this weight. Returns
    {'runtime': w, 'mantle': w} (0 when no usage)."""
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    rows = await db.fetch(
        f"""
        SELECT endpoint, modelId,
               SUM(total_input_tokens)::BIGINT  AS in_tok,
               SUM(total_output_tokens)::BIGINT AS out_tok
        FROM f_daily
        WHERE {" AND ".join(parts)}
        GROUP BY endpoint, modelId
        """,
        *params,
    )
    weights = {"runtime": 0.0, "mantle": 0.0}
    for r in rows:
        ep = r["endpoint"] if r["endpoint"] in ("runtime", "mantle") else "runtime"
        price = BEDROCK_PRICING.get(_provider_of(r["modelid"] or r["modelId"]),
                                    {"input": 0.50, "output": 1.50})
        w = (int(r["in_tok"] or 0) / 1_000_000) * price["input"] \
          + (int(r["out_tok"] or 0) / 1_000_000) * price["output"]
        weights[ep] += w
    return weights


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

    # Allocate the invoice-accurate CE total across endpoints by each
    # endpoint's token-cost weight (CE itself has no runtime/mantle dimension).
    weights = await _endpoint_cost_weights(f)
    wsum = weights["runtime"] + weights["mantle"]
    if wsum > 0:
        by_endpoint = {
            "runtime": round(total * weights["runtime"] / wsum, 6),
            "mantle":  round(total * weights["mantle"]  / wsum, 6),
            "allocated": True,   # derived split, not a native CE dimension
        }
    else:
        by_endpoint = {"runtime": total, "mantle": 0.0, "allocated": True}

    return {
        "total_cost": total,
        "currency": cur["currency"] or "USD",
        "unique_accounts": int(cur["unique_accounts"] or 0),
        "unique_services": int(cur["unique_services"] or 0),
        "previous_total_cost": float(prev["total_cost"] or 0),
        "by_endpoint": by_endpoint,
        "window": {"start": f.start.isoformat(), "end": f.end.isoformat(), "days": days},
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
    # Endpoint slice: scale each DAY's spend by that day's endpoint token-cost
    # fraction (accurate per-day, not a global smear). 'all' → untouched CE $.
    frac = {}
    if f.endpoint in ("runtime", "mantle"):
        frac = await _endpoint_fraction(f, ["event_date"])
    out = []
    for r in rows:
        amt = float(r["total_cost"] or 0)
        if f.endpoint in ("runtime", "mantle"):
            fr = frac.get((r["event_date"],))
            amt = amt * fr[f.endpoint] if fr else 0.0
        out.append({
            "event_date": r["event_date"].isoformat(),
            "total_cost": amt,
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

    # Endpoint slice: scale each account's spend (current + previous) by its
    # own endpoint token-cost fraction. 'all' → untouched.
    frac = {}
    if f.endpoint in ("runtime", "mantle"):
        frac = await _endpoint_fraction(f, ["accountId"])

    out = []
    for acct, cur in cur_by_acct.items():
        prev = prev_by_acct.get(acct, 0)
        tc, pc = cur["total_cost"], prev
        if f.endpoint in ("runtime", "mantle"):
            fr = frac.get((acct,))
            mult = fr[f.endpoint] if fr else 0.0
            tc, pc = tc * mult, pc * mult
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

    # Endpoint slice: scale the CE total to this endpoint's allocated share, so
    # the per-model amounts below sum to the endpoint total (not fleet total).
    if f.endpoint in ("runtime", "mantle"):
        weights = await _endpoint_cost_weights(f)
        wsum = weights["runtime"] + weights["mantle"]
        cost_total = float(cost_total or 0) * (weights[f.endpoint] / wsum if wsum else 0.0)

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
        # Cost per (date, accountId).
        cw = "event_date BETWEEN $1::date AND $2::date"
        cp = [start_d, end_d]
        if f.accounts:
            cw += f" AND accountId = ANY($3::text[])"
            cp.append(list(f.accounts))
        cost_rows = await db.fetch(
            f"""
            SELECT event_date, accountId,
                   SUM(total_cost)::numeric AS daily_cost,
                   MIN(currency) AS currency
            FROM f_daily_cost
            WHERE {cw}
            GROUP BY event_date, accountId
            """,
            *cp,
        )
        cost_by_ad = {}
        for r in cost_rows:
            aid = r["accountid"] if "accountid" in r else r["accountId"]
            cost_by_ad[(r["event_date"], aid)] = (
                float(r["daily_cost"] or 0),
                r["currency"] or "USD",
            )
        # Token mix per (date, account, model).
        tok_rows = await db.fetch(
            f"""
            SELECT event_date, accountId, modelId,
                   (SUM(total_input_tokens) + SUM(total_output_tokens))::BIGINT AS toks
            FROM f_daily
            WHERE {cw}
            GROUP BY event_date, accountId, modelId
            """,
            *cp,
        )
        totals_ad = {}
        for r in tok_rows:
            aid = r["accountid"] if "accountid" in r else r["accountId"]
            totals_ad[(r["event_date"], aid)] = totals_ad.get((r["event_date"], aid), 0) + int(r["toks"] or 0)
        agg = {}
        for r in tok_rows:
            aid = r["accountid"] if "accountid" in r else r["accountId"]
            mid = r["modelid"] if "modelid" in r else r["modelId"]
            d = r["event_date"]
            cost_pair = cost_by_ad.get((d, aid))
            tot = totals_ad.get((d, aid), 0)
            if not cost_pair or not tot:
                continue
            allocated = cost_pair[0] * (int(r["toks"] or 0) / tot)
            key = (aid, mid)
            agg[key] = agg.get(key, 0.0) + allocated
        return agg

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
    where_sql, params = _cost_where(f)

    # Direct: per-service rows. Filter to "Bedrock Edition" services if any.
    direct_rows = await db.fetch(
        f"""
        SELECT event_date, service,
               SUM(total_cost)::numeric AS total_cost,
               MIN(currency) AS currency
        FROM f_daily_cost
        WHERE {where_sql} AND service ILIKE '%Bedrock Edition%'
        GROUP BY event_date, service
        ORDER BY event_date, service
        """,
        *params,
    )
    if direct_rows:
        return [
            {
                "event_date": r["event_date"].isoformat(),
                "model_label": r["service"],
                "total_cost": float(r["total_cost"]),
                "currency":   r["currency"] or "USD",
                "derived": False,
            }
            for r in direct_rows
        ]

    # Fallback: consolidated "Amazon Bedrock" service. Allocate per-model by
    # f_daily token mix. Two CTEs: daily total cost from CE, and per-model
    # daily token share from f_daily; we multiply at the row level.
    if not derive:
        return []

    # Build the WHERE for the f_daily side using the same date+accounts.
    # Endpoint slice: filtering the TOKEN side to one endpoint makes the
    # per-(date,account) normalization total that endpoint's tokens, so each
    # model's allocated cost reflects only its usage on the selected endpoint
    # (models not used there fall out). CE $ (the numerator) is unchanged; this
    # apportions the same real dollars to the endpoint's actual model mix.
    fd_where = "event_date BETWEEN $1::date AND $2::date"
    fd_params = [f.start, f.end]
    if f.accounts:
        fd_where += f" AND accountId = ANY(${len(fd_params)+1}::text[])"
        fd_params.append(list(f.accounts))
    if f.endpoint in ("runtime", "mantle"):
        fd_where += f" AND endpoint = ${len(fd_params)+1}"
        fd_params.append(f.endpoint)

    # Cost Explorer cost is per (event_date, accountId). Token mix is per
    # (event_date, accountId, modelId). Allocate cost ∝ tokens within the
    # same (event_date, accountId), then sum to (event_date, modelId) for
    # the chart series.
    rows = await db.fetch(
        f"""
        WITH cost AS (
            SELECT event_date, accountId,
                   SUM(total_cost)::numeric AS daily_cost,
                   MIN(currency) AS currency
            FROM f_daily_cost
            WHERE {where_sql}
            GROUP BY event_date, accountId
        ),
        tokens AS (
            SELECT event_date, accountId, modelId,
                   SUM(total_input_tokens + total_output_tokens)::numeric AS toks
            FROM f_daily
            WHERE {fd_where}
            GROUP BY event_date, accountId, modelId
        ),
        totals AS (
            SELECT event_date, accountId, SUM(toks)::numeric AS total_toks
            FROM tokens GROUP BY event_date, accountId
        )
        SELECT t.event_date, t.modelId AS model_label,
               SUM(c.daily_cost * t.toks / NULLIF(tt.total_toks, 0))::numeric AS total_cost,
               MIN(c.currency) AS currency
        FROM tokens t
        JOIN totals tt ON tt.event_date = t.event_date AND tt.accountId = t.accountId
        JOIN cost c    ON c.event_date  = t.event_date AND c.accountId  = t.accountId
        GROUP BY t.event_date, t.modelId
        ORDER BY t.event_date, total_cost DESC
        """,
        *params, *(fd_params if f.accounts else fd_params[:2]),  # dedupe accounts param
    ) if False else await db.fetch(
        # Two queries are cleaner than parameter-juggling — fetch tokens and
        # cost separately and stitch in Python.
        f"""
        SELECT event_date, accountId,
               SUM(total_cost)::numeric AS daily_cost,
               MIN(currency) AS currency
        FROM f_daily_cost
        WHERE {where_sql}
        GROUP BY event_date, accountId
        """,
        *params,
    )

    cost_by_acct_day: dict[tuple, tuple[float, str]] = {}
    for r in rows:
        aid = r["accountid"] if "accountid" in r else r["accountId"]
        cost_by_acct_day[(r["event_date"], aid)] = (
            float(r["daily_cost"] or 0),
            r["currency"] or "USD",
        )

    tok_rows = await db.fetch(
        f"""
        SELECT event_date, accountId, modelId,
               (SUM(total_input_tokens) + SUM(total_output_tokens))::BIGINT AS toks
        FROM f_daily
        WHERE {fd_where}
        GROUP BY event_date, accountId, modelId
        """,
        *fd_params,
    )

    # Sum per (date, account) for normalization.
    totals: dict[tuple, int] = {}
    for r in tok_rows:
        aid = r["accountid"] if "accountid" in r else r["accountId"]
        key = (r["event_date"], aid)
        totals[key] = totals.get(key, 0) + int(r["toks"] or 0)

    out_keyed: dict[tuple[str, str], dict] = {}
    for r in tok_rows:
        aid = r["accountid"] if "accountid" in r else r["accountId"]
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        d = r["event_date"]
        cost_pair = cost_by_acct_day.get((d, aid))
        total_toks = totals.get((d, aid), 0)
        if not cost_pair or total_toks == 0:
            continue
        cost_amt, cur = cost_pair
        share = (int(r["toks"] or 0)) / total_toks
        allocated = cost_amt * share
        k = (d.isoformat(), mid)
        if k not in out_keyed:
            out_keyed[k] = {
                "event_date":  d.isoformat(),
                "model_label": mid,
                "total_cost":  0.0,
                "currency":    cur,
                "derived":     True,
            }
        out_keyed[k]["total_cost"] += allocated

    return sorted(out_keyed.values(), key=lambda x: (x["event_date"], -x["total_cost"]))
