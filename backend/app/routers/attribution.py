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

from .. import db, proxy_quota
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
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """Per-value aggregates for one attribute key from the effective source.
    Proxy source carries throttle/latency; invocation-log source returns those
    as null (the UI omits those panels via /attribution/config capabilities)."""
    eff = await _effective_source()
    vals = [v for v in (dim_value or []) if v and v != "all"]

    if eff == "proxy":
        # runtime/mantle are Bedrock paths; anthropic-api/openai-api are
        # direct-API paths that exist only in client telemetry (006).
        ep = endpoint if endpoint in (
            "runtime", "mantle", "anthropic-api", "openai-api", "all") else "all"
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
          'worst_bucket_upper_bound' AS percentile_basis,
              SUM(retried_count)::BIGINT AS retried,
              SUM(cost_usd_est)::DOUBLE PRECISION AS cost_usd_est,
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


# Provider = model family (whose model answered), rolled up ACROSS paths.
# Distinct from `endpoint` (the path the request traveled). Includes the
# direct-API paths only client telemetry can see — so this view answers
# "all my Anthropic usage (Bedrock + direct) vs all my OpenAI usage".
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


@router.get("/attribution/by-provider")
async def attribution_by_provider(days: int = Query(14, ge=1, le=90)):
    """Provider × path rollup from client telemetry (proxy source only —
    the other sources can't see direct-API traffic, so the panel self-gates).
    Reads the '__all__' accounting row, which is one row per request. The old
    comment here claimed "every key covers 100% of requests (fan-out invariant)",
    but client-reported attributes are SPARSE: a request that carries only `team`
    is absent from `workload`, so pinning to the busiest attribute key dropped
    those requests and could hide an entire provider whose callers never send it
    (audit T09)."""
    if await _effective_source() != "proxy":
        return []
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


@router.get("/attribution/quota")
async def attribution_quota(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
):
    """Per-value TPM quota-utilization estimate — proxy source only (invocation
    logs carry no throttle/quota signal). Returns {is_estimate, rows, source}.

    This is the endpoint the Workloads tab calls. It used to carry its OWN copy of
    the quota lookup, which was never fixed: it matched a quota row when ANY token
    of the quota name appeared in the model id, so a Sonnet model matched the
    Haiku row and inherited the smaller limit (100,000 TPM against a 1,000,000
    Sonnet limit read 200%, not 10%); it ignored the account entirely, though
    quotas are per (account, model, region); it fell back to the smallest limit in
    the region when nothing matched; and it scored direct-provider traffic against
    AWS quotas. Both quota endpoints now delegate to one resolver
    (app/proxy_quota.py) so there is a single behaviour to reason about.
    """
    eff = await _effective_source()
    if eff != "proxy":
        return {"is_estimate": True, "rows": [], "source": eff,
                "direct_provider_rows": [],
                "note": "Quota utilization requires the proxy attribution source."}

    parts = ["event_date >= current_date - $1::int", "dim_key = $2"]
    params: list = [days, dim_key]
    ep = endpoint if endpoint in PROXY_ENDPOINTS else "all"
    if ep != "all":
        params.append(ep); parts.append(f"endpoint = ${len(params)}")
    accts, reg = _scope_lists(accounts, region)
    if accts:
        params.append(accts); parts.append(f"accountId = ANY(${len(params)}::text[])")
    if reg:
        params.append(reg); parts.append(f"region = ${len(params)}")
    rows = db.rows_to_dicts(await db.fetch(
        f"""
        SELECT dim_value, modelId, endpoint, region, accountId,
          SUM(input_tokens)::BIGINT  AS input_tokens,
          SUM(output_tokens)::BIGINT AS output_tokens
        FROM f_proxy_dim_hourly
        WHERE {" AND ".join(parts)}
        GROUP BY dim_value, modelId, endpoint, region, accountId, event_date, hour
        """, *params))
    out = await proxy_quota.score(rows, group_key="dim_value")
    out["source"] = eff
    return out


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

# Endpoints the proxy stream can report. runtime/mantle are Bedrock (AWS bills
# them and they appear in Cost Explorer); anthropic-api/openai-api are calls made
# straight to the provider, which AWS does not bill at all (audit T07).
PROXY_ENDPOINTS = ("runtime", "mantle", "anthropic-api", "openai-api",
                   # Reported by an emitter whose provider we do not recognise.
                   # Not AWS-billed, so it gets no invoice share and no quota score.
                   "unknown")
