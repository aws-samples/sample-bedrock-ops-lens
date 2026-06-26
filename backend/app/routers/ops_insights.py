"""Ops Insights tab — CRIS adoption, throttle hotspots, peak RPM, caching, etc."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..burndown import output_burndown_rate
from ..filters import FilterSet, build_where, parse_filters

router = APIRouter()


# ---------------------------------------------------------------------------
# CRIS adoption
# ---------------------------------------------------------------------------
@router.get("/ops-cris-adoption")
async def ops_cris_adoption(f: FilterSet = Depends(parse_filters)):
    """Per-model CRIS vs on-demand request split."""
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)::BIGINT AS cris_requests,
          SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)::BIGINT AS od_requests,
          ROUND(100.0 * SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)
              / NULLIF(SUM(total_requests), 0), 2) AS cris_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(total_requests) > 0
        ORDER BY (SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)
              + SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)) DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


@router.get("/ops-cris-by-account")
async def ops_cris_by_account(f: FilterSet = Depends(parse_filters)):
    """Per-account CRIS vs OD breakdown — find accounts not using CRIS."""
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId,
          SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)::BIGINT AS cris_requests,
          SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)::BIGINT AS od_requests
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId
        HAVING SUM(total_requests) > 0
        ORDER BY (SUM(CASE WHEN traffic_type IN
                ('CROSS_REGION_OD_INFERENCE_REQUEST',
                 'SOURCE_REGION_OD_INFERENCE_REQUEST')
              THEN total_requests ELSE 0 END)
              + SUM(CASE WHEN traffic_type = 'ON_DEMAND_INFERENCE_REQUEST'
              THEN total_requests ELSE 0 END)) DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Throttle rate hotspots
# ---------------------------------------------------------------------------
@router.get("/ops-throttle-rate")
async def ops_throttle_rate(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          SUM(total_requests)::BIGINT   AS total_requests,
          SUM(status_429_count)::BIGINT AS throttled,
          ROUND(100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0), 3)
            AS throttle_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(status_429_count) > 0 AND SUM(total_requests) > 100
        ORDER BY throttle_pct DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Peak RPM / TPM (max-over-hour from f_hourly_peak)
# ---------------------------------------------------------------------------
@router.get("/ops-peak-rpm")
async def ops_peak_rpm(f: FilterSet = Depends(parse_filters)):
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
    w = " AND ".join(parts)

    # Fetch per-hour rows and reduce in Python so the output-token burndown
    # multiplier can be applied to each hour BEFORE the peak is taken (the rate
    # is per-model, and the busiest quota-hour can differ from the busiest
    # raw-token hour — so we cannot pre-sum then multiply). See app/burndown.py
    # and the AWS quota-token-burndown doc.
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          total_requests,
          CASE WHEN total_cache_read_input_tokens IS NULL THEN NULL
               ELSE GREATEST(total_input_tokens - total_cache_read_input_tokens, 0) END AS input_quota_tokens,
          total_output_tokens
        FROM f_hourly_peak
        WHERE {w}
        """,
        *params,
    )

    agg: dict = {}
    for r in db.rows_to_dicts(rows):
        mid = r.get("modelid") or r.get("modelId")
        key = (r.get("accountid") or r.get("accountId"), mid, r["region"])
        a = agg.get(key)
        if a is None:
            rate = output_burndown_rate(mid)
            a = agg[key] = {
                "accountId": key[0], "modelId": mid, "region": key[2],
                "burndown_rate": rate,
                "peak_requests_hour": 0,
                "peak_input_tpm": 0,    # raw input (cache-read excluded)
                "peak_output_tpm": 0,   # raw output, 1:1
                "peak_quota_tpm": 0,    # (input) + output*rate, weighted per-hour
            }
        rate = a["burndown_rate"]
        req = int(r["total_requests"] or 0)
        out = int(r["total_output_tokens"] or 0)
        inp = r["input_quota_tokens"]
        a["peak_requests_hour"] = max(a["peak_requests_hour"], req)
        a["peak_output_tpm"] = max(a["peak_output_tpm"], out)
        if inp is not None:
            inp = int(inp)
            a["peak_input_tpm"] = max(a["peak_input_tpm"], inp)
            a["peak_quota_tpm"] = max(a["peak_quota_tpm"], inp + out * rate)

    out_rows = [a for a in agg.values() if a["peak_requests_hour"] > 0]
    out_rows.sort(key=lambda a: a["peak_requests_hour"], reverse=True)
    return out_rows[:200]


