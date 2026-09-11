"""Per-dimension proxy usage (the proxy telemetry pattern).

A GenAI proxy fronting Bedrock signs every request with one IAM role, so caller
identity can't attribute usage. Instead the proxy emits one metadata-only event
per request into S3 carrying an arbitrary `dimensions` map (workload / env /
business_unit / cost_center / …). ingestion/proxy_events.py fans each request
out to one row per (dim_key, dim_value) in f_proxy_dim_hourly - the same
discipline as f_daily_tagged. Summing ONE dim_key therefore avoids
multiply-counting, but it is NOT a fleet total: the maps are sparse, so a request
that carries only `team` contributes nothing to `workload`. For a total, read the
'__all__' accounting row, which the ingester writes once per request and which is
excluded from the attribute pickers (audit T09).

These endpoints power the "by dimension" views: tokens, throttle rate, error
rate, latency, request volume, AND per-value quota utilization. Quota
utilization covers AWS-billed endpoints only (runtime + mantle); direct-provider
calls consume no AWS quota and are reported separately (audit T08).
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

from .. import db, proxy_quota
from ..auth import is_admin
from ..burndown import output_burndown_rate

router = APIRouter()

# ingestion_meta key holding the admin "show the Workloads tab" toggle. Stored
# as the string 'true'/'false'. Absent → default off (tab only appears once
# proxy data lands). Reuses the generic KV table so no migration is needed.
_WORKLOADS_ENABLED_KEY = "workloads_tab_enabled"

# The conventional default dimension key when the UI doesn't specify one.
# Endpoints AWS bills and enforces quotas for. anthropic-api / openai-api go
# straight to the provider, so no AWS quota or Cost Explorer line applies
# to them (audit T07/T08).
AWS_BILLED_ENDPOINTS = ("runtime", "mantle")

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
    # runtime/mantle are Bedrock paths; anthropic-api/openai-api are direct-API
    # paths visible only in client telemetry (006).
    return endpoint if endpoint in (
        "runtime", "mantle", "anthropic-api", "openai-api", "all") else "all"


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
          -- Audit T10: percentiles are not mergeable, so MAX() over hourly
          -- buckets is the WORST BUCKET's percentile - an upper bound on the
          -- population's. Declared so no caller can read it as a true
          -- quantile. /attribution/xtab/latency-by-model computes real
          -- percentiles from f_request_events when the window is inside raw
          -- retention.
          MAX(p50_latency_ms) AS p50_latency_ms,
          MAX(p90_latency_ms) AS p90_latency_ms,
          MAX(p99_latency_ms) AS p99_latency_ms,
          MAX(p50_ttft_ms)    AS p50_ttft_ms,
          MAX(p90_ttft_ms)    AS p90_ttft_ms,
          'worst_bucket_upper_bound' AS percentile_basis,
          SUM(retried_count)::BIGINT AS retried,
          SUM(cost_usd_est)::DOUBLE PRECISION AS cost_usd_est,
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


# Provider = model family (whose model answered), rolled up ACROSS paths —
# "all my Anthropic usage (Bedrock + direct API) vs all my OpenAI usage".
# Distinct from `endpoint`, which is the PATH the request traveled.
_PROVIDER_SQL = """
    CASE
      WHEN modelId ILIKE '%anthropic%' OR modelId ILIKE 'claude%' THEN 'anthropic'
      WHEN modelId ILIKE '%openai%' OR modelId ILIKE 'gpt-%'
           OR modelId ILIKE 'o1%' OR modelId ILIKE 'o3%' OR modelId ILIKE 'o4%' THEN 'openai'
      WHEN modelId ILIKE 'amazon.%' OR modelId ILIKE '%titan%' OR modelId ILIKE '%nova%' THEN 'amazon'
      WHEN modelId ILIKE 'meta.%' OR modelId ILIKE '%llama%' THEN 'meta'
      WHEN modelId ILIKE 'mistral%' THEN 'mistral'
      WHEN modelId ILIKE 'cohere%' THEN 'cohere'
      ELSE 'other'
    END
