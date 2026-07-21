"""Per-dimension proxy usage (the proxy telemetry pattern).

A GenAI proxy fronting Bedrock signs every request with one IAM role, so caller
identity can't attribute usage. Instead the proxy emits one metadata-only event
per request into S3 carrying an arbitrary `dimensions` map (workload / env /
business_unit / cost_center / …). ingestion/proxy_events.py fans each request
out to one row per (dim_key, dim_value) in f_proxy_dim_hourly — the same
discipline as f_daily_tagged, so summing a single dim_key is correct.

These endpoints power the "by dimension" views: tokens, throttle rate, error
rate, latency, request volume, AND per-value quota utilization — endpoint-
agnostic (runtime + mantle) since the proxy reports the same shape for both.
`workload` is just the conventional default dimension key.

Endpoints:
  GET /api/workload-usage/available   — {available, has_data, enabled}
  PUT /api/workload-usage/enabled     — admin toggle to surface the tab
  GET /api/workload-usage/dimensions  — distinct dim keys (+ values) for pickers
  GET /api/workload-usage             — per-value aggregates for one dim_key
  GET /api/workload-usage/by-model    — one dim value drilled down by model
  GET /api/workload-usage/quota       — per-value TPM quota utilization estimate
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .. import db
from ..auth import is_admin
from ..burndown import output_burndown_rate

router = APIRouter()

# ingestion_meta key holding the admin "show the Workloads tab" toggle. Stored
# as the string 'true'/'false'. Absent → default off (tab only appears once
# proxy data lands). Reuses the generic KV table so no migration is needed.
_WORKLOADS_ENABLED_KEY = "workloads_tab_enabled"

# The conventional default dimension key when the UI doesn't specify one.
_DEFAULT_DIM = "workload"


async def _workloads_enabled() -> bool:
    try:
        row = await db.fetchrow(
            "SELECT value FROM ingestion_meta WHERE key = $1", _WORKLOADS_ENABLED_KEY
        )
        return bool(row) and str(row["value"]).lower() == "true"
    except Exception:
        return False


@router.get("/workload-usage/available")
async def workload_usage_available():
    """Drives whether the UI surfaces the Workloads view.

    has_data  — proxy per-dimension rows actually exist (real telemetry).
    enabled   — an admin switched the tab on in Settings.
    available — show the tab if EITHER is true.
    """
    try:
        row = await db.fetch("SELECT EXISTS(SELECT 1 FROM f_proxy_dim_hourly) AS ok")
        has_data = bool(row and row[0]["ok"])
    except Exception:
        has_data = False
    enabled = await _workloads_enabled()
    return {"available": has_data or enabled, "has_data": has_data, "enabled": enabled}


@router.put("/workload-usage/enabled")
async def set_workloads_enabled(request: Request, body: dict):
    """Admin-only: toggle whether the Workloads tab is surfaced before any
    proxy data exists. Persisted stack-wide in ingestion_meta."""
    if not is_admin(request):
        raise HTTPException(403, detail="admin access required")
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, detail="enabled must be a boolean")
    await db.fetchval(
        """
        INSERT INTO ingestion_meta (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        RETURNING key
        """,
        _WORKLOADS_ENABLED_KEY, "true" if enabled else "false",
    )
    return {"ok": True, "enabled": enabled}


@router.get("/workload-usage/dimensions")
async def workload_usage_dimensions():
    """Distinct dimension keys the proxy has emitted, each with its values
    (ordered by volume). Powers the top-bar dimension:value picker.

    Returns:
      {
        "default_key": "workload",
        "dimensions": [
          {"key": "workload", "values": [{"value": "search", "total_requests": N, "endpoints": [...]}, ...]},
          {"key": "env", "values": [...]},
          ...
        ]
      }
    """
    rows = await db.fetch(
        """
        SELECT dim_key, dim_value, total_requests_30d, endpoints
        FROM dim_proxy_dimensions
        ORDER BY dim_key, total_requests_30d DESC
        """
    )
    by_key: dict[str, list] = {}
    for r in rows:
        by_key.setdefault(r["dim_key"], []).append({
            "value": r["dim_value"],
            "total_requests": int(r["total_requests_30d"] or 0),
            "endpoints": list(r["endpoints"] or []),
        })
    # Order keys: the default key first, then by total volume.
    key_volume = {k: sum(v["total_requests"] for v in vs) for k, vs in by_key.items()}
    ordered_keys = sorted(by_key.keys(),
                          key=lambda k: (k != _DEFAULT_DIM, -key_volume[k]))
    default_key = _DEFAULT_DIM if _DEFAULT_DIM in by_key else (ordered_keys[0] if ordered_keys else _DEFAULT_DIM)
    return {
        "default_key": default_key,
        "dimensions": [{"key": k, "values": by_key[k]} for k in ordered_keys],
    }


@router.get("/workload-usage/values")
async def workload_usage_values(dim_key: str = Query(..., min_length=1)):
    """Distinct values for one dimension key, ordered by volume — powers the
    per-attribute value multiselect (top-bar filter + in-tab filter). Mirrors
    /api/tags/{key}/values for the invocation-log tag path."""
    rows = await db.fetch(
        """
        SELECT dim_value, total_requests_30d, endpoints
        FROM dim_proxy_dimensions
        WHERE dim_key = $1
        ORDER BY total_requests_30d DESC
        """,
        dim_key,
    )
    return [
        {"value": r["dim_value"],
         "total_requests_30d": int(r["total_requests_30d"] or 0),
         "endpoints": list(r["endpoints"] or [])}
        for r in rows
    ]


def _resolve_endpoint(endpoint: str) -> str:
    return endpoint if endpoint in ("runtime", "mantle", "all") else "all"


@router.get("/workload-usage")
async def workload_usage(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all", description="runtime / mantle / all"),
    dim_key: str = Query(_DEFAULT_DIM, description="dimension to group by"),
    dim_value: list[str] | None = Query(None, description="filter to these values (repeatable); omit for all"),
):
    """Per-value aggregates for ONE dimension key over the window: requests,
    tokens, throttle rate, error rate, worst-hour latency percentiles.

    Pins a single dim_key so sums are correct (never cross keys). Optional
    dim_value (repeatable) filters to a subset of values.
    """
    endpoint = _resolve_endpoint(endpoint)
    where = ["event_date >= current_date - $1::int", "dim_key = $2"]
    params: list = [days, dim_key]
    if endpoint != "all":
        params.append(endpoint)
        where.append(f"endpoint = ${len(params)}")
    vals = [v for v in (dim_value or []) if v and v != "all"]
    if vals:
        params.append(vals)
        where.append(f"dim_value = ANY(${len(params)}::text[])")
    w = " AND ".join(where)

    rows = await db.fetch(
        f"""
        SELECT
          dim_value AS workload,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(input_tokens)::BIGINT     AS input_tokens,
          SUM(output_tokens)::BIGINT    AS output_tokens,
          SUM(cache_read_tokens)::BIGINT AS cache_read_tokens,
          SUM(throttled_count)::BIGINT  AS throttled,
          SUM(error_count)::BIGINT      AS errors,
          ROUND(100.0 * SUM(throttled_count) / NULLIF(SUM(total_requests),0), 3) AS throttle_pct,
          ROUND(100.0 * SUM(error_count)     / NULLIF(SUM(total_requests),0), 3) AS error_pct,
          MAX(p50_latency_ms) AS p50_latency_ms,
          MAX(p90_latency_ms) AS p90_latency_ms,
          MAX(p99_latency_ms) AS p99_latency_ms,
          array_agg(DISTINCT endpoint) AS endpoints
        FROM f_proxy_dim_hourly
        WHERE {w}
        GROUP BY dim_value
        HAVING SUM(total_requests) > 0
        ORDER BY total_requests DESC
        LIMIT 500
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


@router.get("/workload-usage/by-model")
async def workload_usage_by_model(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: str = Query(..., min_length=1),
):
    """Drill one dimension value down by model."""
    endpoint = _resolve_endpoint(endpoint)
    params: list = [days, dim_key, dim_value]
    ep_clause = ""
    if endpoint != "all":
        params.append(endpoint)
        ep_clause = f" AND endpoint = ${len(params)}"
    rows = await db.fetch(
        f"""
        SELECT modelId, endpoint,
          SUM(total_requests)::BIGINT AS total_requests,
          SUM(input_tokens)::BIGINT   AS input_tokens,
          SUM(output_tokens)::BIGINT  AS output_tokens,
          SUM(throttled_count)::BIGINT AS throttled
        FROM f_proxy_dim_hourly
        WHERE event_date >= current_date - $1::int
          AND dim_key = $2 AND dim_value = $3{ep_clause}
        GROUP BY modelId, endpoint
        ORDER BY total_requests DESC
        LIMIT 200
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


@router.get("/workload-usage/quota")
async def workload_usage_quota(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
):
    """Per-value TPM quota-utilization ESTIMATE for one dimension key.

    Answers a common customer ask: "quota utilization by workload." Quota limits are
    set per (account, model, region) — never per workload — so we attribute a
    share of that ceiling to each dimension value:

      1. For each (dim_value, model) find the PEAK hour of quota-tokens, where
         quota_tokens = input_tokens + output_tokens * burndown_rate(model)
         (cache-read excluded per the AWS burndown doc; the proxy doesn't report
         cache-write, so this is a proxy-derived estimate that can slightly
         UNDER-count vs CloudWatch's EstimatedTPMQuotaUsage).
      2. Convert peak-hour quota-tokens → per-minute TPM (÷60).
      3. Divide by the applicable TPM limit from f_quotas (applied_value if set,
         else default_value) for that model, picking the matching traffic tier.
      4. utilization% = peak_tpm / limit * 100, taking the worst model per value.

    Burndown applies only to bedrock-runtime; mantle rate is forced to 1.
    Honestly labeled `is_estimate: true` — it's a proxy-derived approximation.
    """
    endpoint = _resolve_endpoint(endpoint)
    params: list = [days, dim_key]
    ep_clause = ""
    if endpoint != "all":
        params.append(endpoint)
        ep_clause = f" AND endpoint = ${len(params)}"

    # Per (dim_value, model, endpoint, region, hour) sum tokens, so we can apply
    # the per-model burndown rate before taking the peak hour.
    rows = await db.fetch(
        f"""
        SELECT dim_value, modelId, endpoint, region, event_date, hour,
          SUM(input_tokens)::BIGINT  AS input_tokens,
          SUM(output_tokens)::BIGINT AS output_tokens
        FROM f_proxy_dim_hourly
        WHERE event_date >= current_date - $1::int AND dim_key = $2{ep_clause}
        GROUP BY dim_value, modelId, endpoint, region, event_date, hour
        """,
        *params,
    )

    # Load TPM limits keyed by (model_name, region). We match a model id to a
    # quota row by substring on model_name (quota model_name is a friendly name
    # like 'Claude Sonnet 4.5'); fall back to the min TPM limit for the region.
    quota_rows = await db.fetch(
        "SELECT region, model_name, traffic_type, metric, applied_value, default_value "
        "FROM f_quotas WHERE metric = 'TPM'"
    )
    # region -> list of (model_name_lower, limit)
    region_quotas: dict[str, list[tuple[str, float]]] = {}
    for q in quota_rows:
        lim = q["applied_value"] or q["default_value"]
        if not lim:
            continue
        region_quotas.setdefault(q["region"], []).append(
            (str(q["model_name"]).lower(), float(lim)))

    def _limit_for(model_id: str, region: str) -> float | None:
        cands = region_quotas.get(region) or []
        if not cands:
            # region-agnostic fallback: any TPM limit for a matching model
            cands = [c for lst in region_quotas.values() for c in lst]
        if not cands:
            return None
        mid = model_id.lower()
        # Prefer a quota whose friendly name shares a token with the model id.
        best = None
        for name, lim in cands:
            toks = [t for t in name.replace("-", " ").split() if len(t) > 2]
            if any(t in mid for t in toks):
                best = lim if best is None else min(best, lim)
        if best is not None:
            return best
        # Fallback: smallest TPM limit in scope (most conservative → highest util).
        return min(lim for _, lim in cands)

    # peak quota-TPM per (dim_value, model)
    from collections import defaultdict
    peak: dict[tuple, float] = defaultdict(float)
    model_ep: dict[tuple, str] = {}
    model_region: dict[tuple, str] = {}
    for r in rows:
        is_mantle = (r["endpoint"] == "mantle")
        mid = r.get("modelid") or r.get("modelId")
        rate = 1 if is_mantle else output_burndown_rate(mid, is_mantle=is_mantle)
        qtok = int(r["input_tokens"] or 0) + int(r["output_tokens"] or 0) * rate
        tpm = qtok / 60.0
        k = (r["dim_value"], mid)
        if tpm > peak[k]:
            peak[k] = tpm
            model_ep[k] = r["endpoint"]
            model_region[k] = r["region"]

    # Roll up to worst model per dim_value.
    result: dict[str, dict] = {}
    for (dim_value, mid), tpm in peak.items():
        region = model_region[(dim_value, mid)]
        limit = _limit_for(mid, region)
        util = (tpm / limit * 100.0) if limit else None
        cur = result.get(dim_value)
        cand = {
            "workload": dim_value,
            "peak_tpm": round(tpm, 1),
            "model": mid,
            "region": region,
            "tpm_limit": round(limit, 1) if limit else None,
            "utilization_pct": round(util, 2) if util is not None else None,
        }
        if cur is None or (util is not None and (cur["utilization_pct"] or -1) < util):
            result[dim_value] = cand

    out = sorted(result.values(),
                 key=lambda d: (d["utilization_pct"] or 0), reverse=True)
    return {"is_estimate": True, "rows": out}