AWS_BILLED_ENDPOINTS = ("runtime", "mantle")


def _scope_lists(accounts, region):
    """Normalise the top-bar scope params. `accounts` arrives either as repeated
    query params or as one comma-joined string (the frontend does the latter)."""
    accts: list[str] = []
    for a in (accounts or []):
        accts.extend(x.strip() for x in str(a).split(",") if x.strip())
    accts = [a for a in accts if a and a != "all"]
    reg = region if region and region != "all" else None
    return accts, reg


def _proxy_where(days: int, dim_key: str, dim_value, endpoint: str,
                 start_param: int = 1, accounts=None, region: str | None = None):
    """Build a WHERE for f_proxy_dim_hourly pinned to one dim_key (+ optional
    values, endpoint, account/region scope). Returns (sql, params).

    Audit T06: the top bar's region and account selections were passed to these
    endpoints and then ignored - FastAPI simply dropped the undeclared params -
    so an account-scoped view silently showed fleet-wide proxy numbers. Both
    columns exist on f_proxy_dim_hourly, so they are applied here now.
    """
    where = [f"event_date >= current_date - ${start_param}::int",
             f"dim_key = ${start_param+1}"]
    params: list = [days, dim_key]
    # The proxy stream carries four endpoint values, two of which are NOT AWS.
    # Accepting only runtime/mantle here meant selecting "anthropic-api" silently
    # fell back to "all" and showed the whole fleet (same class as finding 13).
    ep = endpoint if endpoint in PROXY_ENDPOINTS or endpoint == "all" else "all"
    if ep != "all":
        params.append(ep); where.append(f"endpoint = ${len(params)}")
    vals = [v for v in (dim_value or []) if v and v != "all"]
    if vals:
        params.append(vals); where.append(f"dim_value = ANY(${len(params)}::text[])")
    accts, reg = _scope_lists(accounts, region)
    if accts:
        params.append(accts); where.append(f"accountId = ANY(${len(params)}::text[])")
    if reg:
        params.append(reg); where.append(f"region = ${len(params)}")
    return " AND ".join(where), params


def _tagged_where(days: int, dim_key: str, dim_value,
                  accounts=None, region: str | None = None):
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
    # Same scope fix as _proxy_where (audit T06).
    accts, reg = _scope_lists(accounts, region)
    if accts:
        params.append(accts); where.append(f"accountId = ANY(${len(params)}::text[])")
    if reg:
        params.append(reg); where.append(f"region = ${len(params)}")
    return " AND ".join(where), params


# ---------------------------------------------------------------------------
# Multi-attribute (conjunctive) filtering
# ---------------------------------------------------------------------------
# Audit T06: the top bar lets the user select values from SEVERAL attributes at
# once (e.g. env=prod AND workload=search-service), but the request only ever
# carried one `dim_key`, and the frontend picked whichever key happened to be
# first in the list. The other selections were dropped with no indication, so the
# view claimed to be filtered on prod-search while actually showing every
# workload in prod.
#
# f_proxy_dim_hourly cannot express the conjunction: it is a FAN-OUT, one row per
# (dim_key, dim_value), so a row knows only one attribute of its request. An AND
# across attributes has to be evaluated per REQUEST, which means f_request_events
# and its JSONB `dimensions` map (GIN-indexed).
#
# So: one key -> the hourly table (cheap, full history). Several keys -> the raw
# table, exact but only for the raw retention window. When the requested window
# reaches back past that, we say so via `partial`/`dropped_filters` instead of
# quietly applying a subset.
RAW_EVENT_RETENTION_DAYS = 14


def parse_dim_filters(dim_filter, dim_key: str, dim_value) -> dict[str, list[str]]:
    """Normalise the selection into {key: [values]}.

    Accepts the new repeated `dim_filter=key:value` form and falls back to the
    single `dim_key`/`dim_value` pair so older clients keep working.
    """
    out: dict[str, list[str]] = {}
    for raw in (dim_filter or []):
        s = str(raw)
        if ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k or not v or v == "all":
            continue
        out.setdefault(k, [])
        if v not in out[k]:
            out[k].append(v)
    if not out:
        vals = [v for v in (dim_value or []) if v and v != "all"]
        if dim_key:
            out[dim_key] = vals
    return out


