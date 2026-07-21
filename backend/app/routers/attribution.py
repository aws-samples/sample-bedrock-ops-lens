"""Unified custom-attribute attribution.

Two attribution SOURCES surface the same "Usage · Custom Attributes" experience
(a top-bar attribute picker/filter + the attribution tab). An admin enables ONE
in Settings:

  Option 1 — invocation-log tags: Bedrock requestMetadata parsed into
             f_daily_tagged (runtime only; tokens + requests; no throttle/
             latency/quota — invocation logs don't carry those).
  Option 2 — proxy dimensions: a GenAI proxy emits per-request events into
             f_proxy_dim_hourly (runtime + mantle; tokens, throttle, latency,
             AND quota utilization).

This router hides which source is active behind a common shape so the frontend
is source-agnostic. Panels the active source can't populate are simply omitted
by the UI (same "show what the signal supports" rule as the Mantle sub-tabs).

The admin's explicit choice wins even if both sources have data.

Endpoints:
  GET /attribution/config      — {source, effective_source, available:{...}}
  PUT /attribution/source      — admin sets 'invocation_logs' | 'proxy' | 'off'
  GET /attribution/dimensions  — dim keys (+ values) from the effective source
  GET /attribution/values      — values for one key
  GET /attribution/usage       — per-value aggregates for one key
  GET /attribution/quota       — per-value TPM quota util (proxy source only)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .. import db
from ..auth import is_admin
from ..burndown import output_burndown_rate

router = APIRouter()

_SOURCE_KEY = "attribution_source"          # ingestion_meta: invocation_logs|proxy|off
_DEFAULT_DIM = "workload"
_VALID_SOURCES = ("invocation_logs", "proxy", "off")


async def _has_proxy() -> bool:
    try:
        r = await db.fetch("SELECT EXISTS(SELECT 1 FROM f_proxy_dim_hourly) AS ok")
        return bool(r and r[0]["ok"])
    except Exception:
        return False


async def _has_tags() -> bool:
    try:
        r = await db.fetch(
            "SELECT EXISTS(SELECT 1 FROM dim_tags WHERE tag_key <> '__none__') AS ok")
        return bool(r and r[0]["ok"])
    except Exception:
        return False


async def _configured_source() -> str:
    try:
        row = await db.fetchrow(
            "SELECT value FROM ingestion_meta WHERE key = $1", _SOURCE_KEY)
        v = str(row["value"]).lower() if row else ""
        return v if v in _VALID_SOURCES else "off"
    except Exception:
        return "off"


async def _effective_source() -> str:
    """The source actually used: the admin's choice if it has data; else
    auto-fall-back to whichever source HAS data (so a customer who never opened
    Settings still gets attribution when data lands). 'off' if neither."""
    configured = await _configured_source()
    has_proxy = await _has_proxy()
    has_tags = await _has_tags()
    if configured == "proxy" and has_proxy:
        return "proxy"
    if configured == "invocation_logs" and has_tags:
        return "invocation_logs"
    if configured == "off":
        # Not explicitly configured — auto-detect (proxy preferred for richness).
        if has_proxy:
            return "proxy"
        if has_tags:
            return "invocation_logs"
    # Configured for a source that has no data yet — honor the intent so the
    # tab shows its setup/empty state rather than silently using the other one.
    return configured if configured != "off" else "off"


@router.get("/attribution/config")
async def attribution_config():
    source = await _configured_source()
    eff = await _effective_source()
    return {
        "source": source,                 # what the admin selected
        "effective_source": eff,           # what's actually served
        "available": {
            "proxy": await _has_proxy(),
            "invocation_logs": await _has_tags(),
        },
        # Which metrics the effective source can populate — drives which panels
        # the UI renders. invocation logs = volume only; proxy = everything.
        "capabilities": {
            "tokens": eff in ("proxy", "invocation_logs"),
            "requests": eff in ("proxy", "invocation_logs"),
            "errors": eff in ("proxy", "invocation_logs"),
            "throttle": eff == "proxy",
            "latency": eff == "proxy",
            "quota": eff == "proxy",
            "mantle": eff == "proxy",
        },
    }


@router.put("/attribution/source")
async def set_attribution_source(request: Request, body: dict):
    if not is_admin(request):
        raise HTTPException(403, detail="admin access required")
    source = (body.get("source") or "").lower()
    if source not in _VALID_SOURCES:
        raise HTTPException(400, detail=f"source must be one of {_VALID_SOURCES}")
    await db.fetchval(
        """
        INSERT INTO ingestion_meta (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        RETURNING key
        """,
        _SOURCE_KEY, source,
    )
    return {"ok": True, "source": source}


# --- source-specific query helpers -----------------------------------------

async def _dimensions_proxy():
    rows = await db.fetch(
        "SELECT dim_key, dim_value, total_requests_30d, endpoints "
        "FROM dim_proxy_dimensions ORDER BY dim_key, total_requests_30d DESC")
    return [(r["dim_key"], r["dim_value"], int(r["total_requests_30d"] or 0),
             list(r["endpoints"] or [])) for r in rows]


async def _dimensions_tags():
    rows = await db.fetch(
        "SELECT tag_key AS dim_key, tag_value AS dim_value, total_requests_30d "
        "FROM dim_tags WHERE tag_key <> '__none__' "
        "ORDER BY tag_key, total_requests_30d DESC")
    return [(r["dim_key"], r["dim_value"], int(r["total_requests_30d"] or 0),
             ["runtime"]) for r in rows]


@router.get("/attribution/dimensions")
async def attribution_dimensions():
    eff = await _effective_source()
    if eff == "proxy":
        triples = await _dimensions_proxy()
    elif eff == "invocation_logs":
        triples = await _dimensions_tags()
    else:
        return {"source": eff, "default_key": _DEFAULT_DIM, "dimensions": []}

    by_key: dict[str, list] = {}
    for dim_key, dim_value, reqs, eps in triples:
        by_key.setdefault(dim_key, []).append(
            {"value": dim_value, "total_requests": reqs, "endpoints": eps})
    key_volume = {k: sum(v["total_requests"] for v in vs) for k, vs in by_key.items()}
    ordered = sorted(by_key.keys(), key=lambda k: (k != _DEFAULT_DIM, -key_volume[k]))
    default_key = _DEFAULT_DIM if _DEFAULT_DIM in by_key else (ordered[0] if ordered else _DEFAULT_DIM)
    return {
        "source": eff,
        "default_key": default_key,
        "dimensions": [{"key": k, "values": by_key[k]} for k in ordered],
    }


@router.get("/attribution/keys")
async def attribution_keys():
    """Available attribute KEYS for the effective source, with volume — powers
    the Settings 'which keys to surface' multiselect (symmetric across sources).
    """
    eff = await _effective_source()
    if eff == "proxy":
        rows = await db.fetch(
            "SELECT dim_key AS key, SUM(total_requests_30d)::BIGINT AS reqs, "
            "COUNT(*)::BIGINT AS values "
            "FROM dim_proxy_dimensions GROUP BY dim_key ORDER BY reqs DESC")
    elif eff == "invocation_logs":
        rows = await db.fetch(
            "SELECT tag_key AS key, SUM(total_requests_30d)::BIGINT AS reqs, "
            "COUNT(*)::BIGINT AS values "
            "FROM dim_tags WHERE tag_key <> '__none__' GROUP BY tag_key ORDER BY reqs DESC")
    else:
        return {"source": eff, "keys": []}
    return {"source": eff, "keys": [
        {"key": r["key"], "total_requests": int(r["reqs"] or 0),
         "distinct_values": int(r["values"] or 0)} for r in rows]}


@router.get("/attribution/values")
async def attribution_values(dim_key: str = Query(..., min_length=1)):
    eff = await _effective_source()
    if eff == "proxy":
        rows = await db.fetch(
            "SELECT dim_value, total_requests_30d FROM dim_proxy_dimensions "
            "WHERE dim_key = $1 ORDER BY total_requests_30d DESC", dim_key)
    elif eff == "invocation_logs":
        rows = await db.fetch(
            "SELECT tag_value AS dim_value, total_requests_30d FROM dim_tags "
            "WHERE tag_key = $1 ORDER BY total_requests_30d DESC", dim_key)
    else:
        return []
    return [{"value": r["dim_value"], "total_requests_30d": int(r["total_requests_30d"] or 0)}
            for r in rows]


@router.get("/attribution/usage")
async def attribution_usage(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """Per-value aggregates for one attribute key from the effective source.
    Proxy source carries throttle/latency; invocation-log source returns those
    as null (the UI omits those panels via /attribution/config capabilities)."""
    eff = await _effective_source()
    vals = [v for v in (dim_value or []) if v and v != "all"]

    if eff == "proxy":
        ep = endpoint if endpoint in ("runtime", "mantle", "all") else "all"
        where = ["event_date >= current_date - $1::int", "dim_key = $2"]
        params: list = [days, dim_key]
        if ep != "all":
            params.append(ep); where.append(f"endpoint = ${len(params)}")
        if vals:
            params.append(vals); where.append(f"dim_value = ANY(${len(params)}::text[])")
        rows = await db.fetch(
            f"""
            SELECT dim_value AS workload,
              SUM(total_requests)::BIGINT   AS total_requests,
              SUM(input_tokens)::BIGINT     AS input_tokens,
              SUM(output_tokens)::BIGINT    AS output_tokens,
              SUM(throttled_count)::BIGINT  AS throttled,
              SUM(error_count)::BIGINT      AS errors,
              ROUND(100.0*SUM(throttled_count)/NULLIF(SUM(total_requests),0),3) AS throttle_pct,
              ROUND(100.0*SUM(error_count)/NULLIF(SUM(total_requests),0),3)     AS error_pct,
              MAX(p50_latency_ms) AS p50_latency_ms,
              MAX(p90_latency_ms) AS p90_latency_ms,
              MAX(p99_latency_ms) AS p99_latency_ms,
              array_agg(DISTINCT endpoint) AS endpoints
            FROM f_proxy_dim_hourly WHERE {" AND ".join(where)}
            GROUP BY dim_value HAVING SUM(total_requests) > 0
            ORDER BY total_requests DESC LIMIT 500
            """, *params)
        return db.rows_to_dicts(rows)

    if eff == "invocation_logs":
        where = ["event_date >= current_date - $1::int", "tag_key = $2"]
        params = [days, dim_key]
        if vals:
            params.append(vals); where.append(f"tag_value = ANY(${len(params)}::text[])")
        rows = await db.fetch(
            f"""
            SELECT tag_value AS workload,
              SUM(total_requests)::BIGINT  AS total_requests,
              SUM(total_input_tokens)::BIGINT  AS input_tokens,
              SUM(total_output_tokens)::BIGINT AS output_tokens,
              NULL::BIGINT AS throttled,
              SUM(failed_requests)::BIGINT AS errors,
              NULL::NUMERIC AS throttle_pct,
              ROUND(100.0*SUM(failed_requests)/NULLIF(SUM(total_requests),0),3) AS error_pct,
              NULL::DOUBLE PRECISION AS p50_latency_ms,
              NULL::DOUBLE PRECISION AS p90_latency_ms,
              NULL::DOUBLE PRECISION AS p99_latency_ms,
              ARRAY['runtime'] AS endpoints
            FROM f_daily_tagged WHERE {" AND ".join(where)}
            GROUP BY tag_value HAVING SUM(total_requests) > 0
            ORDER BY total_requests DESC LIMIT 500
            """, *params)
        return db.rows_to_dicts(rows)

    return []


@router.get("/attribution/quota")
async def attribution_quota(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
):
    """Per-value TPM quota-utilization estimate — proxy source only (invocation
    logs carry no throttle/quota signal). Returns {is_estimate, rows, source}."""
    eff = await _effective_source()
    if eff != "proxy":
        return {"is_estimate": True, "rows": [], "source": eff,
                "note": "Quota utilization requires the proxy attribution source."}

    ep = endpoint if endpoint in ("runtime", "mantle", "all") else "all"
    params: list = [days, dim_key]
    ep_clause = ""
    if ep != "all":
        params.append(ep); ep_clause = f" AND endpoint = ${len(params)}"
    rows = await db.fetch(
        f"""
        SELECT dim_value, modelId, endpoint, region, event_date, hour,
          SUM(input_tokens)::BIGINT AS input_tokens,
          SUM(output_tokens)::BIGINT AS output_tokens
        FROM f_proxy_dim_hourly
        WHERE event_date >= current_date - $1::int AND dim_key = $2{ep_clause}
        GROUP BY dim_value, modelId, endpoint, region, event_date, hour
        """, *params)

    quota_rows = await db.fetch(
        "SELECT region, model_name, applied_value, default_value "
        "FROM f_quotas WHERE metric = 'TPM'")
    region_quotas: dict[str, list[tuple[str, float]]] = {}
    for q in quota_rows:
        lim = q["applied_value"] or q["default_value"]
        if lim:
            region_quotas.setdefault(q["region"], []).append(
                (str(q["model_name"]).lower(), float(lim)))

    def _limit_for(model_id: str, region: str):
        cands = region_quotas.get(region) or [c for lst in region_quotas.values() for c in lst]
        if not cands:
            return None
        mid = model_id.lower()
        best = None
        for name, lim in cands:
            toks = [t for t in name.replace("-", " ").split() if len(t) > 2]
            if any(t in mid for t in toks):
                best = lim if best is None else min(best, lim)
        return best if best is not None else min(lim for _, lim in cands)

    from collections import defaultdict
    peak: dict[tuple, float] = defaultdict(float)
    meta: dict[tuple, tuple] = {}
    for r in rows:
        is_mantle = (r["endpoint"] == "mantle")
        mid = r.get("modelid") or r.get("modelId")
        rate = 1 if is_mantle else output_burndown_rate(mid, is_mantle=is_mantle)
        tpm = (int(r["input_tokens"] or 0) + int(r["output_tokens"] or 0) * rate) / 60.0
        k = (r["dim_value"], mid)
        if tpm > peak[k]:
            peak[k] = tpm; meta[k] = (r["endpoint"], r["region"])

    result: dict[str, dict] = {}
    for (dim_value, mid), tpm in peak.items():
        _ep, region = meta[(dim_value, mid)]
        limit = _limit_for(mid, region)
        util = (tpm / limit * 100.0) if limit else None
        cand = {"workload": dim_value, "peak_tpm": round(tpm, 1), "model": mid,
                "region": region, "tpm_limit": round(limit, 1) if limit else None,
                "utilization_pct": round(util, 2) if util is not None else None}
        cur = result.get(dim_value)
        if cur is None or (util is not None and (cur["utilization_pct"] or -1) < util):
            result[dim_value] = cand

    out = sorted(result.values(), key=lambda d: (d["utilization_pct"] or 0), reverse=True)
    return {"is_estimate": True, "source": eff, "rows": out}


# ===========================================================================
# Cross-tab proxy re-slice
# ---------------------------------------------------------------------------
# When the PROXY source is active and the user selects an attribute value in the
# top bar, the CloudWatch-backed tabs (Overview volume/KPIs, Latency) can't
# filter by that attribute — native metrics have no attribute dimension. But the
# proxy event stream DOES (f_proxy_dim_hourly carries tokens/throttle/error/
# latency per attribute value). These endpoints re-serve those tabs' shapes from
# the proxy data, filtered by attribute, so the whole dashboard honors the
# filter. The frontend swaps to these only while a proxy attribute filter is
# active, and shows a "filtered to <attr> · proxy-sourced" provenance banner.
#
# Only signals the proxy actually carries are re-served (volume, tokens,
# throttle, errors, latency). Attribute-less concepts (traffic-type, CRIS,
# per-status-code) have no proxy equivalent and are left to the native tabs.
# ===========================================================================

def _proxy_where(days: int, dim_key: str, dim_value, endpoint: str,
                 start_param: int = 1):
    """Build a WHERE for f_proxy_dim_hourly pinned to one dim_key (+ optional
    values, endpoint). Returns (sql, params)."""
    where = [f"event_date >= current_date - ${start_param}::int",
             f"dim_key = ${start_param+1}"]
    params: list = [days, dim_key]
    ep = endpoint if endpoint in ("runtime", "mantle", "all") else "all"
    if ep != "all":
        params.append(ep); where.append(f"endpoint = ${len(params)}")
    vals = [v for v in (dim_value or []) if v and v != "all"]
    if vals:
        params.append(vals); where.append(f"dim_value = ANY(${len(params)}::text[])")
    return " AND ".join(where), params


def _tagged_where(days: int, dim_key: str, dim_value):
    """Build a WHERE for f_daily_tagged pinned to one tag_key (+ optional values).
    Used when the attribution source is invocation_logs: the top-bar attribute
    filter re-slices the CW-backed tabs from the invocation-log tag fan-out (the
    only per-attribute breakdown available for that source). f_daily_tagged is
    runtime-only and carries no throttle/latency, so those signals are 0/NULL —
    matching the invocation_logs capability set. Returns (sql, params)."""
    where = ["event_date >= current_date - $1::int", "tag_key = $2"]
    params: list = [days, dim_key]
    vals = [v for v in (dim_value or []) if v and v != "all"]
    if vals:
        params.append(vals); where.append(f"tag_value = ANY(${len(params)}::text[])")
    return " AND ".join(where), params


@router.get("/attribution/xtab/summary")
async def xtab_summary(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """Overview KPI shape, attribute-filtered from the effective source
    (proxy → f_proxy_dim_hourly; invocation_logs → f_daily_tagged)."""
    if await _effective_source() == "invocation_logs":
        w, params = _tagged_where(days, dim_key, dim_value)
        row = await db.fetchrow(
            f"""
            SELECT
              COALESCE(SUM(total_requests),0)::BIGINT   AS total_requests,
              COALESCE(SUM(total_requests - COALESCE(failed_requests,0)),0)::BIGINT AS successful_requests,
              COALESCE(SUM(failed_requests),0)::BIGINT  AS failed_requests,
              COALESCE(SUM(total_input_tokens),0)::BIGINT  AS total_input_tokens,
              COALESCE(SUM(total_output_tokens),0)::BIGINT AS total_output_tokens,
              0::BIGINT                                 AS throttled_requests,
              0::BIGINT                                 AS server_errors,
              COUNT(DISTINCT accountId)::BIGINT         AS unique_accounts
            FROM f_daily_tagged WHERE {w}
            """, *params)
        return dict(row) if row else {}
    w, params = _proxy_where(days, dim_key, dim_value, endpoint)
    row = await db.fetchrow(
        f"""
        SELECT
          COALESCE(SUM(total_requests),0)::BIGINT   AS total_requests,
          COALESCE(SUM(total_requests - throttled_count - error_count),0)::BIGINT AS successful_requests,
          COALESCE(SUM(throttled_count + error_count),0)::BIGINT AS failed_requests,
          COALESCE(SUM(input_tokens),0)::BIGINT     AS total_input_tokens,
          COALESCE(SUM(output_tokens),0)::BIGINT    AS total_output_tokens,
          COALESCE(SUM(throttled_count),0)::BIGINT  AS throttled_requests,
          COALESCE(SUM(error_count),0)::BIGINT      AS server_errors,
          COUNT(DISTINCT accountId)::BIGINT         AS unique_accounts
        FROM f_proxy_dim_hourly WHERE {w}
        """, *params)
    return dict(row) if row else {}


@router.get("/attribution/xtab/daily-trend")
async def xtab_daily_trend(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """Overview daily-trend shape, attribute-filtered from the effective source."""
    if await _effective_source() == "invocation_logs":
        w, params = _tagged_where(days, dim_key, dim_value)
        rows = await db.fetch(
            f"""
            SELECT EXTRACT(YEAR FROM event_date)::INT AS year,
                   EXTRACT(MONTH FROM event_date)::INT AS month,
                   EXTRACT(DAY FROM event_date)::INT AS day,
              SUM(total_requests)::BIGINT AS total_requests,
              SUM(total_requests - COALESCE(failed_requests,0))::BIGINT AS successful_requests,
              SUM(COALESCE(failed_requests,0))::BIGINT AS failed_requests,
              SUM(total_input_tokens)::BIGINT  AS input_tokens,
              SUM(total_output_tokens)::BIGINT AS output_tokens,
              SUM(COALESCE(total_cache_read_input_tokens,0))::BIGINT AS cache_read_tokens,
              0::BIGINT AS throttled,
              SUM(total_requests)::BIGINT AS runtime_requests,
              0::BIGINT AS mantle_requests
            FROM f_daily_tagged WHERE {w}
            GROUP BY event_date ORDER BY event_date
            """, *params)
        return db.rows_to_dicts(rows)
    w, params = _proxy_where(days, dim_key, dim_value, endpoint)
    rows = await db.fetch(
        f"""
        SELECT EXTRACT(YEAR FROM event_date)::INT AS year,
               EXTRACT(MONTH FROM event_date)::INT AS month,
               EXTRACT(DAY FROM event_date)::INT AS day,
          SUM(total_requests)::BIGINT AS total_requests,
          SUM(total_requests - throttled_count - error_count)::BIGINT AS successful_requests,
          SUM(throttled_count + error_count)::BIGINT AS failed_requests,
          SUM(input_tokens)::BIGINT  AS input_tokens,
          SUM(output_tokens)::BIGINT AS output_tokens,
          0::BIGINT AS cache_read_tokens,
          SUM(throttled_count)::BIGINT AS throttled,
          SUM(CASE WHEN endpoint='runtime' THEN total_requests ELSE 0 END)::BIGINT AS runtime_requests,
          SUM(CASE WHEN endpoint='mantle'  THEN total_requests ELSE 0 END)::BIGINT AS mantle_requests
        FROM f_proxy_dim_hourly WHERE {w}
        GROUP BY event_date ORDER BY event_date
        """, *params)
    return db.rows_to_dicts(rows)


@router.get("/attribution/xtab/by-model")
async def xtab_by_model(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """requests-by-model shape, attribute-filtered from the effective source."""
    if await _effective_source() == "invocation_logs":
        w, params = _tagged_where(days, dim_key, dim_value)
        rows = await db.fetch(
            f"""
            SELECT modelId,
              SUM(total_requests)::BIGINT AS total_requests,
              SUM(total_input_tokens)::BIGINT   AS input_tokens,
              SUM(total_output_tokens)::BIGINT  AS output_tokens
            FROM f_daily_tagged WHERE {w}
            GROUP BY modelId ORDER BY total_requests DESC
            """, *params)
        return db.rows_to_dicts(rows)
    w, params = _proxy_where(days, dim_key, dim_value, endpoint)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_requests)::BIGINT AS total_requests,
          SUM(input_tokens)::BIGINT   AS input_tokens,
          SUM(output_tokens)::BIGINT  AS output_tokens
        FROM f_proxy_dim_hourly WHERE {w}
        GROUP BY modelId ORDER BY total_requests DESC
        """, *params)
    return db.rows_to_dicts(rows)