# ---------------------------------------------------------------------------
# Burndown risk — Claude 4+ family using > some % of applied quota.
# ---------------------------------------------------------------------------
@router.get("/ops-burndown-risk")
async def ops_burndown_risk(f: FilterSet = Depends(parse_filters)):
    """Joins f_hourly_peak with f_quotas (applied TPM) to surface Claude
    deployments approaching their TPM limit, with the per-model output-token
    burndown multiplier applied (15x Opus 4.8 / 5x other Claude 3.7+ / 1x else)
    so "Peak TPM (quota)" matches how CloudWatch burns down the quota.

    The multiplier is applied to output per-hour BEFORE the peak is taken
    (see app/burndown.py), then joined to the applied quota. The Claude-family
    filter matches both bare ids (`anthropic.claude-...`) and CRIS-prefixed ids
    (`us.`/`eu.`/`apac.`/`global.` ... + `anthropic.claude-...`) — a bare-prefix
    filter silently dropped all cross-region traffic, which is most of it."""
    parts = ["h.event_date BETWEEN $1::date AND $2::date",
             "h.modelId LIKE '%anthropic.claude-%'"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"h.accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"h.region = ${len(params)+1}")
        params.append(f.region)
    w = " AND ".join(parts)

    # Per-hour rows; reduce in Python so the per-model rate is applied to each
    # hour's output before the peak (the rate is per-model so it can't live in
    # the SQL aggregate, and the busiest quota-hour differs from the busiest
    # raw-token hour).
    hourly = await db.fetch(
        f"""
        SELECT h.accountId, h.modelId, h.region,
          CASE WHEN h.total_cache_read_input_tokens IS NULL THEN NULL
               ELSE GREATEST(h.total_input_tokens - h.total_cache_read_input_tokens, 0) END AS input_quota_tokens,
          h.total_output_tokens
        FROM f_hourly_peak h
        WHERE {w}
        """,
        *params,
    )

    peaks: dict = {}
    for r in db.rows_to_dicts(hourly):
        mid = r.get("modelid") or r.get("modelId")
        key = (r.get("accountid") or r.get("accountId"), mid, r["region"])
        inp = r["input_quota_tokens"]
        if inp is None:
            continue  # cache split unknown -> can't compute quota-accurate TPM
        p = peaks.get(key)
        if p is None:
            p = peaks[key] = {
                "accountId": key[0], "modelId": mid, "region": key[2],
                "burndown_rate": output_burndown_rate(mid),
                "peak_output_tpm": 0, "peak_quota_tpm": 0,
            }
        out = int(r["total_output_tokens"] or 0)
        p["peak_output_tpm"] = max(p["peak_output_tpm"], out)
        p["peak_quota_tpm"] = max(p["peak_quota_tpm"], int(inp) + out * p["burndown_rate"])

    if not peaks:
        return []

    # Applied TPM quotas for the in-scope accounts/regions; fuzz-match the
    # human model_name against the technical modelId (same heuristic as the
    # quota drill-down).
    from .quota_drilldown import _matches
    qparts = ["metric = 'TPM'", "applied_value IS NOT NULL"]
    qparams: list = []
    accts = sorted({k[0] for k in peaks})
    qparts.append(f"accountId = ANY(${len(qparams)+1}::text[])")
    qparams.append(accts)
    if f.region != "all":
        qparts.append(f"region = ${len(qparams)+1}")
        qparams.append(f.region)
    quota_rows = db.rows_to_dicts(await db.fetch(
        f"SELECT accountId, region, model_name, applied_value FROM f_quotas WHERE {' AND '.join(qparts)}",
        *qparams,
    ))

    def _applied_tpm(acct, region, model_id):
        best = None
        for q in quota_rows:
            if (q.get("accountid") or q.get("accountId")) != acct or q["region"] != region:
                continue
            if not _matches(q["model_name"], model_id):
                continue
            val = float(q["applied_value"])
            if best is None or val > best:
                best = val
        return best

    out_rows = []
    for p in peaks.values():
        applied = _applied_tpm(p["accountId"], p["region"], p["modelId"])
        if applied is None:
            continue
        peak_quota_tpm = p["peak_quota_tpm"]
        out_rows.append({
            "accountId": p["accountId"],
            "modelId": p["modelId"],
            "region": p["region"],
            "burndown_rate": p["burndown_rate"],
            "peak_tpm": int(peak_quota_tpm),       # quota-weighted (matches CW burndown)
            "peak_output_tpm": int(p["peak_output_tpm"]),
            "effective_tpm": int(applied),         # applied TPM quota
            "overhead_pct": round(100.0 * peak_quota_tpm / applied, 2) if applied else None,
        })
    out_rows.sort(key=lambda r: (r["overhead_pct"] is None, -(r["overhead_pct"] or 0)))
    return out_rows[:200]


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
@router.get("/ops-caching")
async def ops_caching(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_input_tokens)::BIGINT              AS total_input_tokens,
          SUM(total_cache_read_input_tokens)::BIGINT   AS cache_read_tokens,
          SUM(total_cache_write_input_tokens)::BIGINT  AS cache_write_tokens,
          -- Hit rate: cached / (cached + fresh-input). Cloudwatch's
          -- InputTokenCount excludes cache reads; they ship as a separate
          -- counter (CacheReadInputTokenCount), so the denominator is the
          -- sum or the ratio is unbounded.
          ROUND(100.0 * COALESCE(SUM(total_cache_read_input_tokens), 0)
              / NULLIF(COALESCE(SUM(total_cache_read_input_tokens), 0)
                       + COALESCE(SUM(total_input_tokens), 0), 0), 2) AS hit_rate_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY modelId
        HAVING SUM(total_input_tokens) > 0
        ORDER BY total_input_tokens DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Context length routing
# ---------------------------------------------------------------------------
@router.get("/ops-context-length")
async def ops_context_length(f: FilterSet = Depends(parse_filters)):
    parts = ["event_date BETWEEN $1::date AND $2::date"]
    params: list = [f.start, f.end]
    if f.accounts:
        parts.append(f"accountId = ANY(${len(params)+1}::text[])")
        params.append(list(f.accounts))
    if f.region != "all":
        parts.append(f"region = ${len(params)+1}")
        params.append(f.region)
    w = " AND ".join(parts)

    rows = await db.fetch(
        f"""
        SELECT routed_model_id, modelId,
          SUM(total_requests)::BIGINT     AS total_requests,
          SUM(total_input_tokens)::BIGINT AS input_tokens
        FROM f_context_length
        WHERE {w}
        GROUP BY routed_model_id, modelId
        ORDER BY total_requests DESC
        """,
        *params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Request shape (avg input/output ratio)
# ---------------------------------------------------------------------------
@router.get("/ops-request-shape")
async def ops_request_shape(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT accountId, modelId, region,
          ROUND(SUM(total_input_tokens)::NUMERIC
                / NULLIF(SUM(total_requests), 0), 1) AS avg_input,
          ROUND(SUM(total_output_tokens)::NUMERIC
                / NULLIF(SUM(total_requests), 0), 1) AS avg_output,
          ROUND(SUM(total_output_tokens)::NUMERIC
                / NULLIF(SUM(total_input_tokens), 0), 3) AS ratio
        FROM f_daily
        WHERE {w.sql}
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) > 100
        ORDER BY SUM(total_requests) DESC
        LIMIT 200
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Service tier
# ---------------------------------------------------------------------------
@router.get("/ops-service-tier")
async def ops_service_tier(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT service_tier,
          SUM(total_requests)::BIGINT       AS total_requests,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts,
          ROUND(100.0 * SUM(status_429_count) / NULLIF(SUM(total_requests), 0), 3)
            AS throttle_pct
        FROM f_daily
        WHERE {w.sql}
        GROUP BY service_tier
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Inference profile prefix adoption
# ---------------------------------------------------------------------------
@router.get("/ops-inference-profile")
async def ops_inference_profile(f: FilterSet = Depends(parse_filters)):
    w = build_where(f)
    rows = await db.fetch(
        f"""
        SELECT inference_profile_prefix,
          SUM(total_requests)::BIGINT       AS total_requests,
          COUNT(DISTINCT accountId)::BIGINT AS unique_accounts
        FROM f_daily
        WHERE {w.sql}
        GROUP BY inference_profile_prefix
        ORDER BY total_requests DESC
        """,
        *w.params,
    )
    return db.rows_to_dicts(rows)