def dim_filter_plan(dim_filter, dim_key: str, dim_value, days: int) -> dict:
    """Decide how to serve the selection, and report anything not applied."""
    sel = parse_dim_filters(dim_filter, dim_key, dim_value)
    keys = [k for k, v in sel.items() if v]
    if len(keys) <= 1:
        return {"selection": sel, "mode": "hourly", "partial": False,
                "dropped_filters": [], "conjunctive": False}
    if days <= RAW_EVENT_RETENTION_DAYS:
        return {"selection": sel, "mode": "raw_events", "partial": False,
                "dropped_filters": [], "conjunctive": True}
    # Out of raw retention: we can only honour one attribute. Say which.
    primary = dim_key if dim_key in sel else keys[0]
    return {
        "selection": {primary: sel[primary]},
        "mode": "hourly",
        "partial": True,
        "conjunctive": False,
        "dropped_filters": [f"{k}={','.join(sel[k])}" for k in keys if k != primary],
        "reason": (f"Filtering on more than one attribute at once is evaluated per "
                   f"request, and per-request events are kept for "
                   f"{RAW_EVENT_RETENTION_DAYS} days. Narrow the window to "
                   f"{RAW_EVENT_RETENTION_DAYS} days or fewer to combine attributes."),
    }


async def _gate_raw_mode(plan: dict, days: int, endpoint: str, accounts=None,
                         region: str | None = None) -> dict:
    """Downgrade a raw-event plan to the hourly path when per-request rows do not
    cover the window, and say so rather than returning a confident zero."""
    if plan["mode"] != "raw_events":
        return plan
    cov = await raw_event_coverage(days, endpoint, accounts, region)
    if cov["sufficient"]:
        plan["raw_coverage"] = cov
        return plan
    keys = [k for k, v in plan["selection"].items() if v]
    primary = keys[0] if keys else ""
    return {
        **plan,
        "selection": {primary: plan["selection"].get(primary, [])} if primary else {},
        "mode": "hourly",
        "partial": True,
        "conjunctive": False,
        "dropped_filters": [f"{k}={','.join(plan['selection'][k])}"
                            for k in keys if k != primary],
        "reason": (
            "Combining attributes has to be evaluated per request, and this "
            f"deployment has per-request events for only {cov['coverage_ratio']:.0%} "
            "of the window's traffic. Showing the first attribute only; enable "
            "proxy raw-event ingestion (or shorten the window) to combine them."),
        "raw_coverage": cov,
    }


async def raw_event_coverage(days: int, endpoint: str, accounts=None,
                             region: str | None = None) -> dict:
    """How much of the window's traffic actually exists as per-request rows.

    The conjunctive filter can only be evaluated against f_request_events. If the
    deployment keeps a shorter raw window than the rollups (or raw ingestion is
    not wired at all), that table can be empty or sparse while the hourly rollup
    is complete - in which case answering from it would return a confidently
    wrong small number, or zero. So compare the two populations first and only
    use the raw path when it covers the window.
    """
    raw = await db.fetchrow(
        "SELECT COUNT(*)::BIGINT AS n FROM f_request_events "
        "WHERE event_date >= current_date - $1::int", days)
    hourly = await db.fetchrow(
        "SELECT COALESCE(SUM(total_requests),0)::BIGINT AS n FROM f_proxy_dim_hourly "
        "WHERE event_date >= current_date - $1::int AND dim_key = "
        "(SELECT dim_key FROM f_proxy_dim_hourly ORDER BY dim_key LIMIT 1)", days)
    n_raw = int((raw or {}).get("n") or 0)
    n_hourly = int((hourly or {}).get("n") or 0)
    ratio = (n_raw / n_hourly) if n_hourly else (1.0 if n_raw else 0.0)
    return {"raw_requests": n_raw, "rollup_requests": n_hourly,
            "coverage_ratio": round(ratio, 4), "sufficient": ratio >= 0.95}


def _plan_key(plan: dict, fallback: str) -> str:
    keys = [k for k, v in plan["selection"].items()]
    return keys[0] if keys else fallback


def _plan_values(plan: dict, fallback_key: str):
    return plan["selection"].get(_plan_key(plan, fallback_key), [])