@router.get("/attribution/xtab/latency-by-model")
async def xtab_latency_by_model(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """latency-by-model shape, attribute-filtered. Only the proxy source carries
    latency — invocation logs don't, so under invocation_logs we return [] and
    the Latency tab shows its graceful "not available for this source" state."""
    if await _effective_source() == "invocation_logs":
        return []
    w, params = _proxy_where(days, dim_key, dim_value, endpoint)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_requests)::BIGINT AS sample_count,
          SUM(p50_latency_ms * total_requests)/NULLIF(SUM(total_requests),0) AS avg_e2e,
          MAX(p50_latency_ms) AS p50_e2e,
          MAX(p90_latency_ms) AS p90_e2e,
          MAX(p99_latency_ms) AS p99_e2e,
          NULL::DOUBLE PRECISION AS avg_ttft,
          NULL::DOUBLE PRECISION AS p50_ttft,
          NULL::DOUBLE PRECISION AS p90_ttft,
          NULL::DOUBLE PRECISION AS p99_ttft
        FROM f_proxy_dim_hourly WHERE {w}
        GROUP BY modelId HAVING SUM(total_requests) > 0
        ORDER BY sample_count DESC
        """, *params)
    return db.rows_to_dicts(rows)


@router.get("/attribution/xtab/breakdown")
async def xtab_breakdown(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    group_by: str = Query("model"),
    top_n: int = Query(8, ge=1, le=20),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """Daily request-volume breakdown (Overview's main stacked chart), attribute-
    filtered from the effective source. Mirrors /breakdown's shape
    (year/month/day/category/total_requests) with top-N + 'Other' folding.
    Only 'model' grouping has a per-attribute source; other groupings fall back
    to model so the chart still re-slices rather than ignoring the filter."""
    inv = await _effective_source() == "invocation_logs"
    table = "f_daily_tagged" if inv else "f_proxy_dim_hourly"
    if inv:
        w, params = _tagged_where(days, dim_key, dim_value)
    else:
        w, params = _proxy_where(days, dim_key, dim_value, endpoint)
    # top-N model categories within the filtered slice
    top = await db.fetch(
        f"SELECT modelId AS cat, SUM(total_requests)::BIGINT AS t "
        f"FROM {table} WHERE {w} GROUP BY modelId ORDER BY t DESC LIMIT {top_n}",
        *params)
    cats = [r["cat"] for r in top]
    if not cats:
        return []
    rows = await db.fetch(
        f"""
        SELECT EXTRACT(YEAR FROM event_date)::INT AS year,
               EXTRACT(MONTH FROM event_date)::INT AS month,
               EXTRACT(DAY FROM event_date)::INT AS day,
          CASE WHEN modelId = ANY(${len(params)+1}::text[]) THEN modelId ELSE 'Other' END AS category,
          SUM(total_requests)::BIGINT AS total_requests
        FROM {table} WHERE {w}
        GROUP BY year, month, day, category
        ORDER BY year, month, day, category
        """, *params, cats)
    return db.rows_to_dicts(rows)


# --- attribute-filtered COST ------------------------------------------------
# Cost Explorer (f_daily_cost) has no attribute dimension, so an attribute
# filter can't slice it. But spend is derived from token volumes, and the
# per-attribute token breakdown IS available (f_daily_tagged for invocation_logs,
# f_proxy_dim_hourly for proxy). So we recompute cost = input×in_price +
# output×out_price per model, priced by provider — the same basis the cost
# routers use for the derived-cost path — filtered to the selected attribute.
def _price(model_id: str):
    from .model_insights import BEDROCK_PRICING, _provider_of
    return BEDROCK_PRICING.get(_provider_of(model_id), {"input": 0.50, "output": 1.50})


def _weight(in_tok: int, out_tok: int, mid: str) -> float:
    p = _price(mid)
    return (in_tok / 1_000_000) * p["input"] + (out_tok / 1_000_000) * p["output"]


async def _ce_total(days: int) -> float:
    """The REAL Cost Explorer total for the window (f_daily_cost). This is the
    invoice figure the per-attribute slices must sum back to."""
    row = await db.fetchrow(
        "SELECT COALESCE(SUM(total_cost),0)::numeric AS t FROM f_daily_cost "
        "WHERE event_date >= current_date - $1::int", days)
    return float(row["t"] or 0)


async def _attr_cost_fraction(days, dim_key, dim_value, endpoint) -> tuple[float, dict]:
    """Return (fraction, per_model_fraction) for the selected attribute slice.

    fraction = selected slice's token-cost weight ÷ token-cost weight across ALL
    values of dim_key. Multiplying the real CE total by this fraction makes the
    per-value slices sum EXACTLY to the CE total (prod+staging+dev = $809K), which
    is what a cost-attribution view must do. per_model_fraction maps modelId →
    that model's fraction-of-CE within the slice (for the daily stacked chart)."""
    inv = await _effective_source() == "invocation_logs"
    if inv:
        table, in_c, out_c = "f_daily_tagged", "total_input_tokens", "total_output_tokens"
        key_col = "tag_key"
        base_where = f"{key_col} = $2 AND event_date >= current_date - $1::int"
        base_params = [days, dim_key]
    else:
        table, in_c, out_c = "f_proxy_dim_hourly", "input_tokens", "output_tokens"
        key_col = "dim_key"
        base_where = f"{key_col} = $2 AND event_date >= current_date - $1::int"
        base_params = [days, dim_key]
    # Denominator: weight across ALL values of this key (the whole attributed pie).
    denom_rows = await db.fetch(
        f"SELECT modelId, SUM({in_c})::BIGINT i, SUM({out_c})::BIGINT o "
        f"FROM {table} WHERE {base_where} GROUP BY modelId", *base_params)
    denom = sum(_weight(int(r["i"] or 0), int(r["o"] or 0),
                        r["modelid"] if "modelid" in r else r["modelId"]) for r in denom_rows)
    # Numerator: weight for the selected value(s), per model.
    vals = [v for v in (dim_value or []) if v and v != "all"]
    val_col = "tag_value" if inv else "dim_value"
    num_where = base_where
    num_params = list(base_params)
    if vals:
        num_params.append(vals)
        num_where += f" AND {val_col} = ANY(${len(num_params)}::text[])"
    num_rows = await db.fetch(
        f"SELECT modelId, SUM({in_c})::BIGINT i, SUM({out_c})::BIGINT o "
        f"FROM {table} WHERE {num_where} GROUP BY modelId", *num_params)
    per_model_w = {}
    num = 0.0
    for r in num_rows:
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        wv = _weight(int(r["i"] or 0), int(r["o"] or 0), mid)
        per_model_w[mid] = per_model_w.get(mid, 0.0) + wv
        num += wv
    if denom <= 0:
        return 0.0, {}
    frac = num / denom
    per_model_frac = {mid: (w / denom) for mid, w in per_model_w.items()}
    return frac, per_model_frac


@router.get("/attribution/xtab/cost-summary")
async def xtab_cost_summary(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """Total-spend KPI, attribute-filtered as a SHARE of the real CE total.
    cost = CE_total × (slice token-cost weight ÷ all-values weight), so the per-
    value slices sum back to the invoice total. Active accounts/services are the
    distinct counts within the filtered slice (so the KPI tiles aren't zero)."""
    ce = await _ce_total(days)
    frac, _ = await _attr_cost_fraction(days, dim_key, dim_value, endpoint)
    total = round(ce * frac, 2)
    # Distinct accounts + models ("services") in the filtered slice, so the
    # Active accounts / Active services KPI tiles reflect the filter instead of 0.
    inv = await _effective_source() == "invocation_logs"
    if inv:
        w, params = _tagged_where(days, dim_key, dim_value)
        table = "f_daily_tagged"
    else:
        w, params = _proxy_where(days, dim_key, dim_value, endpoint)
        table = "f_proxy_dim_hourly"
    cnt = await db.fetchrow(
        f"SELECT COUNT(DISTINCT accountId)::INT AS accts, "
        f"COUNT(DISTINCT modelId)::INT AS models FROM {table} WHERE {w}", *params)
    return {
        "total_cost": total, "currency": "USD",
        "unique_accounts": int(cnt["accts"] or 0),
        "unique_services": int(cnt["models"] or 0),
        "previous_total_cost": 0.0,
        "by_endpoint": {"runtime": total, "mantle": 0.0, "allocated": True},
        "window": {"days": days},
        "attribute_filtered": True,
    }


@router.get("/attribution/xtab/cost-by-model")
async def xtab_cost_by_model(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
):
    """Daily per-model spend (cost stacked chart), attribute-filtered as a share
    of the real CE total. Distributes CE_total across (day, model) in proportion
    to the slice's per-(day,model) token-cost weight, so the chart totals match
    the filtered KPI and the slices sum to the invoice."""
    inv = await _effective_source() == "invocation_logs"
    if inv:
        w, params = _tagged_where(days, dim_key, dim_value)
        in_c, out_c, table = "total_input_tokens", "total_output_tokens", "f_daily_tagged"
    else:
        w, params = _proxy_where(days, dim_key, dim_value, endpoint)
        in_c, out_c, table = "input_tokens", "output_tokens", "f_proxy_dim_hourly"
    rows = await db.fetch(
        f"SELECT event_date, modelId, SUM({in_c})::BIGINT AS in_tok, "
        f"SUM({out_c})::BIGINT AS out_tok FROM {table} WHERE {w} "
        f"GROUP BY event_date, modelId ORDER BY event_date", *params)
    # weight per (day, model), and the slice's total weight
    weighted = []
    slice_w = 0.0
    for r in rows:
        mid = r["modelid"] if "modelid" in r else r["modelId"]
        wv = _weight(int(r["in_tok"] or 0), int(r["out_tok"] or 0), mid)
        if wv <= 0:
            continue
        weighted.append((r["event_date"], mid, wv))
        slice_w += wv
    if slice_w <= 0:
        return []
    # Scale to the slice's share of the real CE total, then split by weight.
    ce = await _ce_total(days)
    frac, _ = await _attr_cost_fraction(days, dim_key, dim_value, endpoint)
    slice_dollars = ce * frac
    out = []
    for ev, mid, wv in weighted:
        out.append({"event_date": ev.isoformat(), "model_label": mid,
                    "total_cost": round(slice_dollars * (wv / slice_w), 4), "derived": True})
    return out