"""


@router.get("/workload-usage/by-provider")
async def workload_usage_by_provider(days: int = Query(14, ge=1, le=90)):
    """Client-telemetry usage rolled up by PROVIDER (model family) × PATH
    (endpoint). Answers "Anthropic everywhere vs OpenAI everywhere", including
    direct-API traffic no AWS-side source can see. Reads the '__all__' accounting
    row (one row per request). The previous claim that "any key covers 100% of
    requests" was wrong: client-reported attributes are sparse, so pinning to the
    busiest attribute key dropped every request that did not carry it (audit
    T09)."""
    rows = await db.fetch(
        f"""
        WITH pinned AS (
          -- Audit T09: prefer the '__all__' accounting row, which carries one
          -- row per request regardless of which attributes the caller sent.
          -- Picking the highest-volume ATTRIBUTE key instead lost every request
          -- (and any provider) whose traffic never carries that key. Falls back
          -- to the widest real key for data ingested before '__all__' existed.
          SELECT dim_key FROM f_proxy_dim_hourly
          WHERE event_date >= current_date - $1::int
          GROUP BY dim_key
          ORDER BY (dim_key = '__all__') DESC, SUM(total_requests) DESC
          LIMIT 1
        )
        SELECT
          {_PROVIDER_SQL} AS provider,
          endpoint,
          SUM(total_requests)::BIGINT    AS total_requests,
          SUM(input_tokens)::BIGINT      AS input_tokens,
          SUM(output_tokens)::BIGINT     AS output_tokens,
          SUM(cache_read_tokens)::BIGINT AS cache_read_tokens,
          SUM(error_count)::BIGINT       AS errors,
          SUM(retried_count)::BIGINT     AS retried,
          SUM(cost_usd_est)::DOUBLE PRECISION AS cost_usd_est,
          -- Audit T10: percentiles are not mergeable, so MAX() over hourly
          -- buckets is the WORST BUCKET's percentile - an upper bound on the
          -- population's. Declared so no caller can read it as a true
          -- quantile. /attribution/xtab/latency-by-model computes real
          -- percentiles from f_request_events when the window is inside raw
          -- retention.
          MAX(p90_latency_ms) AS p90_latency_ms,
          MAX(p90_ttft_ms)    AS p90_ttft_ms,
          'worst_bucket_upper_bound' AS percentile_basis,
          COUNT(DISTINCT modelId)::BIGINT AS distinct_models
        FROM f_proxy_dim_hourly
        WHERE event_date >= current_date - $1::int
          AND dim_key = (SELECT dim_key FROM pinned)
        GROUP BY 1, endpoint
        ORDER BY total_requests DESC
        """,
        days,
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
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
):
    """Per-value TPM quota-utilization ESTIMATE for one dimension key.

    Answers the common enterprise ask: "quota utilization by workload." Quota
    limits are set per (account, model, region) — never per workload — so a share
    of that ceiling is attributed to each dimension value:

      1. For each (dim_value, model) find the PEAK hour of quota-tokens, where
         quota_tokens = input_tokens + output_tokens * burndown_rate(model)
         (cache-read excluded per the AWS burndown doc; the proxy doesn't report
         cache-write, so this is a proxy-derived estimate that can slightly
         UNDER-count vs CloudWatch's EstimatedTPMQuotaUsage).
      2. Convert peak-hour quota-tokens → per-minute TPM.
      3. Divide by the limit resolved for that (account, region, model, family).
      4. utilization% = peak_tpm / limit * 100, taking the worst model per value.

    Burndown applies only to bedrock-runtime; mantle rate is forced to 1.
    Honestly labeled `is_estimate: true` — it's a proxy-derived approximation.

    The resolution itself lives in app/proxy_quota.py, shared with
    /attribution/quota. Previously each endpoint had its own copy and they
    disagreed: both matched a quota row on ANY token of its name (so a Sonnet
    model could inherit a Haiku ceiling and read 200% instead of 10%) and both
    fell back to the region's smallest limit when nothing matched. There is now
    one resolver, it requires every token of the quota name to match, it keys on
    the account, and an unmatched model reports an UNKNOWN limit rather than
    borrowing another model's.
    """
    rows = db.rows_to_dicts(await db.fetch(
        f"""
        SELECT dim_value, modelId, endpoint, region, accountId,
          SUM(input_tokens)::BIGINT  AS input_tokens,
          SUM(output_tokens)::BIGINT AS output_tokens
        FROM f_proxy_dim_hourly
        WHERE {" AND ".join(_quota_scope_parts(days, dim_key, endpoint, accounts, region)[0])}
        GROUP BY dim_value, modelId, endpoint, region, accountId, event_date, hour
        """, *_quota_scope_parts(days, dim_key, endpoint, accounts, region)[1]))
    return await proxy_quota.score(rows, group_key="dim_value")


def _quota_scope_parts(days, dim_key, endpoint, accounts, region):
    parts = ["event_date >= current_date - $1::int", "dim_key = $2"]
    params: list = [days, dim_key]
    ep = _resolve_endpoint(endpoint)
    if ep != "all":
        params.append(ep); parts.append(f"endpoint = ${len(params)}")
    accts: list[str] = []
    for a in (accounts or []):
        accts.extend(x.strip() for x in str(a).split(",") if x.strip())
    accts = [a for a in accts if a and a != "all"]
    if accts:
        params.append(accts); parts.append(f"accountId = ANY(${len(params)}::text[])")
    if region and region != "all":
        params.append(region); parts.append(f"region = ${len(params)}")
    return parts, params