def _plan_meta(plan: dict) -> dict:
    """Provenance the UI shows, so a partially applied filter is never silent."""
    meta = {
        "filters_applied": [f"{k}={','.join(v)}" for k, v in plan["selection"].items() if v],
        "filters_dropped": plan.get("dropped_filters", []),
        "partial": bool(plan.get("partial")),
        "conjunctive": bool(plan.get("conjunctive")),
        "source_grain": "request" if plan["mode"] == "raw_events" else "hourly_rollup",
    }
    if plan.get("reason"):
        meta["partial_reason"] = plan["reason"]
    return meta


def raw_events_where(plan: dict, days: int, endpoint: str,
                     accounts=None, region: str | None = None):
    """WHERE over f_request_events honouring EVERY selected attribute (AND across
    keys, OR within a key), plus endpoint/account/region scope."""
    where = ["event_date >= current_date - $1::int"]
    params: list = [days]
    ep = endpoint if endpoint in ("runtime", "mantle") else None
    if ep:
        params.append(ep); where.append(f"endpoint = ${len(params)}")
    accts, reg = _scope_lists(accounts, region)
    if accts:
        params.append(accts); where.append(f"accountId = ANY(${len(params)}::text[])")
    if reg:
        params.append(reg); where.append(f"region = ${len(params)}")
    for key, values in plan["selection"].items():
        if not values:
            continue
        # OR within one attribute: any of the selected values matches.
        params.append(key)
        key_param = len(params)
        params.append(values)
        val_param = len(params)
        where.append(f"(dimensions ->> ${key_param}) = ANY(${val_param}::text[])")
    return " AND ".join(where), params


@router.get("/attribution/xtab/summary")
async def xtab_summary(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """Overview KPI shape, attribute-filtered from the effective source
    (proxy → f_proxy_dim_hourly; invocation_logs → f_daily_tagged)."""
    if await _effective_source() == "invocation_logs":
        w, params = _tagged_where(days, dim_key, dim_value, accounts, region)
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
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    plan = await _gate_raw_mode(plan, days, endpoint, accounts, region)
    if plan["mode"] == "raw_events":
        # Conjunction across attributes, evaluated per request (audit T06).
        w, params = raw_events_where(plan, days, endpoint, accounts, region)
        row = await db.fetchrow(
            f"""
            SELECT
              COUNT(*)::BIGINT                                     AS total_requests,
              COUNT(*) FILTER (WHERE NOT throttled AND status < 400)::BIGINT
                                                                   AS successful_requests,
              COUNT(*) FILTER (WHERE throttled OR status >= 400)::BIGINT
                                                                   AS failed_requests,
              COALESCE(SUM(input_tokens),0)::BIGINT                AS total_input_tokens,
              COALESCE(SUM(output_tokens),0)::BIGINT               AS total_output_tokens,
              COUNT(*) FILTER (WHERE throttled)::BIGINT            AS throttled_requests,
              COUNT(*) FILTER (WHERE NOT throttled AND status >= 400)::BIGINT
                                                                   AS server_errors,
              COUNT(DISTINCT accountId)::BIGINT                    AS unique_accounts
            FROM f_request_events WHERE {w}
            """, *params)
        out = dict(row) if row else {}
        out.update(_plan_meta(plan))
        return out
    w, params = _proxy_where(days, _plan_key(plan, dim_key), _plan_values(plan, dim_key),
                             endpoint, accounts=accounts, region=region)
    row = await db.fetchrow(
        f"""
        SELECT
          COALESCE(SUM(total_requests),0)::BIGINT   AS total_requests,
          -- Audit T05: the ingester used to count a 429 as BOTH a throttle and
          -- an error, so this subtraction went negative and failed_requests
          -- double-counted. The populations are disjoint now; GREATEST/LEAST
          -- keep rows written before that fix from rendering impossible values.
          COALESCE(SUM(GREATEST(total_requests - throttled_count - error_count, 0)),0)::BIGINT AS successful_requests,
          COALESCE(SUM(LEAST(throttled_count + error_count, total_requests)),0)::BIGINT AS failed_requests,
          COALESCE(SUM(input_tokens),0)::BIGINT     AS total_input_tokens,
          COALESCE(SUM(output_tokens),0)::BIGINT    AS total_output_tokens,
          COALESCE(SUM(throttled_count),0)::BIGINT  AS throttled_requests,
          COALESCE(SUM(error_count),0)::BIGINT      AS server_errors,
          COUNT(DISTINCT accountId)::BIGINT         AS unique_accounts
        FROM f_proxy_dim_hourly WHERE {w}
        """, *params)
    out = dict(row) if row else {}
    out.update(_plan_meta(plan))
    return out


@router.get("/attribution/xtab/daily-trend")
async def xtab_daily_trend(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """Overview daily-trend shape, attribute-filtered from the effective source."""
    if await _effective_source() == "invocation_logs":
        w, params = _tagged_where(days, dim_key, dim_value, accounts, region)
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
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    plan = await _gate_raw_mode(plan, days, endpoint, accounts, region)
    if plan["mode"] == "raw_events":
        w, params = raw_events_where(plan, days, endpoint, accounts, region)
        rows = await db.fetch(
            f"""
            SELECT EXTRACT(YEAR FROM event_date)::INT AS year,
                   EXTRACT(MONTH FROM event_date)::INT AS month,
                   EXTRACT(DAY FROM event_date)::INT AS day,
              COUNT(*)::BIGINT AS total_requests,
              COUNT(*) FILTER (WHERE NOT throttled AND status < 400)::BIGINT AS successful_requests,
              COUNT(*) FILTER (WHERE throttled OR status >= 400)::BIGINT AS failed_requests,
              COALESCE(SUM(input_tokens),0)::BIGINT  AS input_tokens,
              COALESCE(SUM(output_tokens),0)::BIGINT AS output_tokens,
              COALESCE(SUM(cache_read_tokens),0)::BIGINT AS cache_read_tokens,
              COUNT(*) FILTER (WHERE throttled)::BIGINT AS throttled,
              COUNT(*) FILTER (WHERE endpoint='runtime')::BIGINT AS runtime_requests,
              COUNT(*) FILTER (WHERE endpoint='mantle')::BIGINT  AS mantle_requests
            FROM f_request_events WHERE {w}
            GROUP BY event_date ORDER BY event_date
            """, *params)
        return db.rows_to_dicts(rows)
    w, params = _proxy_where(days, _plan_key(plan, dim_key), _plan_values(plan, dim_key),
                             endpoint, accounts=accounts, region=region)
    rows = await db.fetch(
        f"""
        SELECT EXTRACT(YEAR FROM event_date)::INT AS year,
               EXTRACT(MONTH FROM event_date)::INT AS month,
               EXTRACT(DAY FROM event_date)::INT AS day,
          SUM(total_requests)::BIGINT AS total_requests,
          -- Clamped for the same reason as /attribution summary (audit T05).
          SUM(GREATEST(total_requests - throttled_count - error_count, 0))::BIGINT AS successful_requests,
          SUM(LEAST(throttled_count + error_count, total_requests))::BIGINT AS failed_requests,
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
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """requests-by-model shape, attribute-filtered from the effective source."""
    if await _effective_source() == "invocation_logs":
        w, params = _tagged_where(days, dim_key, dim_value, accounts, region)
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
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    plan = await _gate_raw_mode(plan, days, endpoint, accounts, region)
    if plan["mode"] == "raw_events":
        w, params = raw_events_where(plan, days, endpoint, accounts, region)
        rows = await db.fetch(
            f"""
            SELECT modelId,
              COUNT(*)::BIGINT AS total_requests,
              COALESCE(SUM(input_tokens),0)::BIGINT   AS input_tokens,
              COALESCE(SUM(output_tokens),0)::BIGINT  AS output_tokens
            FROM f_request_events WHERE {w}
            GROUP BY modelId ORDER BY total_requests DESC
            """, *params)
        return db.rows_to_dicts(rows)
    w, params = _proxy_where(days, _plan_key(plan, dim_key), _plan_values(plan, dim_key),
                             endpoint, accounts=accounts, region=region)
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
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """latency-by-model shape, attribute-filtered. Only the proxy source carries
    latency — invocation logs don't, so under invocation_logs we return [] and
    the Latency tab shows its graceful "not available for this source" state."""
    if await _effective_source() == "invocation_logs":
        return []
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    plan = await _gate_raw_mode(plan, days, endpoint, accounts, region)
    if plan["mode"] == "raw_events":
        # Raw events carry one latency per request, so these are TRUE percentiles
        # over the selected population - not a merge of per-bucket percentiles.
        w, params = raw_events_where(plan, days, endpoint, accounts, region)
        rows = await db.fetch(
            f"""
            SELECT modelId,
              COUNT(*) FILTER (WHERE latency_ms IS NOT NULL)::BIGINT AS sample_count,
              AVG(latency_ms) AS avg_e2e,
              PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_e2e,
              PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY latency_ms) AS p90_e2e,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_e2e,
              AVG(ttft_ms) AS avg_ttft,
              PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY ttft_ms) AS p50_ttft,
              PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ttft_ms) AS p90_ttft,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ttft_ms) AS p99_ttft,
              COUNT(*) FILTER (WHERE ttft_ms IS NOT NULL)::BIGINT AS ttft_sample_count,
              'true_population_percentile' AS percentile_basis
            FROM f_request_events WHERE {w}
            GROUP BY modelId HAVING COUNT(*) FILTER (WHERE latency_ms IS NOT NULL) > 0
            ORDER BY sample_count DESC
            """, *params)
        return db.rows_to_dicts(rows)
    w, params = _proxy_where(days, _plan_key(plan, dim_key), _plan_values(plan, dim_key),
                             endpoint, accounts=accounts, region=region)
    rows = await db.fetch(
        f"""
        SELECT modelId,
          SUM(total_requests)::BIGINT AS sample_count,
          SUM(p50_latency_ms * total_requests)/NULLIF(SUM(total_requests),0) AS avg_e2e,
          -- Audit T10: these are the WORST bucket's percentiles, not the
          -- population's. Percentiles are not mergeable, so MAX() over hourly
          -- buckets is an upper bound; the basis is declared so no caller can
          -- mistake it for a true quantile. The raw-event path above computes
          -- real ones when the window is inside raw retention.
          MAX(p50_latency_ms) AS p50_e2e,
          MAX(p90_latency_ms) AS p90_e2e,
          MAX(p99_latency_ms) AS p99_e2e,
          MAX(p50_ttft_ms) AS p50_ttft,
          MAX(p90_ttft_ms) AS p90_ttft,
          NULL::DOUBLE PRECISION AS avg_ttft,
          NULL::DOUBLE PRECISION AS p99_ttft,
          'worst_bucket_upper_bound' AS percentile_basis
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
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    dim_filter: list[str] | None = Query(None),
):
    """Daily request-volume breakdown (Overview's main stacked chart), attribute-
    filtered from the effective source. Mirrors /breakdown's shape
    (year/month/day/category/total_requests) with top-N + 'Other' folding.
    Only 'model' grouping has a per-attribute source; other groupings fall back
    to model so the chart still re-slices rather than ignoring the filter."""
    inv = await _effective_source() == "invocation_logs"
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    if not inv:
        plan = await _gate_raw_mode(plan, days, endpoint, accounts, region)
    # A conjunction of attributes can only be evaluated per request (audit T06).
    use_raw = (not inv) and plan["mode"] == "raw_events"
    if use_raw:
        table = "f_request_events"
        w, params = raw_events_where(plan, days, endpoint, accounts, region)
        req_expr = "COUNT(*)"
    elif inv:
        table = "f_daily_tagged"
        w, params = _tagged_where(days, _plan_key(plan, dim_key),
                                  _plan_values(plan, dim_key), accounts, region)
        req_expr = "SUM(total_requests)"
    else:
        table = "f_proxy_dim_hourly"
        w, params = _proxy_where(days, _plan_key(plan, dim_key),
                                 _plan_values(plan, dim_key), endpoint,
                                 accounts=accounts, region=region)
        req_expr = "SUM(total_requests)"
    # top-N model categories within the filtered slice
    top = await db.fetch(
        f"SELECT modelId AS cat, {req_expr}::BIGINT AS t "
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
          {req_expr}::BIGINT AS total_requests
        FROM {table} WHERE {w}
        -- Group by ORDINAL, not by the output aliases. `f_daily_tagged` and
        -- `f_daily` both have GENERATED columns literally named year/month/day,
        -- so an unqualified `GROUP BY year` binds to the table column rather than
        -- the alias - and then EXTRACT(YEAR FROM event_date) in the SELECT is
        -- ungrouped: "column f_daily_tagged.event_date must appear in the GROUP BY
        -- clause". `f_proxy_dim_hourly` has no such columns, which is why this
        -- only ever failed on the invocation_logs source. This query is
        -- polymorphic over three tables, so it must not depend on which.
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2, 3, 4
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


async def _ce_total(days: int, accounts=None) -> float:
    """The REAL Cost Explorer total for the window (f_daily_cost). This is the
    invoice figure the per-attribute slices must sum back to.

    Scoped by account when the top bar is scoped (audit T06): an account-scoped
    view previously multiplied a fleet-wide invoice by an account-scoped
    fraction, which is not a figure that means anything.
    """
    sql = ("SELECT COALESCE(SUM(total_cost),0)::numeric AS t FROM f_daily_cost "
           "WHERE event_date >= current_date - $1::int")
    params: list = [days]
    accts, _ = _scope_lists(accounts, None)
    if accts:
        params.append(accts)
        sql += f" AND accountId = ANY(${len(params)}::text[])"
    row = await db.fetchrow(sql, *params)
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
        # Audit T07: this fraction multiplies the AWS Cost Explorer total, so it
        # must be computed over AWS-BILLED traffic only. The proxy stream also
        # carries anthropic-api / openai-api calls, which the provider bills
        # directly and which never appear in Cost Explorer - on the demo dataset
        # that is 29.7% of proxy requests. Including them handed roughly 30% of
        # the AWS invoice to traffic AWS did not charge for, and understated
        # every Bedrock slice by the same amount.
        base_params.append(list(AWS_BILLED_ENDPOINTS))
        base_where += f" AND endpoint = ANY(${len(base_params)}::text[])"
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


async def _direct_provider_share(days: int, dim_key: str, dim_value,
                                 accounts=None) -> dict:
    """How much of the selected slice went straight to a provider rather than
    through Bedrock. That spend is real but AWS does not bill it, so it is
    reported separately instead of being folded into the Cost Explorer figure
    (audit T07)."""
    if await _effective_source() == "invocation_logs":
        # Invocation logs only exist for Bedrock calls, so there is nothing to
        # separate out.
        return {"direct_provider_tokens": 0, "aws_billed_tokens": 0,
                "direct_provider_pct": 0.0, "endpoints": []}
    where = "dim_key = $2 AND event_date >= current_date - $1::int"
    params: list = [days, dim_key]
    vals = [v for v in (dim_value or []) if v and v != "all"]
    if vals:
        params.append(vals); where += f" AND dim_value = ANY(${len(params)}::text[])"
    accts, _ = _scope_lists(accounts, None)
    if accts:
        params.append(accts); where += f" AND accountId = ANY(${len(params)}::text[])"
    rows = await db.fetch(
        f"SELECT endpoint, SUM(input_tokens + output_tokens)::BIGINT AS tok "
        f"FROM f_proxy_dim_hourly WHERE {where} GROUP BY endpoint", *params)
    aws_tok = direct_tok = 0
    direct_eps: list[str] = []
    for r in rows:
        ep, tok = r["endpoint"], int(r["tok"] or 0)
        if ep in AWS_BILLED_ENDPOINTS:
            aws_tok += tok
        else:
            direct_tok += tok
            if tok and ep not in direct_eps:
                direct_eps.append(ep)
    total = aws_tok + direct_tok
    return {
        "direct_provider_tokens": direct_tok,
        "aws_billed_tokens": aws_tok,
        "direct_provider_pct": round(100.0 * direct_tok / total, 2) if total else 0.0,
        "endpoints": sorted(direct_eps),
    }


@router.get("/attribution/xtab/cost-summary")
async def xtab_cost_summary(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """Total-spend KPI, attribute-filtered as a SHARE of the real CE total.
    cost = CE_total × (slice token-cost weight ÷ all-values weight), so the per-
    value slices sum back to the invoice total. Active accounts/services are the
    distinct counts within the filtered slice (so the KPI tiles aren't zero)."""
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    # Cost is allocated from Cost Explorer, which has no per-request grain, so a
    # conjunction cannot be honoured here at all. Mark it rather than implying it.
    if plan["mode"] == "raw_events":
        keys = [k for k in plan["selection"] if plan["selection"][k]]
        primary = keys[0]
        plan = {**plan, "mode": "hourly", "conjunctive": False, "partial": True,
                "selection": {primary: plan["selection"][primary]},
                "dropped_filters": [f"{k}={','.join(plan['selection'][k])}"
                                    for k in keys if k != primary],
                "reason": ("Cost Explorer has no per-request grain, so spend can be "
                           "attributed to one attribute at a time.")}
    key = _plan_key(plan, dim_key)
    vals = _plan_values(plan, dim_key)
    ce = await _ce_total(days, accounts)
    frac, _ = await _attr_cost_fraction(days, key, vals, endpoint)
    total = round(ce * frac, 2)
    # Audit T07: report the portion of this slice that a provider bills directly
    # rather than folding it into an AWS invoice figure.
    direct = await _direct_provider_share(days, key, vals, accounts)
    # Distinct accounts + models ("services") in the filtered slice, so the
    # Active accounts / Active services KPI tiles reflect the filter instead of 0.
    inv = await _effective_source() == "invocation_logs"
    if inv:
        w, params = _tagged_where(days, key, vals, accounts, region)
        table = "f_daily_tagged"
    else:
        w, params = _proxy_where(days, key, vals, endpoint,
                                 accounts=accounts, region=region)
        table = "f_proxy_dim_hourly"
    cnt = await db.fetchrow(
        f"SELECT COUNT(DISTINCT accountId)::INT AS accts, "
        f"COUNT(DISTINCT modelId)::INT AS models FROM {table} WHERE {w}", *params)
    out = {
        "total_cost": total, "currency": "USD",
        "unique_accounts": int(cnt["accts"] or 0),
        "unique_services": int(cnt["models"] or 0),
        "previous_total_cost": 0.0,
        "by_endpoint": {"runtime": total, "mantle": 0.0, "allocated": True},
        "window": {"days": days},
        "attribute_filtered": True,
        # This figure is a share of the AWS invoice, so it covers Bedrock traffic
        # only. Anything the provider billed directly is reported alongside it.
        "cost_scope": "aws_cost_explorer_bedrock_only",
        "direct_provider_traffic": direct,
    }
    out.update(_plan_meta(plan))
    return out


@router.get("/attribution/xtab/cost-by-model")
async def xtab_cost_by_model(
    days: int = Query(14, ge=1, le=90),
    endpoint: str = Query("all"),
    dim_key: str = Query(_DEFAULT_DIM),
    dim_value: list[str] | None = Query(None),
    # Audit T06: these were sent by the frontend and silently ignored.
    accounts: list[str] | None = Query(None),
    region: str = Query("all"),
    # Every attribute the user selected, as "key:value". More than one distinct
    # key means the selection is a conjunction, which the hourly fan-out cannot
    # express; see _dim_filter_plan.
    dim_filter: list[str] | None = Query(None),
):
    """Daily per-model spend (cost stacked chart), attribute-filtered as a share
    of the real CE total. Distributes CE_total across (day, model) in proportion
    to the slice's per-(day,model) token-cost weight, so the chart totals match
    the filtered KPI and the slices sum to the invoice."""
    inv = await _effective_source() == "invocation_logs"
    plan = dim_filter_plan(dim_filter, dim_key, dim_value, days)
    key = _plan_key(plan, dim_key)
    vals = _plan_values(plan, dim_key)
    if inv:
        w, params = _tagged_where(days, key, vals, accounts, region)
        in_c, out_c, table = "total_input_tokens", "total_output_tokens", "f_daily_tagged"
    else:
        # Audit T07: these dollars come from the AWS invoice, so only AWS-billed
        # endpoints may receive a share. Direct-provider calls are excluded here
        # and reported by /cost-summary's `direct_provider_traffic` instead of
        # silently absorbing part of the Bedrock bill.
        ep = endpoint if endpoint in AWS_BILLED_ENDPOINTS else "all"
        w, params = _proxy_where(days, key, vals, ep,
                                 accounts=accounts, region=region)
        if ep == "all":
            params.append(list(AWS_BILLED_ENDPOINTS))
            w += f" AND endpoint = ANY(${len(params)}::text[])"
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
    ce = await _ce_total(days, accounts)
    frac, _ = await _attr_cost_fraction(days, key, vals, endpoint)
    slice_dollars = ce * frac
    out = []
    for ev, mid, wv in weighted:
        out.append({"event_date": ev.isoformat(), "model_label": mid,
                    "total_cost": round(slice_dollars * (wv / slice_w), 4), "derived": True})
    return out
